"""The two polling loops.

Design (design.md section 1): keyword rules and watched items poll at very
different cadences, so each gets its own loop and its own timing — but every
request to the upstream goes through ONE semaphore, because the pacing rules
are a safety measure, not a throughput knob. That single permit also gives the
SQLite single-writer guarantee for free.

    search_loop ─┐
                 ├─ COLLECT_SEMAPHORE(1) ─▶ upstream
    watch_loop ──┘
"""

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    RawItem,
    RawSeller,
    SearchResult,
    TransientCollectorError,
)
from app.collector.filters import RuleFilters
from app.collector.pipeline import CHALLENGE_REASON_PREFIX, Pipeline
from app.collector.session import UpstreamSession
from app.config import SEARCH_ROWS, settings
from app.db import engine
from app.models import CollectRun, Item, Monitor, NotifyChannel, Seller, Watchlist, utcnow
from app.notify import (
    channels_for_monitor,
    deliver,
    enabled_channels,
    group_by_reason,
    log_delivery,
)
from app.notify.base import Notification, Notifier, challenge_body
from app.store import (
    NotifiableHit,
    fresh_seller_ids,
    mark_notified,
    mark_watch_notified,
    persist_cycle,
    persist_watch_observation,
    record_watch_failure,
)

log = logging.getLogger(__name__)

# One permit shared by both loops: monitors are collected one at a time.
COLLECT_SEMAPHORE = asyncio.Semaphore(1)

# Spacing between two monitors inside one sweep. Fixed cadence is the easiest
# bot signature there is, so every wait is jittered.
INTER_MONITOR_PAUSE = (3.0, 8.0)
SWEEP_TICK_SECONDS = 15.0
BACKOFF_CAP_SECONDS = 1800

# Per-monitor backoff, in memory on purpose: a restart legitimately clears it.
_backoff: dict[int, float] = {}

# The marker that says WHY a rule is off. `last_error` is the only durable
# record of that, so the import endpoint keys its resume on this exact string --
# which is why it is a constant used both where the challenge branch stamps it
# and where the resume reads it, instead of two hand-typed literals.
#
# It separates the three ways a rule ends up disabled:
#   - challenge          -> last_error contains this; resumed on import
#   - five plain failures-> last_error carries the collector's own message
#   - the user's own hand-> update_monitor writes neither
# Only the first is a state an import actually fixes.
NEEDS_VERIFICATION = "needs verification: "


def jittered(seconds: float) -> float:
    return seconds * random.uniform(0.85, 1.15)


def rule_filters(monitor: Monitor) -> RuleFilters:
    return RuleFilters(
        exclude_words=monitor.exclude_words,
        price_min_cents=monitor.price_min_cents,
        price_max_cents=monitor.price_max_cents,
        published_within_hours=monitor.published_within_hours,
        region=monitor.region,
        condition=monitor.condition,
        free_shipping=monitor.free_shipping,
        min_seller_credit=monitor.min_seller_credit,
        exclude_shop=monitor.exclude_shop,
    )


