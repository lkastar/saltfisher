"""Market analytics over the collected history.

Three metrics, all read-only, all returning plain dicts. Every response
carries `data_days` and `sample_size`, and that is not decoration: this tool
starts with an empty database, so a chart has to be able to say "17 listings
over 4 days" rather than draw a confident line over almost nothing.

Two facts about the data shape drive most of the code here, and both were
measured on the real database rather than assumed:

**"On sale" cannot be read from `Item.status`.** Only a watched item's detail
fetch ever sets it to anything but `on_sale` (`store.persist_watch_observation`);
a keyword-monitored listing that drops out of the search results is never
touched. On the development database as of 2026-09-05, **0 of 328** items had a
status other than `on_sale`. So freshness of `last_seen_at` is the test, and
`status` is used only in the direction where it does carry information: a
listing we actually observed as `sold`/`removed` is excluded outright.

**A keyword is not a product category.** Scope is resolved through
`MonitorHit`, since `Item` has no keyword. Measured overlap on 2026-09-05:
`iPhone 15` saw 264 listings, `iPhone 15 128G` saw 59, and only 29 were shared
— the narrow keyword found 30 the broad one never did. The counts grow with
every cycle; the point is the ratio. Every figure here is therefore "what the
searches for this keyword saw", which the UI must say out loud instead of
implying it is the market.

No pandas: these are three `GROUP BY`s and one call to `statistics.quantiles`
over a few hundred values. See the task's design.md for that deviation.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models import CollectRun, Item, Monitor, MonitorHit, PriceSnapshot, utcnow
from app.store import snapshot_ids_at

# A listing counts as still on sale if we saw it within this many poll cycles.
# Two, not one: a cycle can be skipped by backoff or by a failed fetch, and one
# missed cycle must not read as "delisted". It is a knob because the right
# value depends on how reliable collection actually is in a given deployment,
# which no amount of code can know.
#
# ponytail: a module constant, not a setting. Promote it to Settings the day
# someone actually needs to tune it without editing code.
FRESH_MULTIPLIER = 2

# Used when a keyword has no rule left (a deleted rule whose ledger survives),
# so freshness still has a defined meaning instead of dividing by nothing.
FALLBACK_INTERVAL_SECONDS = 300

MIN_BUCKETS = 5
MAX_BUCKETS = 20
CENTS_PER_YUAN = 100


@dataclass(frozen=True, slots=True)
class Scope:
    """Which rules and how much history one keyword covers.

    Item ids are deliberately NOT materialised here. Resolving them to a
    Python list would put a few hundred bind parameters into every downstream
    query and hit SQLite's variable limit on a busy keyword; every metric
    below instead nests the ledger as a subquery, which also keeps each
    endpoint's query count independent of how many listings it covers.
    """

    keyword: str
    monitor_ids: tuple[int, ...]
    max_interval_seconds: int
    earliest_hit_at: datetime | None

    @property
    def known(self) -> bool:
        return bool(self.monitor_ids)


def keyword_scope(session: Session, keyword: str) -> Scope:
    """Resolve a keyword to its rules and the age of its history."""
    rows = session.exec(
        select(col(Monitor.id), col(Monitor.interval_seconds)).where(Monitor.keyword == keyword)
    ).all()
    monitor_ids = tuple(int(row[0]) for row in rows if row[0] is not None)
    intervals = [int(row[1]) for row in rows] or [FALLBACK_INTERVAL_SECONDS]

    earliest: datetime | None = None
    if monitor_ids:
        earliest = session.exec(
            select(func.min(col(MonitorHit.first_hit_at))).where(
                col(MonitorHit.monitor_id).in_(monitor_ids)
            )
        ).one()

    return Scope(
        keyword=keyword,
        monitor_ids=monitor_ids,
        max_interval_seconds=max(intervals),
        earliest_hit_at=earliest,
    )


def fresh_cutoff(scope: Scope, now: datetime) -> datetime:
    return now - timedelta(seconds=FRESH_MULTIPLIER * scope.max_interval_seconds)


def data_days(scope: Scope, now: datetime) -> int:
    """How many days of history this keyword has, inclusive of today.

    The number behind "collecting for N days" in the UI. It counts from the
    first sighting, not from when the rule was created: a rule created and
    then left disabled has no history to plot.
    """
    if scope.earliest_hit_at is None:
        return 0
    return max(0, (now - scope.earliest_hit_at).days) + 1


def _ledger_item_ids(scope: Scope):
    """Every item any rule for this keyword has ever seen, as a subquery.

    Includes rows with `in_range = 0`. That is what makes the price
    distribution the real market rather than "the part of it inside my budget"
    — the search is by keyword only, so the ledger holds everything the
    keyword returned, and the rule's filters were applied afterwards.
    """
    return select(col(MonitorHit.item_id)).where(col(MonitorHit.monitor_id).in_(scope.monitor_ids))


def _quantiles(prices: list[int]) -> dict[str, int]:
    """p10/p25/p50/p75/p90 in cents, or empty below two samples.

    `statistics.quantiles(n=20)` returns the 19 five-percent cut points, so the
    percentiles wanted here are indexes 1, 4, 9, 14 and 17. It raises below two
    data points, and a one-listing keyword is a completely ordinary state for
    this tool — so that case returns nothing to plot rather than a 500.

    `method="inclusive"` is load-bearing, not a default worth leaving alone.
    The default `"exclusive"` treats the sample as drawn from a wider
    population and EXTRAPOLATES past the observed range whenever there are
    fewer than 19 data points — two listings at ¥1000 and ¥9000 come back with
    a p10 of MINUS ¥4600 and a p90 of ¥14600, neither of which any listing has
    and one of which is not a price at all. Small samples are the normal case
    here, so that is a chart the tool would draw most days of its first week.
    The inclusive method interpolates between the observed order statistics and
    can never leave [min, max], which is also the honest claim for this data:
    these are the listings our searches saw, not a sample of the market (see
    the module docstring).
    """
    if len(prices) < 2:
        return {}
    cuts = statistics.quantiles(prices, n=20, method="inclusive")
    return {
        "p10": round(cuts[1]),
        "p25": round(cuts[4]),
        "p50": round(cuts[9]),
        "p75": round(cuts[14]),
        "p90": round(cuts[17]),
    }


def _histogram(prices: list[int]) -> list[dict[str, int]]:
    """Equal-width buckets aligned to whole yuan.

    Bucket count follows sqrt(n) between 5 and 20: too few buckets hide the
    shape a price distribution is drawn for, too many turn a 30-listing sample
    into a comb of ones. Edges are whole yuan because a bucket labelled
    "¥2413–¥2687" reads as noise; all arithmetic stays integer for the same
    reason prices are integer cents everywhere else.
    """
    if not prices:
        return []
    lo, hi = min(prices), max(prices)
    start = (lo // CENTS_PER_YUAN) * CENTS_PER_YUAN
    wanted = min(MAX_BUCKETS, max(MIN_BUCKETS, math.isqrt(len(prices))))
    span = hi - start + 1
    width = max(CENTS_PER_YUAN, math.ceil(span / wanted / CENTS_PER_YUAN) * CENTS_PER_YUAN)
    count = math.ceil(span / width)

    counts = [0] * count
    for price in prices:
        # ponytail: the min() is provably unreachable, kept as a guard. With
        # span = hi - start + 1 and count = ceil(span / width), the largest
        # index any price can produce is (span - 1) // width, which equals
        # count - 1 in both the exact-multiple and the remainder case. Drop it
        # only alongside a test that pins the edge arithmetic.
        counts[min((price - start) // width, count - 1)] += 1
    return [
        {"lo_cents": start + i * width, "hi_cents": start + (i + 1) * width, "count": c}
        for i, c in enumerate(counts)
    ]


def price_distribution(
    session: Session, keyword: str, *, days: int = 7, now: datetime | None = None
) -> dict:
    """Where this keyword's asking prices sit.

    One sample per LISTING, taken from its newest snapshot — never one per
    snapshot row. A seller who re-prices five times would otherwise be
    weighted five times, which quietly turns the distribution into a map of
    who fiddles with their price most.
    """
    now = now or utcnow()
    scope = keyword_scope(session, keyword)
    empty = {
        "quantiles": {},
        "histogram": [],
        "sample_size": 0,
        "fresh_size": 0,
        "data_days": data_days(scope, now),
        "window_days": days,
    }
    if not scope.known:
        return empty

    newest = snapshot_ids_at(None).subquery()
    rows = session.exec(
        select(col(PriceSnapshot.price_cents), col(Item.last_seen_at))
        .join(newest, col(PriceSnapshot.id) == newest.c.snapshot_id)
        .join(Item, col(Item.id) == col(PriceSnapshot.item_id))
        .where(col(PriceSnapshot.item_id).in_(_ledger_item_ids(scope)))
        .where(col(Item.last_seen_at) >= now - timedelta(days=days))
        # The SNAPSHOT's status, deliberately, not Item.status -- the two are
        # written from the same `raw.status` but only this one is per
        # observation, so it says "what the listing was when we last priced
        # it" rather than "what it is now". Either way it is used in one
        # direction only: a listing actually observed as sold or removed is
        # not an asking price any more. Its `on_sale` value proves nothing
        # (see the module docstring), which is why freshness does the real
        # work above.
        .where(col(PriceSnapshot.status) == "on_sale")
    ).all()
    if not rows:
        return empty

    cutoff = fresh_cutoff(scope, now)
    prices = [int(row[0]) for row in rows]
    return {
        "quantiles": _quantiles(prices),
        "histogram": _histogram(prices),
        "sample_size": len(prices),
        "fresh_size": sum(1 for row in rows if row[1] >= cutoff),
        "data_days": data_days(scope, now),
        "window_days": days,
    }


def price_drops(
    session: Session,
    keyword: str,
    *,
    days: int = 7,
    limit: int = 20,
    now: datetime | None = None,
) -> dict:
    """Listings that have come down in price over the window.

    Measured as "priced at X back then, priced at Y now", where "back then" is
    the newest snapshot at or before the window start. A listing with no
    snapshot that old is excluded automatically, which is exactly the intended
    behaviour: something that first appeared inside the window has no drop to
    report, only the noise of arriving.

    Three queries, joined in Python rather than one four-way SQL join through
    two aliases of the same table. The row count is in the low hundreds and
    the query count still does not follow it, so the join buys nothing that
    the readability costs.
    """
    now = now or utcnow()
    scope = keyword_scope(session, keyword)
    result: dict = {
        "rows": [],
        "sample_size": 0,
        "data_days": data_days(scope, now),
        "window_days": days,
    }
    if not scope.known:
        return result

    def prices_as_of(cutoff: datetime | None) -> dict[str, tuple[int, str, datetime]]:
        marker = snapshot_ids_at(cutoff).subquery()
        rows = session.exec(
            select(
                col(PriceSnapshot.item_id),
                col(PriceSnapshot.price_cents),
                col(PriceSnapshot.status),
                col(PriceSnapshot.captured_at),
            )
            .join(marker, col(PriceSnapshot.id) == marker.c.snapshot_id)
            .where(col(PriceSnapshot.item_id).in_(_ledger_item_ids(scope)))
        ).all()
        return {row[0]: (int(row[1]), row[2], row[3]) for row in rows}

    before = prices_as_of(now - timedelta(days=days))
    current = prices_as_of(None)

    cutoff = fresh_cutoff(scope, now)
    drops: list[dict] = []
    for item_id, (then_cents, _, _) in before.items():
        latest = current.get(item_id)
        if latest is None or then_cents <= 0:
            continue
        now_cents = latest[0]
        if now_cents >= then_cents:
            continue
        drops.append(
            {
                "item_id": item_id,
                "then_cents": then_cents,
                "now_cents": now_cents,
                # Integer basis points, not a float ratio: the same reason
                # prices are integer cents. Formatting is the UI's job.
                "drop_bps": (then_cents - now_cents) * 10000 // then_cents,
                "status": latest[1],
            }
        )

    # Item id breaks ties so the ranking is a total order -- equal drops must
    # not swap places between two requests for the same data.
    drops.sort(key=lambda d: (-d["drop_bps"], d["item_id"]))
    result["sample_size"] = len(drops)

    top = drops[:limit]
    if not top:
        return result

    meta = {
        row[0]: row
        for row in session.exec(
            select(
                col(Item.id), col(Item.title), col(Item.cover_url), col(Item.last_seen_at)
            ).where(col(Item.id).in_([d["item_id"] for d in top]))
        ).all()
    }
    for drop in top:
        row = meta.get(drop["item_id"])
        status = drop.pop("status")
        last_seen = row[3] if row else None
        drop["title"] = row[1] if row else drop["item_id"]
        drop["cover_url"] = row[2] if row else None
        drop["last_seen_at"] = last_seen
        # "Still worth clicking": recently seen AND never observed as gone. A
        # ranking that silently lists sold-out listings sends the user to a
        # dead page with no warning.
        drop["is_fresh"] = last_seen is not None and last_seen >= cutoff and status == "on_sale"

    result["rows"] = top
    return result


def supply_trend(
    session: Session, keyword: str, *, days: int = 30, now: datetime | None = None
) -> dict:
    """New listings per day, with days we did not collect marked as such.

    Three states per day, not two. "No new listings" and "we never ran" are
    different facts, and drawing both as a zero bar is how a chart tells the
    user the market went quiet when really the process was down. That is the
    entire reason CollectRun exists — on this project's own database, three
    consecutive days of downtime were indistinguishable from three dead market
    days until it did.

    Days are grouped by `MonitorHit.first_hit_at`, not `Item.first_seen_at`:
    the latter is global, so a listing two keywords both saw would be dated by
    whichever searched first and land on the wrong day for the other.

    Day boundaries are UTC, because that is what the columns hold. Converting
    for display is the frontend's job.
    """
    now = now or utcnow()
    scope = keyword_scope(session, keyword)
    start = (now - timedelta(days=days - 1)).date() if days > 0 else now.date()
    result: dict = {
        "days": [],
        "sample_size": 0,
        "data_days": data_days(scope, now),
        "window_days": days,
    }
    if not scope.known:
        return result

    first_hit_day = func.date(col(MonitorHit.first_hit_at))
    new_by_day = dict(
        session.exec(
            select(first_hit_day, func.count(func.distinct(col(MonitorHit.item_id))))
            .where(col(MonitorHit.monitor_id).in_(scope.monitor_ids))
            .where(first_hit_day >= start.isoformat())
            .group_by(first_hit_day)
        ).all()
    )

    run_day = func.date(col(CollectRun.started_at))
    runs_by_day: dict[str, tuple[int, int]] = {
        row[0]: (int(row[1]), int(row[2]))
        for row in session.exec(
            select(
                run_day,
                func.sum(func.iif(col(CollectRun.ok), 1, 0)),
                func.sum(func.iif(col(CollectRun.ok), 0, 1)),
            )
            .where(col(CollectRun.monitor_id).in_(scope.monitor_ids))
            .where(run_day >= start.isoformat())
            .group_by(run_day)
        ).all()
    }

    series: list[dict] = []
    total_new = 0
    # `days >= 1` is enforced by the route's Query(ge=1), and `start` above
    # already handles the non-positive case, so this range needs no second
    # guard of its own.
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        ok_count, fail_count = runs_by_day.get(day, (0, 0))
        new_count = int(new_by_day.get(day, 0))
        total_new += new_count
        series.append(
            {
                "date": day,
                "new_count": new_count,
                # A day with no successful run has nothing to say about supply,
                # whatever new_count happens to be -- and before this table
                # existed, EVERY historical day looks like this. That is
                # accurate: there is no record that we collected then.
                "collected": ok_count > 0,
                "runs_ok": ok_count,
                "runs_failed": fail_count,
            }
        )

    result["days"] = series
    result["sample_size"] = total_new
    return result
