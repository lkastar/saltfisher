"""Build the persistent development dataset the panel is exercised against.

`seed_demo.py` fills a database with four real listings so the pages are not
blank. This one fills a database with the *edge cases*: a rule with a large
ledger next to one that never matched, a listing that never changed price next
to one that sold mid-window, days the collector ran, days it only failed, and
days it never ran at all. Those are the states the charts and the three-state
components exist to tell apart, and none of them appear in a database that was
seeded to look healthy.

It is development data and it is NOT cleaned up: it lives in its own data dir
(`SFD_DATA_DIR=data/dev`) so the real `data/app.db` is never touched, and the
script refuses to run against the production dir outright.

    SFD_DATA_DIR=data/dev uv run python scripts/seed_dev.py --reset

**Provenance.** Every listing, price, title, seller, region and image URL is a
real capture: the four in `tests/fixtures`, plus — when a previously collected
database is available via `--from-db` — real rows copied out of it. Nothing
about a listing is invented, because a hand-written row makes a display bug and
a bad fixture indistinguishable.

What IS synthesised, and says so in the summary: the *history*. A capture is one
moment; the price series, the collect runs, the notification log and the
watchlist timestamps are built backwards from each listing's real current price,
the same trade `seed_demo.py` makes for the same reason. The only fully invented
values are the notification channel configs, which are operator settings rather
than captured data — and they point at `.invalid` hosts so nothing can deliver.
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlmodel import Session, col, delete, select  # noqa: E402

from app.collector.base import (  # noqa: E402
    RawItem,
    RawSeller,
    normalize_item,
    normalize_seller,
)
from app.collector.mtop import (  # noqa: E402
    flatten_detail,
    flatten_search_row,
    flatten_seller,
)
from app.config import settings  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    CollectRun,
    Item,
    Monitor,
    MonitorChannel,
    MonitorHit,
    NotifyChannel,
    NotifyLog,
    PriceSnapshot,
    Seller,
    Watchlist,
    utcnow,
)
from app.store import (  # noqa: E402
    maybe_snapshot,
    snapshot_ids_at,
    upsert_item,
    upsert_seller_profile,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures"
PROD_DATA_DIR = REPO_ROOT / "data"

# The upstream's own risk-control envelope, taken from the fixture rather than
# retyped: `Monitor.last_error` is rendered verbatim in the panel, and the
# whole point of the warn colour is that this exact string is recognisable.
CHALLENGE_RET = json.loads((FIXTURES / "challenge_synthetic.json").read_text(encoding="utf-8"))[
    "ret"
][0]

WINDOW_DAYS = 60  # > 45, so a 30-day chart is full and a 180-day one is not

# Day offsets (days before today) with no `CollectRun` at all, and with failed
# runs only. Both sets sit INSIDE the window rather than at its leading edge:
# a gap at the start of a series is also what "we had not started collecting
# yet" looks like, so only an inner gap proves a chart distinguishes downtime
# from a quiet market.
NO_RUN_DAYS = frozenset({15, 16, 17})
FAILED_ONLY_DAYS = frozenset({21, 22})


@dataclass(frozen=True)
class Shape:
    """A synthesised price history, as multipliers of the REAL current price.

    The last step is always 1.0 so the newest snapshot is the price actually
    captured; everything before it is extrapolated backwards and labelled as
    such in the summary.

    The four shapes are chosen for the branches they exercise, not for variety:

    - `flat` writes exactly ONE snapshot, because `PriceSnapshot` is
      append-on-change. It is the only thing that makes `monitor_trend`'s
      carry-forward visible: a listing with no snapshot inside the window has
      to keep counting at its last observed price.
    - `sold` ends with a status-only snapshot, the drop-out branch — a sold
      listing stops counting from that day.
    - `drop` / `rise` give `formatChangeRatio` both signs to render, which
      matters because green means *cheaper* here and the glyph has to carry it.
    """

    steps: tuple[tuple[float, str], ...]
    end_day_ago: int = 2


SHAPES = {
    "drop": Shape(((1.22, "on_sale"), (1.14, "on_sale"), (1.06, "on_sale"), (1.0, "on_sale"))),
    "flat": Shape(((1.0, "on_sale"),)),
    "rise": Shape(((0.86, "on_sale"), (0.93, "on_sale"), (1.0, "on_sale"))),
    # Sold 12 days ago, i.e. inside a 30-day window and outside a 7-day one.
    "sold": Shape(((1.08, "on_sale"), (1.0, "on_sale"), (1.0, "sold")), end_day_ago=12),
}

# The four fixture listings, pinned to a shape each so every branch above is
# covered even with no `--from-db` pool. Ages are >= 41 days so the 30-day
# window is fully populated by carry-forward from before it starts.
FIXTURE_PLAN: dict[str, tuple[str, int]] = {
    "1081955696076": ("drop", 52),
    "1079892102343": ("rise", 47),
    "1081955312115": ("flat", 41),
    "1079129727205": ("sold", 45),
}

# (shape, days since first observed) for listings copied from --from-db. The
# ages are deliberately scattered: grouped by `first_hit_at` they are what
# gives `supply_trend` a per-day count instead of one spike, and the young ones
# are what puts a non-zero number on the overview's "today" KPI.
POOL_PLAN: tuple[tuple[str, int], ...] = (
    ("drop", 44),
    ("flat", 3),
    ("rise", 37),
    ("sold", 30),
    ("drop", 21),
    ("flat", 0),
    ("rise", 12),
    ("drop", 26),
    ("flat", 48),
    ("rise", 6),
    ("drop", 16),
    ("sold", 18),
    ("flat", 9),
    ("rise", 33),
    ("drop", 1),
    ("flat", 24),
)

# (shape to draw the item from, days ago it was added, note, last_error).
# `added_at` is scattered so "the 5 most recently added" on the overview is a
# real ordering and not just insertion order, and each row lands on a different
# change sign: added while it was expensive -> drop, while it was cheap -> rise.
WATCH_PLAN: tuple[tuple[str, int, str, str | None], ...] = (
    ("drop", 33, "跌到 2400 以内就出手", None),
    ("rise", 24, "参考价，别追高", None),
    ("flat", 20, "一直没动，看它能挂多久", None),
    ("sold", 13, "已售，留着看成交价", None),
    ("drop", 6, "备用机，颜色不挑", "detail 抓取失败: HTTP 500"),
    ("flat", 0, "刚加的，观察一周", None),
)

# (hours ago, kind, channel, ok, item_count, error). The first four are inside
# the rolling 24h window the overview KPI counts, and two of the twelve failed:
# a push log where everything succeeded never exercises the failure row.
NOTIFY_PLAN: tuple[tuple[int, str, str, bool, int, str | None], ...] = (
    (2, "price_drop", "email", True, 3, None),
    (5, "new_in_range", "telegram", True, 7, None),
    (9, "new_in_range", "email", False, 4, "smtp: (535) 5.7.8 authentication failed"),
    (20, "price_drop", "telegram", True, 1, None),
    (27, "challenge", "email", True, 0, None),
    (33, "new_in_range", "email", True, 11, None),
    (48, "test", "telegram", True, 0, None),
    (52, "gone", "email", True, 2, None),
    (76, "new_in_range", "telegram", False, 5, "telegram: 400 Bad Request: chat not found"),
    (101, "price_drop", "email", True, 2, None),
    (149, "new_in_range", "email", True, 9, None),
    (173, "challenge", "telegram", True, 0, None),
)


# --------------------------------------------------------------------------- #
# Real listings in
# --------------------------------------------------------------------------- #


def fixture_items() -> tuple[list[RawItem], RawSeller | None]:
    """The four real captures, through the real normalising path."""
    search = json.loads((FIXTURES / "search_real.json").read_text(encoding="utf-8"))
    rows = search.get("data", {}).get("resultList") or []
    items = [normalize_item(flatten_search_row(row), "mtop") for row in rows]

    detail = json.loads((FIXTURES / "detail_real.json").read_text(encoding="utf-8"))
    data = detail.get("data") or {}
    item_do = data.get("itemDO") or {}
    seller_do = data.get("sellerDO") or {}
    item_id = str(item_do.get("itemId") or "")
    items.append(normalize_item(flatten_detail(item_do, seller_do, item_id), "detail"))
    seller = (
        normalize_seller(flatten_seller(seller_do), items[-1].seller_id, "detail")
        if seller_do
        else None
    )
    return items, seller


POOL_SQL = """
SELECT i.id, i.title, i.description, i.cover_url, i.image_urls, i.region,
       i.seller_id, i.seller_nick, i.seller_avatar_url, i.publish_time,
       p.price_cents, p.want_count, p.view_count, p.source,
       s.is_shop, s.review_count, s.positive_rate
