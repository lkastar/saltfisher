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
        analytics.listing_duration(session, "nothing here", now=NOW),
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
# Listing duration
# --------------------------------------------------------------------------- #

# fresh_cutoff at the 300s default is NOW - 600s, so "stale" has to be further
# back than that. An hour is comfortably outside it and still lets a duration
# be read in whole minutes.
STALE = NOW - timedelta(hours=1)


def test_only_listings_that_stopped_coming_back_have_a_duration(session):
    """A listing still in the search results has an age, not a duration.

    Counting it would report every live listing as "left after N hours" the
    moment the chart was drawn, which is the shape of a market measurement
    made out of nothing but how long the tool has been running.
    """
    monitor_id = seed_monitor(session)
    seed_item(session, "live", monitor_id, prices=[300000], first_hit_at=STALE, last_seen_at=NOW)
    seed_item(
        session,
        "gone",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=3),
        last_seen_at=STALE,
    )

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["sample_size"] == 1
    # 3h first hit, last seen 1h ago -> two hours inside our range.
    assert result["quantiles"] == {}, "one sample gives no quantiles"
    assert sum(b["count"] for b in result["histogram"]) == 1
    assert result["histogram"][0]["lo_minutes"] <= 120 < result["histogram"][-1]["hi_minutes"]


def test_a_listing_we_observed_as_gone_still_counts(session):
    """No status filter in either direction.

    `status` is uninformative for keyword listings (0 of 328 differ), but on
    the rare row where it IS set, that listing has the most meaningful end
    time in the set. Excluding it would bias the distribution short.
    """
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "sold",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=5),
        last_seen_at=STALE,
        status="removed",
    )

    assert analytics.listing_duration(session, KW, now=NOW)["sample_size"] == 1


def test_the_duration_is_measured_from_this_keywords_first_sighting(session):
    """Item.first_seen_at is global; MonitorHit.first_hit_at is per keyword.

    Measured on the real database: the two disagree by four full days. Using
    the global one would credit a shared listing to whichever keyword searched
    first and report a duration that keyword never observed.

    Reverse verification: swap first_hit for Item.first_seen_at in
    listing_duration and this assertion goes red -- the narrow keyword's
    single sample reports 5 days instead of 2 hours.
    """
    broad = seed_monitor(session, keyword="iPhone 15")
    narrow = seed_monitor(session, keyword=KW)
    long_ago = NOW - timedelta(days=5)

    # Global first_seen_at is five days back, because the broad rule found it
    # then. The narrow keyword only saw it three hours ago.
    seed_item(session, "shared", broad, prices=[300000], first_hit_at=long_ago, last_seen_at=STALE)
    session.add(
        MonitorHit(
            monitor_id=narrow,
            item_id="shared",
            first_hit_at=NOW - timedelta(hours=3),
            in_range=True,
        )
    )
    session.commit()

    narrow_hist = analytics.listing_duration(session, KW, now=NOW)["histogram"]
    broad_hist = analytics.listing_duration(session, "iPhone 15", now=NOW)["histogram"]

    # Two hours for the keyword that saw it three hours ago and lost it one
    # hour ago; five days for the one that had it all along.
    assert narrow_hist[-1]["hi_minutes"] <= 3 * 60
    assert broad_hist[-1]["hi_minutes"] > 4 * 24 * 60


