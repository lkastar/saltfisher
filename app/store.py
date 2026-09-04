"""Persistence for one collection cycle.

Everything here is SYNCHRONOUS and meant to be called through
`asyncio.to_thread` (see .trellis/spec/backend/database-guidelines.md): SQLite
sessions are sync by design in this project, and `collector/` is forbidden from
touching the database at all.

The hit decision lives here rather than in the scheduler because it is a
database question. Answering "has this rule already told the user about this
item?" from in-memory state re-notifies everything after a restart.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.collector.base import RawItem, RawSeller
from app.collector.filters import describe_unverified
from app.collector.pipeline import Candidate
from app.config import settings
from app.models import Item, MonitorHit, PriceSnapshot, Seller, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotifiableHit:
    """One thing worth telling the user about, produced by the cycle.

    Carries the display data so the notifier needs no second query, and the
    waived-filter labels so "conservatively let through" cannot degrade into
    silently pushing items that do not meet the stated filters.
    """

    item_id: str
    title: str
    price_cents: int
    previous_price_cents: int | None
    reason: str  # new_in_range | price_drop
    url: str
    cover_url: str | None
    seller_nick: str
    unverified_labels: tuple[str, ...]


def item_url(item_id: str) -> str:
    return f"https://www.goofish.com/item?id={item_id}"


# --------------------------------------------------------------------------- #
# Upserts
# --------------------------------------------------------------------------- #


def upsert_seller_from_item(session: Session, raw: RawItem) -> None:
    """Create or refresh the seller row that an item points at.

    Search results carry nick, avatar and reputation for free, so the row is
    useful long before the profile page is ever fetched. `fetched_at` stays
    None until a real profile fetch happens, which is what marks the row as
    "identity only, profile pending".
    """
    if not raw.seller_id:
        return
    seller = session.get(Seller, raw.seller_id)
    if seller is None:
        seller = Seller(id=raw.seller_id, nick=raw.seller_nick)
        session.add(seller)
    if raw.seller_nick:
        seller.nick = raw.seller_nick
    if raw.seller_avatar_url:
        seller.avatar_url = raw.seller_avatar_url
    if raw.seller_is_shop is not None:
        seller.is_shop = raw.seller_is_shop
    if raw.seller_review_count is not None:
        seller.review_count = raw.seller_review_count
    if raw.seller_positive_rate is not None:
        seller.positive_rate = raw.seller_positive_rate


def upsert_seller_profile(session: Session, raw: RawSeller) -> None:
    seller = session.get(Seller, raw.seller_id)
    if seller is None:
        seller = Seller(id=raw.seller_id, nick=raw.nick)
        session.add(seller)
    for field_name in (
        "nick",
        "avatar_url",
        "is_shop",
        "credit_level",
        "credit_score",
        "verified",
        "sold_count",
        "reply_rate",
        "review_count",
        "positive_rate",
        "account_age_days",
    ):
        value = getattr(raw, field_name)
        # None means "not available from this source"; it must not overwrite a
        # value another source already provided.
        if value is not None and value != "":
            setattr(seller, field_name, value)
    seller.fetched_at = utcnow()
    seller.fetch_error = None


def upsert_item(session: Session, raw: RawItem, *, now: datetime | None = None) -> Item:
    now = now or utcnow()
    item = session.get(Item, raw.item_id)
    if item is None:
        item = Item(
            id=raw.item_id,
            title=raw.title,
            seller_id=raw.seller_id,
            seller_nick=raw.seller_nick,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(item)
    # Refreshed every cycle: this is what makes listing duration exact even
    # though snapshots are only written on change.
    item.last_seen_at = now
    item.title = raw.title
    item.status = raw.status
    if raw.description:
        item.description = raw.description
    if raw.cover_url:
        item.cover_url = raw.cover_url
    if raw.image_urls:
        item.image_urls = json.dumps(list(raw.image_urls), ensure_ascii=False)
    if raw.region:
        item.region = raw.region
    if raw.seller_nick:
        item.seller_nick = raw.seller_nick
    if raw.seller_avatar_url:
        item.seller_avatar_url = raw.seller_avatar_url
    if raw.publish_time:
        item.publish_time = raw.publish_time
    return item


def maybe_snapshot(
    session: Session, raw: RawItem, *, now: datetime | None = None
) -> PriceSnapshot | None:
    """Append a snapshot only when price or status changed.

    Item.last_seen_at is refreshed every cycle instead, which keeps the full
    price history while cutting row volume by two to three orders of magnitude.
    The first sighting always writes one: without a baseline there is no
    previous value to compare a later price against.
    """
    latest = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == raw.item_id)
        .order_by(PriceSnapshot.captured_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()
    if latest is not None and latest.price_cents == raw.price_cents and latest.status == raw.status:
        return None
    snapshot = PriceSnapshot(
        item_id=raw.item_id,
        price_cents=raw.price_cents,
        want_count=raw.want_count,
        view_count=raw.view_count,
        status=raw.status,
        captured_at=now or utcnow(),
        source=raw.source,
    )
    session.add(snapshot)
    return snapshot


# --------------------------------------------------------------------------- #
# Hit evaluation — the load-bearing logic of M1
# --------------------------------------------------------------------------- #


def evaluate_hit(
    session: Session,
    monitor_id: int,
    candidate: Candidate,
    *,
    baseline_done: bool,
    now: datetime | None = None,
) -> NotifiableHit | None:
    """Decide whether this rule should tell the user about this item.

    The rule is "the price ENTERED the range", not "the item is new". Four
    transitions must all work:

      1. first seen, in range          -> notify
      2. seen before ABOVE budget, now in range -> notify
      3. was in range, rose out, fell back      -> notify again
      4. in range already, dropped past the threshold -> notify again

    Deduping on "have we seen this item id" alone silently loses case 2, and
    case 2 is the most common way a deal actually appears.
    """
    now = now or utcnow()
    raw = candidate.item
    in_range = candidate.passed
    labels = tuple(describe_unverified(candidate.outcome.unverified))

    hit = session.get(MonitorHit, (monitor_id, raw.item_id))
    if hit is None:
        hit = MonitorHit(
            monitor_id=monitor_id,
            item_id=raw.item_id,
            first_hit_at=now,
            in_range=in_range,
            unverified_filters=json.dumps(list(labels), ensure_ascii=False) or None,
        )
        session.add(hit)
        if not in_range:
            return None
        if not baseline_done:
            # Establishing the baseline: record it, tell nobody. Creating a
            # rule must not dump the existing market into the user's inbox.
            #
            # The row is stamped as ALREADY ANNOUNCED. That is not cosmetic:
            # a baseline row is otherwise indistinguishable from "we decided
            # to notify and the send failed", and the retry branch below then
            # announces the entire baseline one cycle later — verified against
            # live data, where 7 of 30 baseline rows were re-announced on the
            # second cycle. Stamping it also makes the baseline price the
            # reference for a later drop, which is what the user expects: they
            # know about it at this price, so tell them if it falls.
            _stamp_as_known(hit, raw.price_cents, now)
            return None
        return _notifiable(raw, None, "new_in_range", labels)

    previous_price = hit.notified_price_cents
    was_in_range = hit.in_range
    hit.in_range = in_range
    hit.unverified_filters = json.dumps(list(labels), ensure_ascii=False) or None

    if not in_range:
        return None
    if not baseline_done:
        # A baseline spanning more than one cycle must stamp its rows too.
        if hit.notified_at is None:
            _stamp_as_known(hit, raw.price_cents, now)
        return None

    if not was_in_range:
        # Re-entered the range: either the first time it came into budget, or
        # it rose out and fell back. Both are news.
        return _notifiable(raw, previous_price, "new_in_range", labels)

    if hit.notified_at is None:
        # In range, in the ledger, but never actually announced — a previous
        # cycle decided to notify and the send failed. Retry.
        return _notifiable(raw, previous_price, "new_in_range", labels)

    if _cooling_down(hit.notified_at, now):
        return None

    if previous_price is not None and raw.price_cents < _drop_threshold(previous_price):
        return _notifiable(raw, previous_price, "price_drop", labels)
    return None


def _stamp_as_known(hit: MonitorHit, price_cents: int, now: datetime) -> None:
    """Mark a ledger row as one the user is considered informed about."""
    hit.notified_at = now
    hit.notified_price_cents = price_cents


def _drop_threshold(previous_cents: int) -> int:
    """The price a listing must fall below to be worth re-announcing.

    Integer arithmetic on cents: a float ratio here is how "price dropped"
    alerts start firing on rounding noise.
    """
    return previous_cents - (previous_cents * int(settings.price_drop_ratio * 100)) // 100


def _cooling_down(notified_at: datetime, now: datetime) -> bool:
    """Suppress repeats while an item flaps around the range boundary."""
    return now - notified_at < timedelta(minutes=settings.renotify_cooldown_minutes)


def _notifiable(
    raw: RawItem, previous: int | None, reason: str, labels: tuple[str, ...]
) -> NotifiableHit:
    return NotifiableHit(
        item_id=raw.item_id,
        title=raw.title,
        price_cents=raw.price_cents,
        previous_price_cents=previous,
        reason=reason,
        url=item_url(raw.item_id),
        cover_url=raw.cover_url,
        seller_nick=raw.seller_nick,
        unverified_labels=labels,
    )


def mark_notified(
    session: Session, monitor_id: int, hits: list[NotifiableHit], *, now: datetime | None = None
) -> None:
    """Record delivery AFTER a successful send.

    Writing this before sending loses a crashed message forever; writing it
    after risks at most one duplicate. For an alerting tool that is the correct
    direction to fail in.
    """
    now = now or utcnow()
    for hit in hits:
        row = session.get(MonitorHit, (monitor_id, hit.item_id))
        if row is None:
            continue
        row.notified_at = now
        row.notified_price_cents = hit.price_cents


def persist_cycle(
    session: Session,
    monitor_id: int,
    candidates: list[Candidate],
    *,
    baseline_done: bool,
    now: datetime | None = None,
) -> list[NotifiableHit]:
    """Write one cycle's observations and return what deserves a notification.

    Ordering inside the transaction matters: sellers before items (foreign
    key), items before snapshots, hits last so they see the fresh prices.
    """
    now = now or utcnow()
    notifiable: list[NotifiableHit] = []

    for candidate in candidates:
        upsert_seller_from_item(session, candidate.item)
    session.flush()

    for candidate in candidates:
        upsert_item(session, candidate.item, now=now)
    session.flush()

    for candidate in candidates:
        maybe_snapshot(session, candidate.item, now=now)

    for candidate in candidates:
        hit = evaluate_hit(session, monitor_id, candidate, baseline_done=baseline_done, now=now)
        if hit is not None:
            notifiable.append(hit)

    return notifiable