FROM item i
JOIN (SELECT item_id, price_cents, want_count, view_count, source,
             ROW_NUMBER() OVER (PARTITION BY item_id
                                ORDER BY captured_at DESC, id DESC) AS rn
      FROM pricesnapshot) p ON p.item_id = i.id AND p.rn = 1
LEFT JOIN seller s ON s.id = i.seller_id
WHERE p.price_cents > 0 AND i.cover_url LIKE 'http%'
ORDER BY i.first_seen_at DESC, i.id
"""


def pool_items(path: Path, want: int, exclude: set[str]) -> list[RawItem]:
    """Real listings copied out of an already-collected database.

    The fixtures hold four listings, which is not enough to have a rule with a
    "large" ledger, six watchlist entries, or a price distribution with a shape.
    The alternative to copying real rows is inventing listings, which this
    project does not do — so an absent source database degrades the dataset
    (and says so) rather than being papered over with fiction.

    Opened read-only: this is usually the operator's live database.
    """
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(POOL_SQL).fetchall()
    finally:
        conn.close()

    items: list[RawItem] = []
    for row in rows:
        if len(items) >= want:
            break
        (
            item_id,
            title,
            description,
            cover_url,
            image_urls,
            region,
            seller_id,
            seller_nick,
            seller_avatar_url,
            publish_time,
            price_cents,
            want_count,
            view_count,
            source,
            is_shop,
            review_count,
            positive_rate,
        ) = row
        if item_id in exclude or not title or not seller_id:
            continue
        items.append(
            RawItem(
                item_id=item_id,
                title=title,
                price_cents=int(price_cents),
                seller_id=seller_id,
                seller_nick=seller_nick or seller_id,
                # The snapshot's own source column, not a guess: these rows
                # came off mtop or the browser fallback, and which one it was
                # is part of the capture.
                source=source or "mtop",
                description=description,
                cover_url=cover_url,
                image_urls=tuple(json.loads(image_urls)) if image_urls else (),
                region=region,
                seller_avatar_url=seller_avatar_url,
                publish_time=_parse_stored(publish_time),
                want_count=want_count,
                view_count=view_count,
                seller_is_shop=None if is_shop is None else bool(is_shop),
                seller_review_count=review_count,
                seller_positive_rate=positive_rate,
            )
        )
    return items


def _parse_stored(value: str | None) -> datetime | None:
    """Re-attach the UTC the storage layer strips (see models.UtcDateTime)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# History out