def test_the_clock_stops_at_this_keywords_last_sighting(session):
    """The mirror of the test above, and the harder half.

    `Item.last_seen_at` is global, so a listing another rule still returns
    keeps a fresh timestamp long after it dropped out of THIS keyword's pages.
    Under the global clock such a listing looks alive and vanishes from the
    sample entirely -- not an inflated duration, an absent one, which is the
    kind of bias nobody notices.

    Measured on the real database: 29 of the 59 listings in the
    `iPhone 15 128G` ledger are also in `iPhone 15`, so 49% of that keyword's
    rows were on a clock a different rule was winding.

    Reverse verification: read `Item.last_seen_at` instead of
    `MonitorHit.last_hit_at` and the narrow keyword reports no samples at all.
    """
    broad = seed_monitor(session, keyword="iPhone 15")
    narrow = seed_monitor(session, keyword=KW)

    # The broad rule saw it a minute ago, so the GLOBAL timestamp is fresh.
    seed_item(session, "shared", broad, prices=[300000], first_hit_at=NOW, last_seen_at=NOW)
    # The narrow rule found it three hours ago and last saw it two hours ago.
    session.add(
        MonitorHit(
            monitor_id=narrow,
            item_id="shared",
            first_hit_at=NOW - timedelta(hours=3),
            last_hit_at=NOW - timedelta(hours=2),
            in_range=True,
        )
    )
    session.commit()

    narrow = analytics.listing_duration(session, KW, now=NOW)
    broad_result = analytics.listing_duration(session, "iPhone 15", now=NOW)

    # Left the narrow keyword's pages two hours ago: one sample, ~60 minutes.
    # Read off the histogram rather than the quantiles -- one sample has no
    # quantiles by design, and that is asserted elsewhere.
    assert narrow["sample_size"] == 1, "the global clock hid it"
    assert sum(b["count"] for b in narrow["histogram"]) == 1
    assert narrow["histogram"][-1]["hi_minutes"] <= 2 * 60
    assert narrow["legacy_clock_rows"] == 0
    # Still live for the broad keyword, so it has nothing to report.
    assert broad_result["sample_size"] == 0


def test_a_ledger_row_predating_the_per_keyword_clock_is_counted_and_flagged(session):
    """`last_hit_at` cannot be backfilled, so old rows fall back to the global
    timestamp -- and the response has to admit the sample is a mixture rather
    than present it as one clean measurement.
    """
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "old",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=3),
        last_seen_at=STALE,
    )
    row = session.get(MonitorHit, (monitor_id, "old"))
    assert row is not None
    row.last_hit_at = None  # as written before the column existed
    session.add(row)
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["sample_size"] == 1
    assert result["legacy_clock_rows"] == 1


def test_two_rules_on_one_keyword_do_not_count_a_listing_twice(session):
    """The earlier of the two ledger rows is when the KEYWORD first saw it."""
    first = seed_monitor(session)
    second = seed_monitor(session)
    seed_item(
        session,
        "shared",
        first,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=4),
        last_seen_at=STALE,
    )
    session.add(
        MonitorHit(monitor_id=second, item_id="shared", first_hit_at=NOW - timedelta(hours=2))
    )
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["sample_size"] == 1
    assert result["histogram"][-1]["hi_minutes"] > 2 * 60, "the earlier sighting starts the clock"
    # And the legacy flag counts SAMPLES, not ledger rows. Both rows here
    # predate last_hit_at, so a sum over the group reported 2 legacy rows for
    # 1 sample -- a number larger than sample_size, which the page phrases as
    # "N of M samples".
    assert result["legacy_clock_rows"] == 1
    assert result["legacy_clock_rows"] <= result["sample_size"]


def test_a_listing_first_seen_before_the_window_is_not_in_it(session):
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "ancient",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(days=40),
        last_seen_at=NOW - timedelta(days=35),
    )
    seed_item(
        session,
        "recent",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(days=2),
        last_seen_at=STALE,
    )

    assert analytics.listing_duration(session, KW, days=30, now=NOW)["sample_size"] == 1
    assert analytics.listing_duration(session, KW, days=60, now=NOW)["sample_size"] == 2


def test_histogram_buckets_are_whole_hours_and_cover_every_sample(session):
    """Whole hours for the same reason price edges are whole yuan: a bucket
    labelled "1小时43分–3小时20分" reads as noise. `_histogram` is the price
    routine with its alignment unit passed in, not a second copy.
    """
    monitor_id = seed_monitor(session)
    hours = [1, 3, 7, 26, 71]
    for i, h in enumerate(hours):
        seed_item(
            session,
            f"d{i}",
            monitor_id,
            prices=[300000],
            first_hit_at=STALE - timedelta(hours=h),
            last_seen_at=STALE,
        )

    result = analytics.listing_duration(session, KW, now=NOW)
    histogram = result["histogram"]
    assert sum(b["count"] for b in histogram) == len(hours)
    for bucket in histogram:
        assert bucket["lo_minutes"] % 60 == 0, bucket
        assert bucket["hi_minutes"] % 60 == 0, bucket
    assert histogram[0]["lo_minutes"] <= 60
    assert histogram[-1]["hi_minutes"] > 71 * 60


