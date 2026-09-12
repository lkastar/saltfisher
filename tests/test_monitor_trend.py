"""The rule-level price trend: which listings count on which day.

The arithmetic here is a mean and two quantiles, and it is not where the bugs
are. The bugs are in the sample: `PriceSnapshot` is append-on-change, so "the
listings observed today" and "the listings on sale today" are different sets,
and a chart built from the first one moves when repricing activity moves rather
than when the market does. Every test below pins a membership rule, not a
number.
"""

from datetime import datetime, timedelta

from app import analytics
from app.models import CollectRun, Item, Monitor, MonitorHit, PriceSnapshot, Seller, utcnow

# Midday, so a +/- few hours in a test never silently crosses a UTC day edge
# and turns a membership assertion into a calendar assertion.
NOW = datetime.combine(utcnow().date(), datetime.min.time()) + timedelta(hours=12)


def seed_monitor(session, name: str = "rule") -> int:
    monitor = Monitor(name=name, keyword="iPhone 15 128G", interval_seconds=300)
    session.add(monitor)
    session.commit()
    assert monitor.id is not None
    return monitor.id


def seed_listing(
    session,
    item_id: str,
    monitor_id: int,
    *,
    observations: list[tuple[datetime, int, str]],
) -> None:
    """One listing whose snapshots are given explicitly as (when, price, status).

    Explicit rather than generated: every test here is about which day an
    observation lands on, so the timestamps are the subject and must be visible
    in the test that depends on them.
    """
    session.add(Seller(id=f"s{item_id}", nick="老王"))
    first = observations[0][0]
    session.add(
        Item(
            id=item_id,
            title=f"listing {item_id}",
            seller_id=f"s{item_id}",
            seller_nick="老王",
            first_seen_at=first,
            last_seen_at=observations[-1][0],
            status=observations[-1][2],
        )
    )
    for captured_at, price, status in observations:
        session.add(
            PriceSnapshot(
                item_id=item_id,
                price_cents=price,
                status=status,
                source="mtop",
                captured_at=captured_at,
            )
        )
    session.add(MonitorHit(monitor_id=monitor_id, item_id=item_id, first_hit_at=first))
    session.commit()


def seed_runs(session, monitor_id: int, *, days_ago: list[int], ok: bool = True) -> None:
    for offset in days_ago:
        session.add(
            CollectRun(
                monitor_id=monitor_id,
                started_at=NOW - timedelta(days=offset),
                finished_at=NOW - timedelta(days=offset),
                ok=ok,
                collected=1,
            )
        )
    session.commit()


def day(result: dict, days_ago: int) -> dict:
    """The row for `days_ago` days before NOW, by date rather than by index."""
    wanted = (NOW - timedelta(days=days_ago)).date().isoformat()
    match = [d for d in result["days"] if d["date"] == wanted]
    assert match, f"{wanted} missing from {[d['date'] for d in result['days']]}"
    return match[0]


def test_a_listing_that_did_not_move_still_counts_at_its_last_price(session):
    """The reason this aggregate carries forward at all.

    Snapshots are written only on change, so a listing priced once four days
    ago has no row for the three days since. Counting only same-day snapshots
    would drop it from those days -- and with it, every stable listing, leaving
    the mean to be computed over whichever listings happened to be repriced.
    """
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "stable",
        monitor,
        observations=[(NOW - timedelta(days=4), 400_000, "on_sale")],
    )

    result = analytics.monitor_trend(session, monitor, days=7, now=NOW)

    for days_ago in (4, 3, 2, 1, 0):
        assert day(result, days_ago)["mean_cents"] == 400_000
        assert day(result, days_ago)["listing_count"] == 1


def test_a_listing_does_not_exist_before_we_first_saw_it(session):
    """No back-projection. A price observed today says nothing about last week,
    and filling it backwards invents a flat history the tool never measured.
    """
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "new",
        monitor,
        observations=[(NOW - timedelta(days=2), 500_000, "on_sale")],
    )

    result = analytics.monitor_trend(session, monitor, days=7, now=NOW)

    assert day(result, 3)["mean_cents"] is None
    assert day(result, 3)["listing_count"] == 0
    assert day(result, 2)["mean_cents"] == 500_000