# --------------------------------------------------------------------------- #


def observation_days(first_day_ago: int, end_day_ago: int, count: int) -> list[int]:
    """Day offsets for `count` observations spread over a listing's life."""
    if count == 1:
        return [first_day_ago]
    span = max(first_day_ago - end_day_ago, 0)
    step = span / (count - 1)
    return [round(first_day_ago - step * index) for index in range(count)]


def self_check() -> None:
    """The one invariant the dataset silently depends on, asserted before use.

    A dev script gets no place in `tests/`, but `observation_days` is the piece
    that decides which day a listing sells on, and getting it wrong produces a
    plausible-looking database whose drop-out lands outside the window — i.e.
    exactly the failure this dataset exists to make visible.
    """
    assert observation_days(52, 2, 1) == [52]  # one step: no spreading at all
    drop = observation_days(52, 2, 4)
    assert drop[0] == 52 and drop[-1] == 2, drop
    assert drop == sorted(drop, reverse=True), drop
    sold = observation_days(45, 12, 3)
    assert sold[-1] == 12, sold  # the sale lands on the shape's end day
    assert observation_days(1, 2, 3) == [1, 1, 1]  # younger than its own shape


def write_history(session: Session, raw: RawItem, shape: Shape, first_day_ago: int, now: datetime):
    """Replay a synthesised series through the real upsert path.

    Through `upsert_item`/`maybe_snapshot` rather than by inserting rows: the
    append-on-change rule is enforced there, so a flat listing ends with exactly
    one snapshot here for the same reason it does in production.
    """
    days = observation_days(first_day_ago, shape.end_day_ago, len(shape.steps))
    last_status = "on_sale"
    for (factor, status), day_ago in zip(shape.steps, days, strict=True):
        at = now - timedelta(days=day_ago)
        observed = replace(raw, price_cents=int(raw.price_cents * factor), status=status)
        upsert_item(session, observed, now=at)
        maybe_snapshot(session, observed, now=at)
        last_status = status
    if last_status == "on_sale":
        # The cycle that just ran: it saw the listing unchanged, so it refreshes
        # last_seen_at and writes no snapshot. Without it every listing looks
        # stale to `analytics.fresh_cutoff`, which counts in poll intervals.
        upsert_item(session, raw, now=now - timedelta(minutes=4))
    session.commit()
    return now - timedelta(days=days[0])


