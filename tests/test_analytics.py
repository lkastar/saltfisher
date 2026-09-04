"""Market analytics: the arithmetic, and the sample it is taken over.

Wrong math here does not crash — it produces a plausible chart, which is worse
than a crash because nobody goes looking. So these tests are mostly about the
sample: which listings are counted, how many times each one counts, and which
days are honestly unknown rather than zero.
"""

from datetime import timedelta

import pytest

from app import analytics
from app.models import CollectRun, Item, Monitor, MonitorHit, PriceSnapshot, Seller, utcnow

NOW = utcnow()
KW = "iPhone 15 128G"


def seed_monitor(session, keyword: str = KW, interval: int = 300) -> int:
    monitor = Monitor(name=f"rule {keyword}", keyword=keyword, interval_seconds=interval)
    session.add(monitor)
    session.commit()
    assert monitor.id is not None
    return monitor.id


def seed_item(
    session,
    item_id: str,
    monitor_id: int,
    *,
    prices: list[int],
    first_hit_at=None,
    last_seen_at=None,
    snapshot_times: list | None = None,
    status: str = "on_sale",
    in_range: bool = True,
) -> None:
    """One listing with a price history, wired into a rule's ledger.

    `prices` is oldest-first. Snapshot timestamps default to one hour apart
    ending at NOW, because "the newest snapshot" is defined by captured_at and
    a series that all shares one timestamp cannot exercise that.
    """
    session.add(Seller(id=f"s{item_id}", nick="老王"))
    session.commit()
    session.add(
        Item(
            id=item_id,
            title=f"{KW} {item_id}",
            seller_id=f"s{item_id}",
            seller_nick="老王",
            first_seen_at=first_hit_at or NOW,
            last_seen_at=last_seen_at or NOW,
            status=status,
        )
    )
    times = snapshot_times or [
        NOW - timedelta(hours=len(prices) - 1 - i) for i in range(len(prices))
    ]
    for price, captured_at in zip(prices, times, strict=True):
        session.add(
            PriceSnapshot(
                item_id=item_id,
                price_cents=price,
                status=status,
                source="mtop",
                captured_at=captured_at,
            )
        )
    session.add(
        MonitorHit(
            monitor_id=monitor_id,
            item_id=item_id,
            first_hit_at=first_hit_at or NOW,
            in_range=in_range,
        )
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def test_an_unknown_keyword_is_empty_not_an_error(session):
    """A keyword with no rule is an ordinary state (a deleted rule, a typo in a
    shared link). Every metric returns a complete, zeroed shape so the frontend
    renders "no data" instead of handling a special case.
    """
    for result in (
        analytics.price_distribution(session, "nothing here", now=NOW),
        analytics.price_drops(session, "nothing here", now=NOW),
        analytics.supply_trend(session, "nothing here", now=NOW),
    ):
        assert result["sample_size"] == 0
        assert result["data_days"] == 0


def test_the_scope_includes_listings_the_rule_filtered_out(session):
    """The search is by keyword alone; the rule's price range is applied after.

    So the ledger holds everything the keyword returned, and counting only the
    in-range rows would report "the market" as whatever fits the user's budget
    — a distribution truncated exactly where it matters. Measured on the real
    database: 292 in range, 39 out, all 331 in the ledger.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "in", monitor_id, prices=[300000], in_range=True)
    seed_item(session, "out", monitor_id, prices=[900000], in_range=False)

    result = analytics.price_distribution(session, KW, now=NOW)
    assert result["sample_size"] == 2


def test_two_rules_on_one_keyword_pool_their_ledgers(session):
    first = seed_monitor(session)
    second = seed_monitor(session)
    seed_item(session, "a", first, prices=[300000])
    seed_item(session, "b", second, prices=[310000])

    assert analytics.price_distribution(session, KW, now=NOW)["sample_size"] == 2


def test_freshness_follows_the_slowest_rule_on_the_keyword(session):
    """The cutoff is two cycles of the LONGEST interval among the keyword's
    rules. Taking the shortest would mark a listing delisted while the rule
    that actually watches it has not come round yet.
    """
    seed_monitor(session, interval=300)
    seed_monitor(session, interval=1800)
    scope = analytics.keyword_scope(session, KW)

    assert scope.max_interval_seconds == 1800
    assert analytics.fresh_cutoff(scope, NOW) == NOW - timedelta(seconds=3600)


# --------------------------------------------------------------------------- #
# Price distribution
# --------------------------------------------------------------------------- #


def test_a_listing_counts_once_however_often_it_is_repriced(session):
    """The trap this metric exists to avoid.

    Taking quantiles over snapshot ROWS weights a seller by how much they
    fiddle with the price: the listing below would contribute five samples and
    drag the median toward whoever re-prices most. One sample per listing, from
    its newest snapshot.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "fiddler", monitor_id, prices=[500000, 480000, 460000, 440000, 420000])
    seed_item(session, "steady", monitor_id, prices=[300000])

    result = analytics.price_distribution(session, KW, now=NOW)
    assert result["sample_size"] == 2
    counted = sum(bucket["count"] for bucket in result["histogram"])
    assert counted == 2
    # The newest snapshot, not the first and not the cheapest.
    assert result["histogram"][-1]["lo_cents"] <= 420000 < result["histogram"][-1]["hi_cents"]