def test_a_sold_listing_leaves_the_average_from_that_day_on(session):
    """A sold listing is a fact about the past. Left in, it pins the mean to a
    price nobody can pay any more -- and the cheap ones sell first, so it drags
    the line down exactly when the market moved up.
    """
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "sold-one",
        monitor,
        observations=[
            (NOW - timedelta(days=5), 200_000, "on_sale"),
            (NOW - timedelta(days=2), 200_000, "sold"),
        ],
    )
    seed_listing(
        session,
        "still-up",
        monitor,
        observations=[(NOW - timedelta(days=5), 400_000, "on_sale")],
    )

    result = analytics.monitor_trend(session, monitor, days=7, now=NOW)

    assert day(result, 3)["listing_count"] == 2
    assert day(result, 3)["mean_cents"] == 300_000
    assert day(result, 2)["listing_count"] == 1
    assert day(result, 2)["mean_cents"] == 400_000


def test_a_day_the_rule_never_ran_is_marked_not_zeroed(session):
    """`collected` is the difference between a quiet market and a dead process.

    The price is still carried forward on such a day -- that is what we last
    observed -- but the flag lets the chart break the line instead of drawing
    over a window where nothing was measured.
    """
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "one",
        monitor,
        observations=[(NOW - timedelta(days=5), 300_000, "on_sale")],
    )
    seed_runs(session, monitor, days_ago=[5, 4, 0])

    result = analytics.monitor_trend(session, monitor, days=7, now=NOW)

    assert day(result, 4)["collected"] is True
    assert day(result, 3)["collected"] is False
    assert day(result, 2)["collected"] is False
    assert day(result, 0)["collected"] is True


def test_a_failed_run_does_not_count_as_collected(session):
    """Same rule as supply_trend: a cycle that errored measured nothing."""
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "one",
        monitor,
        observations=[(NOW - timedelta(days=2), 300_000, "on_sale")],
    )
    seed_runs(session, monitor, days_ago=[1], ok=False)

    result = analytics.monitor_trend(session, monitor, days=4, now=NOW)

    assert day(result, 1)["collected"] is False


def test_one_listing_has_a_mean_but_no_band(session):
    """A band over a single sample would be a line pretending to be a range.

    `_quantiles` returns nothing below two samples, and a one-listing rule is
    an ordinary state for this tool -- the first day of every new rule.
    """
    monitor = seed_monitor(session)
    seed_listing(
        session,
        "only",
        monitor,
        observations=[(NOW - timedelta(days=1), 250_000, "on_sale")],
    )

    result = analytics.monitor_trend(session, monitor, days=3, now=NOW)
    today = day(result, 0)

    assert today["mean_cents"] == 250_000
    assert today["p25_cents"] is None
    assert today["p75_cents"] is None


def test_the_band_never_leaves_the_observed_prices(session):
    """Guards `_quantiles`' inclusive method through this caller too: the
    exclusive default extrapolates past the observed range on small samples,
    which is most days here.
    """
    monitor = seed_monitor(session)
    prices = [100_000, 900_000]
    for i, price in enumerate(prices):
        seed_listing(
            session,
            f"item{i}",
            monitor,
            observations=[(NOW - timedelta(days=1), price, "on_sale")],
        )

    today = day(analytics.monitor_trend(session, monitor, days=3, now=NOW), 0)

    assert min(prices) <= today["p25_cents"] <= today["p75_cents"] <= max(prices)


def test_a_rule_with_no_ledger_returns_a_full_empty_series(session):
    """A rule created and never run is the state every rule starts in. It has
    to render as "no data yet", which needs a complete shape, not a short one.
    """
    monitor = seed_monitor(session)

    result = analytics.monitor_trend(session, monitor, days=30, now=NOW)

    assert result["sample_size"] == 0
    assert len(result["days"]) == 30
    assert all(d["mean_cents"] is None for d in result["days"])
    assert all(d["listing_count"] == 0 for d in result["days"])


def test_another_rules_listings_are_not_counted(session):
    """Scope is the ledger, not the items table. Two rules watching different
    products share an Item table, and mixing them averages unrelated things.
    """
    mine = seed_monitor(session, "mine")
    theirs = seed_monitor(session, "theirs")
    seed_listing(
        session, "mine-1", mine, observations=[(NOW - timedelta(days=1), 300_000, "on_sale")]
    )
    seed_listing(
        session,
        "theirs-1",
        theirs,
        observations=[(NOW - timedelta(days=1), 9_000_000, "on_sale")],
    )

    today = day(analytics.monitor_trend(session, mine, days=3, now=NOW), 0)

    assert today["listing_count"] == 1
    assert today["mean_cents"] == 300_000