def price_at(session: Session, item_id: str, when: datetime) -> int:
    """What we had observed this listing at, as of `when`."""
    newest = snapshot_ids_at(when).subquery()
    row = session.exec(
        select(col(PriceSnapshot.price_cents))
        .join(newest, col(PriceSnapshot.id) == newest.c.snapshot_id)
        .where(col(PriceSnapshot.item_id) == item_id)
    ).first()
    return int(row) if row is not None else 0


# --------------------------------------------------------------------------- #
# Rules, ledger, runs
# --------------------------------------------------------------------------- #


def seed_monitors(session: Session, seller_id: str, now: datetime) -> dict[str, Monitor]:
    """Five rules, one per shape of rule the panel has to render.

    Every one is disabled: an enabled rule in a dev database would have the
    scheduler calling goofish from a developer's laptop, which is exactly the
    traffic the interval floor exists to prevent.
    """
    rules = {
        "big": Monitor(
            name="开发：iPhone 15 128G（主力）",
            keyword="iPhone 15 128G",
            price_min_cents=200_000,
            price_max_cents=330_000,
            exclude_words="碎屏,配件",
            interval_seconds=300,
            enabled=False,
            baseline_done=True,
            last_run_at=now - timedelta(minutes=4),
            last_collector="mtop",
        ),
        "small": Monitor(
            name="开发：iPhone 15 256G（少量命中）",
            keyword="iPhone 15 256G",
            interval_seconds=900,
            enabled=False,
            baseline_done=True,
            last_run_at=now - timedelta(minutes=11),
            last_collector="mtop",
        ),
        # A rule whose searches came back empty every time. Its ledger is empty
        # and its baseline never completed, which is the one case where an
        # empty table means "working, nothing found" and not "broken".
        "empty": Monitor(
            name="开发：AirPods Pro 2（从未命中）",
            keyword="AirPods Pro 2 全新",
            price_max_cents=120_000,
            interval_seconds=1800,
            enabled=False,
            baseline_done=False,
            last_run_at=now - timedelta(minutes=22),
            last_collector="mtop",
        ),
        "failing": Monitor(
            name="开发：Switch OLED（采集报错）",
            keyword="Switch OLED 续航版",
            interval_seconds=600,
            enabled=False,
            baseline_done=True,
            last_run_at=now - timedelta(minutes=7),
            last_collector="mtop",
            last_error=CHALLENGE_RET,
            consecutive_failures=3,
        ),
        # keyword=None with seller_id set is the other side of the xor CHECK in
        # models.Monitor, and a separate rendering path in the panel.
        "seller": Monitor(
            name="开发：卖家在售（详情页卖家）",
            keyword=None,
            seller_id=seller_id,
            interval_seconds=1200,
            enabled=False,
            baseline_done=True,
            last_run_at=now - timedelta(minutes=15),
            last_collector="mtop",
        ),
    }
    for rule in rules.values():
        session.add(rule)
    session.commit()
    for rule in rules.values():
        session.refresh(rule)
    return rules


def seed_ledger(
    session: Session,
    rules: dict[str, Monitor],
    ledgers: dict[str, list[str]],
    first_seen: dict[str, datetime],
    statuses: dict[str, str],
    now: datetime,
) -> None:
    for key, item_ids in ledgers.items():
        rule = rules[key]
        for index, item_id in enumerate(item_ids):
            sold = statuses[item_id] != "on_sale"
            session.merge(
                MonitorHit(
                    monitor_id=rule.id,
                    item_id=item_id,
                    first_hit_at=first_seen[item_id],
                    # A sold listing stopped being returned by the search, so
                    # this rule's clock for it stopped then too. Keeping it
                    # fresh is the exact bug last_hit_at was added for.
                    last_hit_at=now - (timedelta(days=12) if sold else timedelta(minutes=4)),
                    in_range=index % 4 != 0,
                    notified_at=now - timedelta(hours=6) if index % 3 == 0 else None,
                    notified_price_cents=price_at(session, item_id, now)
                    if index % 3 == 0
                    else None,
                    # Filters the upstream cannot express were waived on some
                    # rows; the 标注 column renders only when a rule is selected.
                    unverified_filters=(
                        json.dumps(["region", "min_seller_credit"]) if index % 5 == 0 else None
                    ),
                )
            )
        rule.hit_count = len(item_ids)
        session.add(rule)
    session.commit()