def test_a_stale_listing_leaves_the_window(session):
    monitor_id = seed_monitor(session)
    seed_item(session, "recent", monitor_id, prices=[300000], last_seen_at=NOW)
    seed_item(
        session, "ancient", monitor_id, prices=[310000], last_seen_at=NOW - timedelta(days=30)
    )

    assert analytics.price_distribution(session, KW, days=7, now=NOW)["sample_size"] == 1
    assert analytics.price_distribution(session, KW, days=60, now=NOW)["sample_size"] == 2


def test_fresh_size_separates_still_listed_from_merely_in_window(session):
    """Both numbers are needed. `sample_size` says how much the distribution is
    computed over; `fresh_size` says how much of it is still on sale right now.
    Reporting only the first invites reading four-day-old asking prices as the
    current market.
    """
    monitor_id = seed_monitor(session, interval=300)
    seed_item(session, "live", monitor_id, prices=[300000], last_seen_at=NOW)
    seed_item(session, "cold", monitor_id, prices=[310000], last_seen_at=NOW - timedelta(hours=6))

    result = analytics.price_distribution(session, KW, days=7, now=NOW)
    assert result["sample_size"] == 2
    assert result["fresh_size"] == 1


def test_a_listing_observed_as_gone_is_not_an_asking_price(session):
    """Item.status is uninformative for keyword rules -- it is `on_sale` for
    all of them. But when a detail fetch DID observe a removal, that is real,
    and a removed listing's last price is not an offer any more.

    The two rows are stamped SEPARATELY on purpose. `seed_item` writes the
    same status to both `Item` and every `PriceSnapshot`, so a test that used
    it would pass whichever column the query filtered on and prove nothing
    about which one the code reads. Forcing them apart -- `Item` still says
    on sale, the last observation says removed -- makes this test fail if the
    filter is ever moved to `Item.status`.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "live", monitor_id, prices=[300000])
    seed_item(session, "gone", monitor_id, prices=[250000], status="removed")

    assert analytics.price_distribution(session, KW, now=NOW)["sample_size"] == 1

    # Now the disagreeing case: Item says on_sale, the newest snapshot says
    # removed. The snapshot is the observation, so the listing is excluded.
    stale = session.get(Item, "gone")
    assert stale is not None
    stale.status = "on_sale"
    session.add(stale)
    session.commit()

    assert analytics.price_distribution(session, KW, now=NOW)["sample_size"] == 1


def test_quantiles_are_absent_rather_than_fatal_below_two_samples(session):
    """`statistics.quantiles` raises under two data points, and a keyword with
    one listing is a completely ordinary day-one state for this tool.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "only", monitor_id, prices=[300000])

    result = analytics.price_distribution(session, KW, now=NOW)
    assert result["sample_size"] == 1
    assert result["quantiles"] == {}
    assert sum(b["count"] for b in result["histogram"]) == 1


def test_the_quantiles_are_the_percentiles_they_claim_to_be(session):
    monitor_id = seed_monitor(session)
    for i in range(1, 101):
        seed_item(session, f"i{i}", monitor_id, prices=[i * 100])

    q = analytics.price_distribution(session, KW, now=NOW)["quantiles"]
    assert q["p50"] == pytest.approx(5050, abs=100)
    assert q["p10"] < q["p25"] < q["p50"] < q["p75"] < q["p90"]