def _sync_due_monitor_ids(now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    with Session(engine) as session:
        due: list[int] = []
        for monitor in session.exec(select(Monitor).where(Monitor.enabled)).all():
            if monitor.id is None:
                continue
            wait = jittered(monitor.interval_seconds) + _backoff.get(monitor.id, 0.0)
            if monitor.last_run_at is None or monitor.last_run_at + timedelta(seconds=wait) <= now:
                due.append(monitor.id)
        return due


def _sync_load_monitor(monitor_id: int) -> Monitor | None:
    with Session(engine) as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is not None:
            session.expunge(monitor)
        return monitor


def _sync_fresh_sellers(seller_ids: list[str]) -> frozenset[str]:
    with Session(engine) as session:
        return fresh_seller_ids(session, seller_ids)


@dataclass(frozen=True, slots=True)
class SellerTarget:
    """Everything a seller rule needs before it can collect, read in one go.

    `numeric_id` is the id the listing API takes and `seller_id` the opaque one
    every Item points at; `known_item_id` is the listing the first mapping
    between them has to be read from.
    """

    seller_id: str
    nick: str
    numeric_id: str | None
    known_item_id: str | None


def _sync_seller_target(seller_id: str) -> SellerTarget | None:
    with Session(engine) as session:
        seller = session.get(Seller, seller_id)
        if seller is None:
            return None
        # Newest first: an old listing is likelier to have been deleted, and a
        # deleted one cannot answer with a sellerDO.
        known_item_id = session.exec(
            select(col(Item.id))
            .where(col(Item.seller_id) == seller_id)
            .order_by(col(Item.last_seen_at).desc())
        ).first()
        return SellerTarget(
            seller_id=seller_id,
            nick=seller.nick,
            numeric_id=seller.numeric_id,
            known_item_id=known_item_id,
        )


def _sync_store_numeric_id(seller_id: str, numeric_id: str) -> None:
    """Keep the resolved mapping, so it costs one request per seller ever."""
    with Session(engine) as session:
        seller = session.get(Seller, seller_id)
        if seller is None:
            return
        seller.numeric_id = numeric_id
        session.commit()


async def resolve_numeric_id(pipeline: Pipeline, target: SellerTarget) -> str:
    """The numeric userId the seller-listing API takes, resolved once and stored.

    Orchestrated here and not in `collector/`, which may not touch the database
    (spec/backend/directory-structure.md). Same shape as `_sync_fresh_sellers`:
    the database half runs in a thread, and the value is handed to the
    collection layer as a plain argument.

    Failure is a plain CollectorError carrying words a human can act on, which
    is what puts it in `last_error` and marks the run `ok=False`. Not silently
    skipped: a rule that never runs and never says why is the failure the
    health columns exist to prevent.
    """
    if target.numeric_id:
        return target.numeric_id
    if target.seller_id.isdigit():
        # Two rows in the real database are keyed on the numeric id itself,
        # written by the detail path before `8f10a77` re-keyed profiles onto
        # the opaque token. For those the mapping is already in hand, and
        # spending an upstream request to rediscover it would be buying
        # information we are holding.
        await asyncio.to_thread(_sync_store_numeric_id, target.seller_id, target.seller_id)
        return target.seller_id
    if target.known_item_id is None:
        raise CollectorError(
            f"seller {target.nick} has no stored listing, and the numeric id this rule "
            "needs is only readable from a listing's sellerDO. Collect one of their "
            "listings first (a keyword rule, or paste one of their links into the "
            "watchlist), then this rule starts working."
        )
    # The auxiliary route on purpose: a challenge on the DETAIL endpoint must
    # not mark the shared session dead, which is the live-data bug
    # collector-guidelines.md records. It also never raises, so the reason
    # reaches `last_error` as text rather than as a class.
    seller, reason = await pipeline.collect_seller_via_item(target.known_item_id)
    numeric_id = None if seller is None else seller.numeric_id
    if not numeric_id:
        raise CollectorError(
            f"could not read the numeric id for seller {target.nick} from listing "
            f"{target.known_item_id}: {reason or 'the response carried no sellerDO.sellerId'}"
        )
    await asyncio.to_thread(_sync_store_numeric_id, target.seller_id, numeric_id)
    log.info(
        "resolved a seller's numeric id",
        extra={"seller_id": target.seller_id, "numeric_id": numeric_id},
    )
    return numeric_id


async def collect_for_seller_rule(pipeline: Pipeline, monitor: Monitor) -> SearchResult:
    """The seller half of a cycle: resolve the id, then read the on-sale list."""
    assert monitor.seller_id is not None
    target = await asyncio.to_thread(_sync_seller_target, monitor.seller_id)
    if target is None:
        raise CollectorError(
            f"rule {monitor.id} watches seller {monitor.seller_id}, which is not in the database"
        )
    numeric_id = await resolve_numeric_id(pipeline, target)
    return await pipeline.collect_seller_listings(
        numeric_id,
        seller_id=target.seller_id,
        seller_nick=target.nick,
        pages=settings.search_pages,
    )


def _sync_persist_cycle(
    monitor_id: int, candidates: list, *, baseline_done: bool
) -> list[NotifiableHit]:
    with Session(engine) as session:
        hits = persist_cycle(session, monitor_id, candidates, baseline_done=baseline_done)
        monitor = session.get(Monitor, monitor_id)
        if monitor is not None:
            monitor.hit_count += len(hits)
            if not monitor.baseline_done:
                # Flipped in the SAME transaction that wrote the baseline hits,
                # so a crash mid-baseline cannot turn into a full-volume push
                # on the next cycle.
                monitor.baseline_done = True
        session.commit()
        return hits


def _sync_channels(monitor_id: int) -> list[NotifyChannel]:
    with Session(engine) as session:
        channels = channels_for_monitor(session, monitor_id)
        for channel in channels:
            session.expunge(channel)
        return channels


def _sync_commit_delivery(
    monitor_id: int,
    channel_id: int,
    notification: Notification,
    hits: list[NotifiableHit],
    error: str | None,
) -> None:
    """Record the attempt, and the dedup stamp only if it succeeded.

    Both writes happen in one transaction: a NotifyLog saying "sent" with no
    corresponding stamp would re-announce the same items next cycle.
    """
    with Session(engine) as session:
        log_delivery(session, channel_id, notification, monitor_id, error)
        if error is None:
            mark_notified(session, monitor_id, hits)
        session.commit()


async def dispatch_hits(
    registry: dict[str, Notifier],
    monitor_id: int,
    monitor_name: str,
    hits: list[NotifiableHit],
) -> None:
    """Send one cycle's hits, batched per reason, to every bound channel.

    No retry here: the next cycle IS the retry. A retry loop plus an
    unreachable SMTP host would stall the scheduler.
    """
    if not hits:
        return
    channels = await asyncio.to_thread(_sync_channels, monitor_id)
    if not channels:
        log.info("no channels bound", extra={"monitor_id": monitor_id, "hits": len(hits)})
        return

    for reason, group in group_by_reason(hits).items():
        notification = Notification(kind=reason, hits=group, monitor_name=monitor_name)
        for channel in channels:
            assert channel.id is not None
            error = await deliver(registry, channel, notification)
            if error is not None:
                log.error(
                    "notification failed",
                    extra={"channel_id": channel.id, "kind": channel.kind, "err": error},
                )
            await asyncio.to_thread(
                _sync_commit_delivery, monitor_id, channel.id, notification, group, error
            )


def _sync_record_run(
    *,
    monitor_id: int | None = None,
    item_id: str | None = None,
    ok: bool,
    item_count: int = 0,
    pages: int | None = None,
    collector: str | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Append one cycle to the run log. Cannot fail its caller.

    Its own session and its own catch, both deliberate. Sharing the caller's
    transaction would let a broken log write roll back the collection it was
    only supposed to observe, and an observation mechanism that can destroy
    what it observes is worse than no record at all. So a failure here is
    logged and dropped: collection is the business, this is instrumentation.
    """
    try:
        with Session(engine) as session:
            session.add(
                CollectRun(
                    monitor_id=monitor_id,
                    item_id=item_id,
                    started_at=started_at or utcnow(),
                    ok=ok,
                    item_count=item_count,
                    pages=pages,
                    collector=collector,
                    error=error,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - instrumentation must not break a cycle
        log.warning(
            "could not record collect run",
            exc_info=True,
            extra={"monitor_id": monitor_id, "item_id": item_id},
        )


def _sync_record_outcome(
    monitor_id: int,
    *,
    collector: str | None,
    error: str | None,
    disable: bool = False,
    item_count: int = 0,
    pages: int | None = None,
    partial_error: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Stamp the rule's current state, then append the cycle to the run log.

    Every search-cycle exit -- success and all four failure branches in
    _run_and_record, plus the manual run endpoint -- already funnels through
    here, so this is the one place the run log has to be written from. Adding
    it at each call site instead would mean six edits and a permanent invite
    to forget the next branch, and a run log missing its failures is exactly
    the log that cannot answer "did we collect that day".

    One exception, and it is the correct one: a rule deleted mid-cycle returns
    below without logging. The row's foreign key would have nowhere to point,
    and a cycle for a rule that no longer exists is not coverage of anything.

    `partial_error` is a page failure inside an otherwise working cycle, and it
    is deliberately kept out of the rule's state: the run row says ok=False so
    the supply chart hatches that cycle, while last_error and the failure
    streak stay clean. A rule that reliably gets page 1 and loses page 2 must
    not be auto-disabled after five cycles — that would trade a narrower
    aperture for no collection at all.
    """
    with Session(engine) as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return
        monitor.last_run_at = utcnow()
        monitor.last_error = error
        if collector:
            monitor.last_collector = collector
        if error is None:
            monitor.consecutive_failures = 0
        else:
            monitor.consecutive_failures += 1
            if disable or monitor.consecutive_failures >= settings.max_consecutive_failures:
                monitor.enabled = False
                monitor.last_error = (
                    f"auto-disabled after {monitor.consecutive_failures} failures: {error}"
                )
        session.commit()
    _sync_record_run(
        monitor_id=monitor_id,
        ok=error is None and partial_error is None,
        item_count=item_count,
        pages=pages,
        collector=collector,
        error=error or partial_error,
        started_at=started_at,
    )


def record_manual_run(
    monitor_id: int,
    *,
    collector: str | None,
    item_count: int = 0,
    pages: int | None = None,
    partial_error: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Stamp a successful manual run so the management page reflects it.

    Shares `_sync_record_outcome` with the scheduler rather than writing the
    same three fields a second way -- two writers for one row is how they start
    disagreeing.

    `item_count` has to be forwarded explicitly, and the first live drill of
    the run log is what proved it: the endpoint reported 30 listings collected
    while the run row said 0, because this wrapper simply did not pass the
    number on. A defaulted count is indistinguishable in the table from a
    cycle that genuinely saw an empty market.
    """
    _sync_record_outcome(
        monitor_id,
        collector=collector,
        error=None,
        item_count=item_count,
        pages=pages,
        partial_error=partial_error,
        started_at=started_at,
    )


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """What one cycle produced. `collector` must reach the database: it is how
    the management page shows that the cheap path has degraded.
    """

    hits: list[NotifiableHit] = field(default_factory=list)
    collector: str | None = None
    collected: int = 0
    passed: int = 0
    # The aperture this cycle actually observed, and any page that failed
    # inside it. `collected` is deduped across all of `pages`, so the two only
    # make sense read together.
    pages: int | None = None
    partial_error: str | None = None


async def run_monitor_cycle(pipeline: Pipeline, monitor: Monitor) -> CycleOutcome:
    """One collection cycle for one rule.

    Order is fixed (design.md section 5): collect, screen, persist, then hand
    the notifiable list back. Deciding "should we notify?" is a database
    question answered inside persist_cycle — answering it from in-memory state
    re-announces everything after a restart.
    """
    assert monitor.id is not None
    # Which target this rule has is the routing decision, and there is nothing
    # else to check: models._RULE_TARGET_CHECK enforces exactly one of the two.
    # For a seller rule, "newly listed" means "newly present in his on-sale
    # list", so everything after this point -- screening, the hit ledger,
    # baseline silence, backoff -- is shared with a keyword rule unchanged.
    #
    # settings.search_pages is read here, not inside the pipeline: this is the
    # one place that owns how many upstream requests a cycle is worth, and the
    # manual-run endpoint reaches the upstream through this same function.
    if monitor.keyword is None:
        result = await collect_for_seller_rule(pipeline, monitor)
    else:
        result = await pipeline.collect_search(
            monitor.keyword, rows=SEARCH_ROWS, pages=settings.search_pages
        )
    items = list(result.items)
    # Which sellers we already have a fresh profile for, so screening does not
    # re-buy one. One query, off the event loop, before any upstream request.
    fresh = await asyncio.to_thread(_sync_fresh_sellers, [i.seller_id for i in items])
    candidates = await pipeline.screen(items, rule_filters(monitor), fresh_sellers=fresh)
    hits = await asyncio.to_thread(
        _sync_persist_cycle, monitor.id, candidates, baseline_done=monitor.baseline_done
    )
    source = items[0].source if items else None
    passed = sum(1 for c in candidates if c.passed)
    log.info(
        "cycle ok",
        extra={
            "monitor_id": monitor.id,
            "collector": source,
            "pages": result.pages,
            "found": len(items),
            "passed": passed,
            "notifiable": len(hits),
            "partial": result.partial_error,
        },
    )
    return CycleOutcome(
        hits=hits,
        collector=source,
        collected=len(items),
        passed=passed,
        pages=result.pages,
        partial_error=result.partial_error,
    )


async def _run_and_record(
    pipeline: Pipeline, monitor_id: int, registry: dict[str, Notifier] | None = None
) -> CycleOutcome:
    """Wrap one cycle so that no failure can escape into the loop.

    The bare `except Exception` here is the one the project allows (see
    .trellis/spec/backend/error-handling.md): a single malformed listing must
    not stop every other monitor. It is paired with a full traceback and a
    persisted last_error, so nothing is ever swallowed silently.
    """
    monitor = await asyncio.to_thread(_sync_load_monitor, monitor_id)
    if monitor is None or not monitor.enabled:
        return CycleOutcome()
    # Captured BEFORE the cycle, not after it. The column is called
    # started_at, and until this existed nothing passed it, so every row was
    # stamped at completion instead -- a cycle beginning 23:59:50 UTC landed
    # on the next day and answered "did we collect that day" for the wrong
    # day. The window is milliseconds wide in practice, but a column whose
    # name disagrees with its contents misleads every later reader.
    started_at = utcnow()
    try:
        outcome = await run_monitor_cycle(pipeline, monitor)
    except TransientCollectorError as exc:
        _bump_backoff(monitor_id)
        await asyncio.to_thread(
            _sync_record_outcome,
            monitor_id,
            collector=None,
            error=f"transient: {exc}",
            started_at=started_at,
        )
        return CycleOutcome()
    except ChallengeError as exc:
        # Not a rate limit: a human must re-verify or re-import cookies. Stop
        # hammering and make the reason visible instead of backing off quietly.
        await asyncio.to_thread(
            _sync_record_outcome,
            monitor_id,
            collector=None,
            error=f"{NEEDS_VERIFICATION}{exc}",
            disable=True,
            started_at=started_at,
        )
        if registry is not None:
            # Reading last_error off the management page is not a notification.
            # Collection has stopped for everything, and until this the user
            # only found out by opening the panel.
            await dispatch_challenge(registry, pipeline.session, str(exc))
        return CycleOutcome()
    except CollectorError as exc:
        _bump_backoff(monitor_id)
        await asyncio.to_thread(
            _sync_record_outcome,
            monitor_id,
            collector=None,
            error=str(exc),
            started_at=started_at,
        )
        return CycleOutcome()
    except Exception as exc:  # noqa: BLE001 - the loop must outlive any bug
        log.exception("cycle failed", extra={"monitor_id": monitor_id})
        await asyncio.to_thread(
            _sync_record_outcome,
            monitor_id,
            collector=None,
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
        )
        return CycleOutcome()
    _backoff.pop(monitor_id, None)
    await asyncio.to_thread(
        _sync_record_outcome,
        monitor_id,
        collector=outcome.collector,
        error=None,
        item_count=outcome.collected,
        pages=outcome.pages,
        partial_error=outcome.partial_error,
        started_at=started_at,
    )
    if registry is not None and outcome.hits:
        # Deliberately outside the try above: a channel outage must not be
        # recorded as a collection failure, or a dead SMTP server would
        # auto-disable a perfectly healthy rule.
        await dispatch_hits(registry, monitor_id, monitor.name, outcome.hits)
    return outcome


def _bump_backoff(monitor_id: int) -> None:
    current = _backoff.get(monitor_id, 0.0)
    _backoff[monitor_id] = min(max(current * 2, 60.0), BACKOFF_CAP_SECONDS)


async def search_loop(
    pipeline: Pipeline, stop: asyncio.Event, registry: dict[str, Notifier] | None = None
) -> None:
    """Poll keyword rules until told to stop.

    Serial by design. With a handful of rules the worst-case delay is roughly
    rule_count x cycle_duration, which is why the management page has to show
    each rule's last run time — the delay must be visible rather than guessed.
    """
    log.info("search loop started")
    while not stop.is_set():
        try:
            due = await asyncio.to_thread(_sync_due_monitor_ids)
            for monitor_id in due:
                if stop.is_set():
                    break
                async with COLLECT_SEMAPHORE:
                    await _run_and_record(pipeline, monitor_id, registry)
                await asyncio.sleep(random.uniform(*INTER_MONITOR_PAUSE))
        except Exception:  # noqa: BLE001 - the loop itself must never die
            log.exception("search loop iteration failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_TICK_SECONDS)
    log.info("search loop stopped")


def _sync_due_watch_ids(now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    with Session(engine) as session:
        due: list[str] = []
        stmt = select(Watchlist).where(Watchlist.price_watch_enabled)
        for entry in session.exec(stmt).all():
            wait = jittered(entry.interval_seconds)
            if entry.last_run_at is None or entry.last_run_at + timedelta(seconds=wait) <= now:
                due.append(entry.item_id)
        return due


def _sync_persist_watch(
    item_id: str,
    raw: RawItem | None,
    seller: RawSeller | None,
    *,
    gone: bool,
    started_at: datetime | None = None,
) -> NotifiableHit | None:
    """Persist one watch observation, then log the cycle.

    A `gone` observation is a SUCCESSFUL cycle with nothing on sale to count,
    not a failure -- recording it as failed would make the supply chart read
    the disappearance as downtime.
    """
    with Session(engine) as session:
        hit = persist_watch_observation(session, item_id, raw, seller, gone=gone)
        session.commit()
    _sync_record_run(
        item_id=item_id,
        ok=True,
        item_count=0 if raw is None else 1,
        collector=raw.source if raw is not None else None,
        started_at=started_at,
    )
    return hit


def _sync_enabled_channels() -> list[NotifyChannel]:
    """Every enabled channel.

    Two callers, same reasoning: a watched item is not bound to a rule, and a
    challenge is not one rule's problem, so neither has a rule-level channel
    list to consult.
    """
    with Session(engine) as session:
        channels = enabled_channels(session)
        for channel in channels:
            session.expunge(channel)
        return channels


def _sync_commit_watch_delivery(
    channel_id: int, notification: Notification, hit: NotifiableHit, error: str | None
) -> None:
    with Session(engine) as session:
        log_delivery(session, channel_id, notification, None, error)
        if error is None:
            mark_watch_notified(session, hit)
        session.commit()


async def dispatch_watch_hit(registry: dict[str, Notifier], hit: NotifiableHit) -> None:
    channels = await asyncio.to_thread(_sync_enabled_channels)
    if not channels:
        log.info("no channels enabled", extra={"item_id": hit.item_id})
        return
    notification = Notification(kind=hit.reason, hits=[hit])
    for channel in channels:
        assert channel.id is not None
        error = await deliver(registry, channel, notification)
        if error is not None:
            log.error(
                "watch notification failed",
                extra={"channel_id": channel.id, "err": error},
            )
        await asyncio.to_thread(_sync_commit_watch_delivery, channel.id, notification, hit, error)


def _sync_log_challenge(channel_id: int, notification: Notification, error: str | None) -> None:
    """NotifyLog only. There is no per-item dedup stamp to write: the dedup for
    a challenge lives on the session (`take_challenge_notice`), because the
    thing being deduplicated is the episode, not an item.
    """
    with Session(engine) as session:
        log_delivery(session, channel_id, notification, None, error)
        session.commit()


async def dispatch_challenge(
    registry: dict[str, Notifier], upstream: UpstreamSession, detail: str
) -> None:
    """Announce a challenged session to EVERY enabled channel, once.

    Not the rule's bound channels: collection stopping is global, and a rule
    with nothing bound to it would otherwise fail in total silence -- which is
    exactly the failure this was written to remove.

    `take_challenge_notice` is the edge detector, and it is consulted before
    anything else here so that two loops times N rules produce one message.
    """
    notice = upstream.take_challenge_notice(detail)
    if notice is None:
        return
    notification = Notification(kind="challenge", hits=[], body=challenge_body(notice))
    channels = await asyncio.to_thread(_sync_enabled_channels)
    if not channels:
        log.warning("session challenged and no channel is enabled to say so")
        return
    for channel in channels:
        assert channel.id is not None
        error = await deliver(registry, channel, notification)
        if error is not None:
            log.error(
                "challenge notification failed",
                extra={"channel_id": channel.id, "kind": channel.kind, "err": error},
            )
        await asyncio.to_thread(_sync_log_challenge, channel.id, notification, error)


def resume_challenge_disabled() -> list[int]:
    """Re-enable what a challenge switched off, and only that.

    Called after a credential import. Clearing the failure state mirrors what
    `update_monitor` does for a manual re-enable -- leaving the streak intact
    would re-trip the auto-disable on the first hiccup.

    It also clears the seller profiles the same challenge poisoned. A failed
    profile fetch stamps `fetched_at` so the next cycle does not re-buy a
    request that just failed, but that stamp is read against the seven-day
    profile TTL: without this, one risk-control episode would suppress profile
    collection for a week on every seller screened during it, and it would come
    back silently. Scoped to the challenge reason -- a seller-specific failure
    is genuinely sticky and must keep its stamp.

    It deliberately does NOT run a cycle. Every resumed rule firing at once is
    precisely the request burst that gets a fresh session re-flagged, and the
    normal sweep picks them up within one tick anyway, serialised through
    COLLECT_SEMAPHORE with the usual jittered pause. Proof that recovery
    worked therefore comes from a real collection, not from the import --
    docs/operations.md:「判定成功看行为，不看 cookie 名单」.
    """
    resumed: list[int] = []
    watched: list[str] = []
    with Session(engine) as session:
        for monitor in session.exec(select(Monitor)).all():
            if monitor.enabled or NEEDS_VERIFICATION not in (monitor.last_error or ""):
                continue
            monitor.enabled = True
            monitor.consecutive_failures = 0
            monitor.last_error = None
            if monitor.id is not None:
                resumed.append(monitor.id)
        # Watched items are disabled by the same challenge through their own
        # counter (`record_watch_failure`), and they were missed here at first:
        # a re-import brought the search rules back while the watched item
        # stayed dead with nothing on screen to say why. Same marker, same
        # rule -- only the challenge class, never a hand-disabled entry or one
        # that failed five times for its own reasons.
        for entry in session.exec(select(Watchlist)).all():
            if entry.price_watch_enabled or NEEDS_VERIFICATION not in (entry.last_error or ""):
                continue
            entry.price_watch_enabled = True
            entry.consecutive_failures = 0
            entry.last_error = None
            watched.append(entry.item_id)

        cleared = session.exec(
            select(Seller).where(col(Seller.fetch_error).startswith(CHALLENGE_REASON_PREFIX))
        ).all()
        for seller in cleared:
            seller.fetch_error = None
            # Back to "never fetched", which is what it actually is. Leaving
            # the stamp would keep the seller inside `fresh_seller_ids`.
            seller.fetched_at = None
        session.commit()
    if resumed or watched or cleared:
        log.info(
            "resumed what the challenge stopped",
            extra={
                "monitor_ids": resumed,
                "watch_item_ids": watched,
                "sellers_cleared": len(cleared),
            },
        )
    return resumed


async def run_watch_cycle(
    pipeline: Pipeline, item_id: str, registry: dict[str, Notifier] | None = None
) -> NotifiableHit | None:
    """One cycle for one watched item.

    Uses the detail route, which is the only one that reports the item status —
    the signal that the thing the user was waiting for is gone.
    """
    started_at = utcnow()
    try:
        raw, seller = await pipeline.collect_item(item_id)
    except ItemGoneError:
        # Not a failure: the disappearance IS the observation, and it is what
        # the user most needs to hear.
        hit = await asyncio.to_thread(
            _sync_persist_watch, item_id, None, None, gone=True, started_at=started_at
        )
    except TransientCollectorError as exc:
        await asyncio.to_thread(record_watch_failure_sync, item_id, f"transient: {exc}", started_at)
        return None
    except ChallengeError as exc:
        await asyncio.to_thread(
            record_watch_failure_sync, item_id, f"{NEEDS_VERIFICATION}{exc}", started_at
        )
        if registry is not None:
            await dispatch_challenge(registry, pipeline.session, str(exc))
        return None
    except CollectorError as exc:
        await asyncio.to_thread(record_watch_failure_sync, item_id, str(exc), started_at)
        return None
    except Exception as exc:  # noqa: BLE001 - the loop must outlive any bug
        log.exception("watch cycle failed", extra={"item_id": item_id})
        await asyncio.to_thread(
            record_watch_failure_sync, item_id, f"{type(exc).__name__}: {exc}", started_at
        )
        return None
    else:
        hit = await asyncio.to_thread(
            _sync_persist_watch, item_id, raw, seller, gone=False, started_at=started_at
        )

    if hit is not None:
        log.info("watch alert", extra={"item_id": item_id, "reason": hit.reason})
        if registry is not None:
            await dispatch_watch_hit(registry, hit)
    return hit


def record_watch_failure_sync(item_id: str, error: str, started_at: datetime | None = None) -> None:
    """The single funnel for every watch-cycle failure branch.

    All four `except` arms of run_watch_cycle route through here, which is why
    the run log is written here rather than at each of them: a run log that
    records only the successful cycles is exactly the log that cannot answer
    "did we collect that day".
    """
    with Session(engine) as session:
        record_watch_failure(session, item_id, error)
        session.commit()
    _sync_record_run(item_id=item_id, ok=False, error=error, started_at=started_at)


async def watch_loop(
    pipeline: Pipeline, stop: asyncio.Event, registry: dict[str, Notifier] | None = None
) -> None:
    """Poll watched items.

    Separate from the search loop because the cadences differ by roughly five
    times, but sharing its semaphore: the pacing rules are a safety measure,
    not a per-loop budget.
    """
    log.info("watch loop started")
    while not stop.is_set():
        try:
            for item_id in await asyncio.to_thread(_sync_due_watch_ids):
                if stop.is_set():
                    break
                async with COLLECT_SEMAPHORE:
                    await run_watch_cycle(pipeline, item_id, registry)
                await asyncio.sleep(random.uniform(*INTER_MONITOR_PAUSE))
        except Exception:  # noqa: BLE001 - the loop itself must never die
            log.exception("watch loop iteration failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_TICK_SECONDS)
    log.info("watch loop stopped")