def test_the_duration_histogram_shows_a_shape_at_real_scale(session):
    """Hour-aligned buckets are wider than the whole distribution.

    Measured on the real database: the median listing stays in range for 1 to
    9 minutes, so a 60-minute bucket swallowed everything and 264 samples drew
    as ONE bar with a second holding nine. `_histogram`'s own comment says too
    few buckets hide the shape a distribution is drawn for, and that is
    exactly what happened.

    Reverse verification: pin `align=MINUTES_PER_HOUR` and this goes red at
    two buckets.
    """
    monitor_id = seed_monitor(session)
    # Sixty listings across two hours, front-loaded -- the real shape in
    # miniature. Sixty rather than twenty because the bucket COUNT is governed
    # by sqrt(n) (`_histogram`: too many buckets turn a small sample into a
    # comb of ones), so twenty samples legitimately get about four and could
    # not tell a healthy histogram from the collapsed one.
    spread = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50, 70, 95, 118]
    for i, gone_after in enumerate(spread * 3):
        seed_item(
            session,
            f"d{i}",
            monitor_id,
            prices=[300000],
            first_hit_at=NOW - timedelta(hours=3),
            last_seen_at=STALE,
        )
        row = session.get(MonitorHit, (monitor_id, f"d{i}"))
        assert row is not None
        row.last_hit_at = row.first_hit_at + timedelta(minutes=gone_after)
        session.add(row)
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    histogram = result["histogram"]

    assert result["sample_size"] == 60
    assert len(histogram) >= 5, f"collapsed into {len(histogram)} buckets"
    assert sum(b["count"] for b in histogram) == 60
    step = histogram[0]["hi_minutes"] - histogram[0]["lo_minutes"]
    assert step < 60, "an hour-wide bucket is wider than the whole spread"
    # A multiple of the aligned unit, which is itself off a 5/10/15/30/60
    # ladder -- so "a whole number of 5 minutes" is the invariant, not "one of
    # the ladder values". `_histogram` widens by whole align steps to reach
    # its bucket count.
    assert step % 5 == 0, f"unreadable step {step}"
    # More than one bucket is actually occupied -- that is what "shows a
    # shape" means, and a single 116-in-bucket-one histogram would not.
    assert sum(1 for b in histogram if b["count"] > 0) >= 4


def test_no_duration_quantile_lands_outside_the_sample(session):
    """The negative-price bug, in the duration unit.

    statistics.quantiles defaults to method="exclusive", which extrapolates
    past the observed range below 19 samples -- it produced a p10 of MINUS
    ¥4600 in M2. A negative duration is just as impossible, and small samples
    are the normal case here, so the boundary is pinned at every size from 2
    to 19.
    """
    for size in range(2, 20):
        minutes = [60] + [600] * (size - 2) + [6000]
        assert len(minutes) == size
        cuts = analytics._quantiles(minutes)
        assert min(cuts.values()) >= 60, (size, cuts)
        assert max(cuts.values()) <= 6000, (size, cuts)


def test_the_aperture_is_reported_and_pre_paging_rows_count_as_one_page(session):
    """A NULL `pages` on a SUCCESSFUL run is a fact about that cycle -- it read
    a single page, because that is all the code could do -- not missing data.
    """
    monitor_id = seed_monitor(session)
    session.add(CollectRun(monitor_id=monitor_id, started_at=NOW, ok=True, item_count=30))
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["aperture_pages_min"] == 1
    assert result["aperture_pages_max"] == 1
    assert result["aperture_rows"] == 30


def test_an_aperture_that_changed_mid_window_is_visible(session):
    """The whole reason the field exists.

    The same keyword measured at 1x30 and at 2x30 gives incomparable
    distributions: a wider aperture makes a listing "leave" later. A duration
    distribution without its aperture reads as a market measurement when part
    of it is a measurement of how many pages we looked at.

    Reverse verification: hard-code aperture_pages_min/max to a constant and
    this test goes red.
    """
    monitor_id = seed_monitor(session)
    session.add(
        CollectRun(
            monitor_id=monitor_id, started_at=NOW - timedelta(days=3), ok=True, item_count=28
        )
    )
    session.add(CollectRun(monitor_id=monitor_id, started_at=NOW, ok=True, item_count=58, pages=2))
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["aperture_pages_min"] == 1
    assert result["aperture_pages_max"] == 2