def test_no_quantile_lands_outside_the_prices_that_produced_it(session):
    """The trap in `statistics.quantiles`: its DEFAULT method extrapolates.

    `method="exclusive"` treats the sample as drawn from a wider population,
    so with fewer than 19 data points it projects past the observed range --
    two listings at 1000 and 9000 yuan give a p10 of minus 4600 yuan and a p90
    of 14600. A negative asking price is not a plausible-but-wrong chart, it is
    an impossible one, and small samples are the normal case for this tool.

    Two samples and a wide spread is the worst case, so it is the one asserted;
    the loop covers every size where the clamp inside `quantiles` can fire.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "cheap", monitor_id, prices=[100000])
    seed_item(session, "dear", monitor_id, prices=[900000])

    q = analytics.price_distribution(session, KW, now=NOW)["quantiles"]
    assert min(q.values()) >= 100000, q
    assert max(q.values()) <= 900000, q

    # Every sample size below 19 clamps inside statistics.quantiles; walk them
    # all rather than trusting that two is the only broken one.
    for size in range(2, 20):
        prices = [100000] + [500000] * (size - 2) + [900000]
        assert len(prices) == size
        cuts = analytics._quantiles(prices)
        assert min(cuts.values()) >= 100000, (size, cuts)
        assert max(cuts.values()) <= 900000, (size, cuts)


def test_histogram_buckets_are_whole_yuan_and_cover_every_sample(session):
    monitor_id = seed_monitor(session)
    prices = [241300, 268700, 300000, 315500, 402100]
    for i, price in enumerate(prices):
        seed_item(session, f"h{i}", monitor_id, prices=[price])

    histogram = analytics.price_distribution(session, KW, now=NOW)["histogram"]
    assert sum(b["count"] for b in histogram) == len(prices)
    for bucket in histogram:
        assert bucket["lo_cents"] % 100 == 0
        assert bucket["hi_cents"] % 100 == 0
    assert histogram[0]["lo_cents"] <= min(prices)
    assert histogram[-1]["hi_cents"] > max(prices)


# --------------------------------------------------------------------------- #
# Price drops
# --------------------------------------------------------------------------- #


def test_a_drop_is_measured_against_the_price_at_the_window_start(session):
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "dropped",
        monitor_id,
        prices=[400000, 300000],
        first_hit_at=NOW - timedelta(days=30),
        snapshot_times=[NOW - timedelta(days=30), NOW - timedelta(hours=1)],
    )

    result = analytics.price_drops(session, KW, days=7, now=NOW)
    (row,) = result["rows"]
    assert row["then_cents"] == 400000
    assert row["now_cents"] == 300000
    assert row["drop_bps"] == 2500  # 25.00%
    assert row["is_fresh"] is True


def test_a_listing_that_arrived_inside_the_window_has_no_drop_to_report(session):
    """It has no price from before the window, so there is nothing to compare
    against -- and reporting its arrival as a drop is the noise this exclusion
    exists to remove.
    """
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "newcomer",
        monitor_id,
        prices=[400000, 300000],
        first_hit_at=NOW - timedelta(hours=3),
        snapshot_times=[NOW - timedelta(hours=3), NOW - timedelta(hours=1)],
    )

    assert analytics.price_drops(session, KW, days=7, now=NOW)["sample_size"] == 0


def test_a_rise_is_not_a_drop(session):
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "risen",
        monitor_id,
        prices=[300000, 400000],
        snapshot_times=[NOW - timedelta(days=30), NOW - timedelta(hours=1)],
    )

    assert analytics.price_drops(session, KW, days=7, now=NOW)["sample_size"] == 0


def test_the_ranking_is_ordered_by_depth_and_capped(session):
    monitor_id = seed_monitor(session)
    old = NOW - timedelta(days=30)
    for i, now_cents in enumerate([390000, 200000, 350000]):
        seed_item(
            session,
            f"d{i}",
            monitor_id,
            prices=[400000, now_cents],
            snapshot_times=[old, NOW - timedelta(hours=1)],
        )

    result = analytics.price_drops(session, KW, days=7, limit=2, now=NOW)
    assert result["sample_size"] == 3  # every drop counted...
    assert len(result["rows"]) == 2  # ...even though only two are returned
    assert [r["drop_bps"] for r in result["rows"]] == [5000, 1250]


def test_a_sold_out_listing_is_ranked_but_flagged(session):
    """It stays in the ranking -- a recent sale price is a reference point --
    but `is_fresh` false is what stops the UI sending someone to a dead page.
    """
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "sold",
        monitor_id,
        prices=[400000, 300000],
        snapshot_times=[NOW - timedelta(days=30), NOW - timedelta(hours=1)],
        status="removed",
    )

    (row,) = analytics.price_drops(session, KW, days=7, now=NOW)["rows"]
    assert row["drop_bps"] == 2500
    assert row["is_fresh"] is False


def test_drop_depth_is_integer_basis_points(session):
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "third",
        monitor_id,
        prices=[300000, 200000],
        snapshot_times=[NOW - timedelta(days=30), NOW - timedelta(hours=1)],
    )

    (row,) = analytics.price_drops(session, KW, days=7, now=NOW)["rows"]
    assert isinstance(row["drop_bps"], int)
    assert row["drop_bps"] == 3333  # truncated, never a float ratio


# --------------------------------------------------------------------------- #
# Supply trend
# --------------------------------------------------------------------------- #


def test_a_day_without_a_successful_run_is_not_a_zero(session):
    """The whole reason CollectRun exists.

    Both days below have zero new listings. One was collected and the market
    was quiet; the other has no run at all. Drawing them the same way tells
    the user supply dried up when the process was actually down -- which is
    what this project's own database looked like for 2026-09-01 to 09-03.
    """
    monitor_id = seed_monitor(session)
    quiet = NOW - timedelta(days=1)
    session.add(CollectRun(monitor_id=monitor_id, started_at=quiet, ok=True, item_count=30))
    session.commit()

    days = {d["date"]: d for d in analytics.supply_trend(session, KW, days=3, now=NOW)["days"]}
    assert days[quiet.date().isoformat()]["collected"] is True
    assert days[quiet.date().isoformat()]["new_count"] == 0
    assert days[(NOW - timedelta(days=2)).date().isoformat()]["collected"] is False


def test_failed_runs_alone_do_not_count_as_collected(session):
    monitor_id = seed_monitor(session)
    session.add(CollectRun(monitor_id=monitor_id, started_at=NOW, ok=False, error="rate limited"))
    session.add(CollectRun(monitor_id=monitor_id, started_at=NOW, ok=False, error="rate limited"))
    session.commit()

    today = analytics.supply_trend(session, KW, days=1, now=NOW)["days"][0]
    assert today["collected"] is False
    assert today["runs_ok"] == 0
    assert today["runs_failed"] == 2


def test_every_day_in_the_window_appears_even_with_no_records(session):
    seed_monitor(session)
    series = analytics.supply_trend(session, KW, days=14, now=NOW)["days"]

    assert len(series) == 14
    assert series[0]["date"] < series[-1]["date"]
    assert series[-1]["date"] == NOW.date().isoformat()
    assert all(day["collected"] is False for day in series)


def test_new_listings_are_dated_per_keyword_not_globally(session):
    """Item.first_seen_at is global, so a listing two keywords both saw would
    be dated by whichever searched first -- and land on the wrong day for the
    other. Grouping by MonitorHit.first_hit_at gives each keyword its own
    first sighting.
    """
    broad = seed_monitor(session, keyword="iPhone 15")
    narrow = seed_monitor(session, keyword=KW)
    long_ago = NOW - timedelta(days=5)

    # Seen by the broad rule five days ago; the narrow rule only found it today.
    seed_item(session, "shared", broad, prices=[300000], first_hit_at=long_ago)
    session.add(MonitorHit(monitor_id=narrow, item_id="shared", first_hit_at=NOW, in_range=True))
    session.commit()

    broad_days = {
        d["date"]: d["new_count"]
        for d in analytics.supply_trend(session, "iPhone 15", days=7, now=NOW)["days"]
    }
    narrow_days = {
        d["date"]: d["new_count"]
        for d in analytics.supply_trend(session, KW, days=7, now=NOW)["days"]
    }

    assert broad_days[long_ago.date().isoformat()] == 1
    assert broad_days[NOW.date().isoformat()] == 0
    assert narrow_days[NOW.date().isoformat()] == 1
    assert narrow_days[long_ago.date().isoformat()] == 0


def test_a_listing_seen_repeatedly_is_new_only_once(session):
    monitor_id = seed_monitor(session)
    seed_item(session, "a", monitor_id, prices=[300000, 290000, 280000])

    today = analytics.supply_trend(session, KW, days=1, now=NOW)["days"][0]
    assert today["new_count"] == 1


def test_another_rules_runs_do_not_count_toward_this_keyword(session):
    """Coverage is per keyword. A different rule collecting successfully says
    nothing about whether THIS keyword was searched that day.
    """
    seed_monitor(session, keyword=KW)
    other = seed_monitor(session, keyword="索尼 a7c2")
    session.add(CollectRun(monitor_id=other, started_at=NOW, ok=True, item_count=30))
    session.commit()

    today = analytics.supply_trend(session, KW, days=1, now=NOW)["days"][0]
    assert today["collected"] is False


# --------------------------------------------------------------------------- #
# data_days
# --------------------------------------------------------------------------- #


def test_data_days_counts_from_the_first_sighting_inclusive(session):
    monitor_id = seed_monitor(session)
    seed_item(session, "old", monitor_id, prices=[300000], first_hit_at=NOW - timedelta(days=3))

    assert analytics.price_distribution(session, KW, now=NOW)["data_days"] == 4


def test_a_rule_that_never_ran_has_no_history(session):
    seed_monitor(session)
    assert analytics.supply_trend(session, KW, now=NOW)["data_days"] == 0


# --------------------------------------------------------------------------- #
# Layer boundary
# --------------------------------------------------------------------------- #


def test_analytics_does_not_reach_into_the_http_layer():
    """spec/backend/error-handling.md: HTTPException may only be raised in
    api/. An analytics module that raises it cannot be reused by anything but
    a route -- and this one is also read by the LLM market scenario in M4.
    """
    source = (analytics.__file__ or "").replace("\\", "/")
    assert source
    with open(analytics.__file__ or "", encoding="utf-8") as handle:
        text = handle.read()
    assert "fastapi" not in text
    assert "HTTPException" not in text