def seed_runs(
    session: Session,
    rules: dict[str, Monitor],
    ledgers: dict[str, list[str]],
    watch_items: list[tuple[str, str | None]],
    now: datetime,
) -> None:
    """Collect cycles, including the days that produce a gap in the charts."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def add(at: datetime, *, ok: bool, **kwargs: object) -> None:
        # A cycle whose slot has not come round yet today is pulled back to just
        # before now rather than dropped: dropping it makes today's `collected`
        # depend on the clock time the seed was run, so a rule polled at 12:00
        # UTC would look down all morning on a dataset seeded at 09:00.
        started = min(at, now - timedelta(minutes=5))
        session.add(CollectRun(started_at=started, ok=ok, **kwargs))  # type: ignore[arg-type]

    big = rules["big"]
    for offset in range(WINDOW_DAYS):
        if offset in NO_RUN_DAYS:
            continue
        day = midnight - timedelta(days=offset)
        failed_only = offset in FAILED_ONLY_DAYS
        for hour in (3, 15):
            add(
                day + timedelta(hours=hour),
                ok=not failed_only,
                monitor_id=big.id,
                item_count=0 if failed_only else len(ledgers["big"]),
                pages=None if failed_only else 2,
                collector="mtop",
                error=CHALLENGE_RET if failed_only else None,
            )
        if offset < 2:
            # One recent failure so the 24h success-rate KPI is not a flat 100%,
            # which is the only value that proves nothing about the arithmetic.
            add(
                day + timedelta(hours=9),
                ok=False,
                monitor_id=big.id,
                collector="mtop",
                error="httpx.ReadTimeout: timed out",
            )

    for offset in range(7):
        add(
            midnight - timedelta(days=offset) + timedelta(hours=8),
            ok=True,
            monitor_id=rules["small"].id,
            item_count=len(ledgers["small"]),
            pages=1,
            collector="mtop",
        )

    for offset in range(14):
        add(
            midnight - timedelta(days=offset) + timedelta(hours=6),
            ok=True,
            monitor_id=rules["empty"].id,
            item_count=0,
            pages=1,
            collector="mtop",
        )

    # Broke five days ago and has failed every cycle since: consecutive_failures
    # on the rule is the current state, this is the history behind it.
    for offset in range(12):
        add(
            midnight - timedelta(days=offset) + timedelta(hours=10),
            ok=offset >= 5,
            monitor_id=rules["failing"].id,
            item_count=0 if offset < 5 else 4,
            pages=None if offset < 5 else 1,
            collector="mtop",
            error=CHALLENGE_RET if offset < 5 else None,
        )

    for offset in range(10):
        add(
            midnight - timedelta(days=offset) + timedelta(hours=12),
            ok=True,
            monitor_id=rules["seller"].id,
            item_count=len(ledgers["seller"]),
            pages=1,
            collector="mtop",
        )

    # Watch cycles: item_id set instead of monitor_id, and no `pages` — a watch
    # cycle fetches one detail page and does not search.
    for item_id, watch_error in watch_items:
        for offset in range(6):
            failed = watch_error is not None and offset < 2
            add(
                midnight - timedelta(days=offset) + timedelta(hours=13),
                ok=not failed,
                item_id=item_id,
                item_count=0 if failed else 1,
                collector="detail",
                error=watch_error if failed else None,
            )
    session.commit()


def seed_watchlist(
    session: Session,
    by_shape: dict[str, list[tuple[str, int]]],
    now: datetime,
) -> list[tuple[str, str | None]]:
    used: set[str] = set()
    entries: list[tuple[str, str | None]] = []
    for shape_name, days_ago, note, error in WATCH_PLAN:
        pick = next(
            (
                item_id
                for item_id, first_day_ago in by_shape.get(shape_name, [])
                # Added after we first saw it, or there is no observed price to
                # record as the baseline and every change would read as +inf.
                if item_id not in used and first_day_ago > days_ago
            ),
            None,
        )
        if pick is None:
            continue
        used.add(pick)
        added_at = now - timedelta(days=days_ago)
        session.add(
            Watchlist(
                item_id=pick,
                added_at=added_at,
                added_price_cents=price_at(session, pick, added_at),
                note=note,
                # Off for the same reason every seeded rule is disabled, and it
                # is not the same switch: the watch loop is gated per entry by
                # this column (`scheduler._sync_due_watch_ids`), so an enabled
                # entry has a dev machine fetching goofish detail pages within
                # a minute of the panel starting -- measured, on the first run
                # of this script. Flip one on by hand to exercise that path.
                price_watch_enabled=False,
                interval_seconds=600,
                last_run_at=now - timedelta(minutes=9),
                last_error=error,
                consecutive_failures=2 if error else 0,
            )
        )
        entries.append((pick, error))
    session.commit()
    return entries


def seed_notify(session: Session, rules: dict[str, Monitor], now: datetime) -> dict[str, int]:
    """Channels and their log.

    The configs here are the one invented thing in this file — they are
    operator settings, not captured data. Hosts are under `.invalid` (reserved
    by RFC 2606, guaranteed not to resolve) and the secrets are labelled, so a
    misfired test send cannot reach anybody.
    """
    email = NotifyChannel(
        kind="email",
        label="开发邮箱",
        config=json.dumps(
            {
                "smtp_host": "smtp.example.invalid",
                "smtp_port": 465,
                "username": "dev@example.invalid",
                "password": "dev-placeholder-not-a-secret",
                "from_addr": "dev@example.invalid",
                "to_addrs": ["dev@example.invalid"],
            }
        ),
        enabled=True,
        created_at=now - timedelta(days=WINDOW_DAYS),
    )
    telegram = NotifyChannel(
        kind="telegram",
        label="开发 Telegram",
        config=json.dumps(
            {
                "bot_token": "0000000000:DEV-PLACEHOLDER-not-a-real-token",
                "chat_id": "-1000000000000",
            }
        ),
        enabled=True,
        created_at=now - timedelta(days=WINDOW_DAYS),
    )
    session.add(email)
    session.add(telegram)
    session.commit()
    session.refresh(email)
    session.refresh(telegram)
    ids = {"email": int(email.id or 0), "telegram": int(telegram.id or 0)}

    # Only two of the five rules get a channel. A rule with none is a real and
    # easy-to-reach state — it collects and notifies nobody — and the panel
    # warns about it, which cannot be seen if every rule is wired up.
    session.add(MonitorChannel(monitor_id=rules["big"].id, channel_id=ids["email"]))
    session.add(MonitorChannel(monitor_id=rules["big"].id, channel_id=ids["telegram"]))
    session.add(MonitorChannel(monitor_id=rules["failing"].id, channel_id=ids["email"]))

    for hours_ago, kind, channel, ok, item_count, error in NOTIFY_PLAN:
        session.add(
            NotifyLog(
                channel_id=ids[channel],
                kind=kind,
                monitor_id=None if kind == "test" else rules["big"].id,
                item_count=item_count,
                ok=ok,
                error=error,
                sent_at=now - timedelta(hours=hours_ago),
            )
        )
    session.commit()
    return ids


def reset(session: Session) -> None:
    # Child tables first: every foreign key here points at Monitor, Item,
    # Seller or NotifyChannel.
    for table in (
        MonitorChannel,
        NotifyLog,
        NotifyChannel,
        CollectRun,
        MonitorHit,
        Watchlist,
        PriceSnapshot,
        Monitor,
        Item,
        Seller,
    ):
        session.exec(delete(table))  # type: ignore[call-overload]
    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="empty every seeded table first")
    parser.add_argument(
        "--from-db",
        default=str(PROD_DATA_DIR / "app.db"),
        help="already-collected database to copy extra REAL listings from; 'none' to skip",
    )
    parser.add_argument(
        "--pool", type=int, default=len(POOL_PLAN), help="how many of those listings to copy"
    )
    args = parser.parse_args()

    if settings.data_dir.resolve() == PROD_DATA_DIR.resolve():
        print(
            "refused: SFD_DATA_DIR resolves to the production data dir "
            f"({PROD_DATA_DIR}).\n"
            "  This script writes and (with --reset) deletes rows. Run it as:\n"
            "    SFD_DATA_DIR=data/dev uv run python scripts/seed_dev.py --reset",
            file=sys.stderr,
        )
        return 2

    self_check()
    init_db()
    now = utcnow()
    fixtures, detail_seller = fixture_items()
    plan: list[tuple[RawItem, str, int]] = []
    for item in fixtures:
        shape_name, first_day_ago = FIXTURE_PLAN[item.item_id]
        plan.append((item, shape_name, first_day_ago))

    source = None if args.from_db == "none" else Path(args.from_db)
    pool = (
        pool_items(source, min(args.pool, len(POOL_PLAN)), {item.item_id for item in fixtures})
        if source is not None
        else []
    )
    for item, (shape_name, first_day_ago) in zip(pool, POOL_PLAN, strict=False):
        plan.append((item, shape_name, first_day_ago))

    with Session(engine) as session:
        if args.reset:
            reset(session)

        if detail_seller is not None:
            upsert_seller_profile(session, detail_seller)
            session.commit()

        by_shape: dict[str, list[tuple[str, int]]] = {}
        first_seen: dict[str, datetime] = {}
        statuses: dict[str, str] = {}
        for raw, shape_name, first_day_ago in plan:
            # Search rows carry the seller's review count, positive rate and
            # shop flag; writing only id and nick would throw away real captured
            # data and leave the profile panel showing 未知.
            upsert_seller_profile(
                session,
                RawSeller(
                    seller_id=raw.seller_id,
                    nick=raw.seller_nick,
                    source=raw.source,
                    avatar_url=raw.seller_avatar_url,
                    is_shop=raw.seller_is_shop,
                    review_count=raw.seller_review_count,
                    positive_rate=raw.seller_positive_rate,
                ),
            )
            session.commit()
            shape = SHAPES[shape_name]
            first_seen[raw.item_id] = write_history(session, raw, shape, first_day_ago, now)
            statuses[raw.item_id] = shape.steps[-1][1]
            by_shape.setdefault(shape_name, []).append((raw.item_id, first_day_ago))

        item_ids = [raw.item_id for raw, _, _ in plan]
        detail_seller_id = fixtures[-1].seller_id
        rules = seed_monitors(session, detail_seller_id, now)
        ledgers = {
            "big": item_ids,
            "small": item_ids[1:4],
            "failing": item_ids[:2],
            "seller": [raw.item_id for raw, _, _ in plan if raw.seller_id == detail_seller_id],
            "empty": [],
        }
        seed_ledger(session, rules, ledgers, first_seen, statuses, now)
        watch_entries = seed_watchlist(session, by_shape, now)
        seed_runs(session, rules, ledgers, watch_entries, now)
        seed_notify(session, rules, now)

        counts = {
            "listings": len(session.exec(select(Item)).all()),
            "sellers": len(session.exec(select(Seller)).all()),
            "snapshots": len(session.exec(select(PriceSnapshot)).all()),
            "hits": len(session.exec(select(MonitorHit)).all()),
            "runs": len(session.exec(select(CollectRun)).all()),
            "watchlist": len(session.exec(select(Watchlist)).all()),
            "pushes": len(session.exec(select(NotifyLog)).all()),
        }
        rule_lines = [f"  id={rules[key].id:<3} {rules[key].name}" for key in rules]

    print(f"data dir: {settings.data_dir.resolve()}  (db: {settings.db_path})")
    print(
        "seeded "
        + ", ".join(f"{value} {name}" for name, value in counts.items())
        + f" over a {WINDOW_DAYS}-day window"
    )
    print(
        f"listings: {len(fixtures)} from tests/fixtures"
        + (f" + {len(pool)} copied from {source}" if pool else "")
    )
    if not pool:
        print(
            "  no listing pool: the four fixture listings are all there is, so the "
            "ledger is small\n  and the watchlist has fewer than six entries. "
            "Pass --from-db <an already-collected app.db>."
        )
    print("rules (ALL DISABLED, so the scheduler never calls upstream):")
    for line in rule_lines:
        print(line)
    print("watchlist price watching is off on every entry, for the same reason")
    print("prices, titles, sellers, regions and images are REAL captures.")
    print(
        "SYNTHESISED from each listing's real current price: the price series, the sold\n"
        "status, the collect runs, the watchlist timestamps and the push log. Channel\n"
        "configs are invented and point at .invalid hosts."
    )
    print(
        f"chart edge cases: no run on days -{sorted(NO_RUN_DAYS)}, "
        f"failed-only on days -{sorted(FAILED_ONLY_DAYS)} (both inside the window)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