def test_a_cycle_that_fetched_nothing_does_not_claim_an_aperture(session):
    """A hard failure records no pages at all, and reading that as one page
    would report "the aperture changed" for every keyword that ever timed out.
    """
    monitor_id = seed_monitor(session)
    session.add(
        CollectRun(monitor_id=monitor_id, started_at=NOW, ok=False, error="transient: timeout")
    )
    session.add(CollectRun(monitor_id=monitor_id, started_at=NOW, ok=True, item_count=58, pages=2))
    session.commit()

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["aperture_pages_min"] == 2
    assert result["aperture_pages_max"] == 2


def test_a_partially_failed_cycle_still_reports_the_pages_it_got(session):
    """ok=False but pages=1: page two failed after page one succeeded, and
    those items were persisted. That cycle really did observe one page.
    """
    monitor_id = seed_monitor(session)
    session.add(
        CollectRun(
            monitor_id=monitor_id,
            started_at=NOW,
            ok=False,
            item_count=28,
            pages=1,
            error="page 2: timeout",
        )
    )
    session.commit()

    assert analytics.listing_duration(session, KW, now=NOW)["aperture_pages_max"] == 1


def test_no_runs_in_the_window_means_no_aperture_not_one_page(session):
    """ "We never looked" is not "we looked at one page"."""
    seed_monitor(session)
    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["aperture_pages_min"] is None
    assert result["aperture_pages_max"] is None


def test_another_keywords_aperture_is_not_this_keywords(session):
    seed_monitor(session, keyword=KW)
    other = seed_monitor(session, keyword="索尼 a7c2")
    session.add(CollectRun(monitor_id=other, started_at=NOW, ok=True, item_count=58, pages=2))
    session.commit()

    assert analytics.listing_duration(session, KW, now=NOW)["aperture_pages_max"] is None


def test_the_metric_never_claims_a_listing_was_sold(session):
    """Acceptance item, asserted rather than left to a grep in a report.

    A disappearance may be a purchase, a delisting, or merely a rank drop past
    the pages we read -- and the third one is our own doing. Naming it 成交 or
    售出 would turn our own aperture into a market fact.
    """
    import app.api.analytics as api_analytics
    import app.schemas as schemas

    for module in (analytics, api_analytics, schemas):
        with open(module.__file__ or "", encoding="utf-8") as handle:
            text = handle.read()
        assert "成交" not in text, module.__name__
        assert "售出" not in text, module.__name__


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


# --------------------------------------------------------------------------- #
# Raw samples for the KDE + rug (frontend-refactor round 2)
# --------------------------------------------------------------------------- #


def test_downsample_sorts_caps_and_keeps_the_extremes():
    """The frontend draws a density from these points; a cap that dropped the
    min or the max would move the tails of the drawn distribution."""
    values = list(range(1234))
    values.reverse()  # deliberately unsorted input
    out = analytics.downsample_sorted(values)
    assert len(out) == analytics.SAMPLES_CAP == 500
    assert out == sorted(out)
    assert out[0] == 0 and out[-1] == 1233

    small = analytics.downsample_sorted([5, 3, 4])
    assert small == [3, 4, 5], "below the cap: sorted, nothing dropped"


def test_price_samples_are_the_quantile_row_set_sorted(session):
    """Same rows the quantiles read, so the density the frontend draws cannot
    disagree with the numbers printed beside it."""
    monitor_id = seed_monitor(session)
    seed_item(session, "a", monitor_id, prices=[300000])
    seed_item(session, "b", monitor_id, prices=[100000])
    seed_item(session, "c", monitor_id, prices=[200000])

    result = analytics.price_distribution(session, KW, now=NOW)
    assert result["samples"] == [100000, 200000, 300000]
    assert len(result["samples"]) == result["sample_size"]

    empty = analytics.price_distribution(session, "nothing here", now=NOW)
    assert empty["samples"] == []


def test_duration_samples_are_sorted_minutes(session):
    """Minutes, matching the unit the duration quantiles already use."""
    monitor_id = seed_monitor(session)
    seed_item(
        session,
        "long",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=6),
        last_seen_at=STALE,
    )
    seed_item(
        session,
        "short",
        monitor_id,
        prices=[300000],
        first_hit_at=NOW - timedelta(hours=3),
        last_seen_at=STALE,
    )

    result = analytics.listing_duration(session, KW, now=NOW)
    assert result["sample_size"] == 2
    # 6h->1h ago is 300 minutes inside our range; 3h->1h ago is 120.
    assert result["samples"] == [120, 300]
