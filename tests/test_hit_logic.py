"""The four state transitions of "the price entered the range".

This is the logic that decides whether the user hears about a listing at all,
and every one of these cases is a way the tool can silently stop working:

  1. first seen in range          -> announce
  2. seen before ABOVE budget, now inside      -> announce  (most common deal)
  3. was inside, rose out, came back           -> announce again
  4. inside already, fell past the threshold   -> announce again

Deduping on "have we seen this item id" — the obvious implementation — loses
case 2 permanently.
"""

from datetime import timedelta

import pytest
from sqlmodel import select

from app.collector.base import RawItem
from app.collector.filters import FilterOutcome
from app.collector.pipeline import Candidate
from app.config import settings
from app.models import Item, MonitorHit, PriceSnapshot, Seller, utcnow
from app.store import evaluate_hit, mark_notified, persist_cycle

NOW = utcnow()
MONITOR = 1


def raw(price_cents: int = 300000, item_id: str = "i1", **kw) -> RawItem:
    return RawItem(
        **{
            **dict(
                item_id=item_id,
                title="iPhone 15 128G",
                price_cents=price_cents,
                seller_id="s1",
                seller_nick="老王",
                source="mtop",
            ),
            **kw,
        }
    )


def cand(price_cents: int = 300000, *, passed: bool = True, unverified=(), **kw) -> Candidate:
    return Candidate(
        item=raw(price_cents, **kw),
        outcome=FilterOutcome(passed, unverified=tuple(unverified)),
    )


@pytest.fixture
def seeded(session):
    """A cycle that has already established its baseline."""
    session.add(Seller(id="s1", nick="老王"))
    session.commit()
    return session


# --------------------------------------------------------------------------- #
# Case 1 — first sighting
# --------------------------------------------------------------------------- #


def test_first_sighting_in_range_is_announced(seeded):
    hit = evaluate_hit(seeded, MONITOR, cand(), baseline_done=True, now=NOW)
    assert hit is not None
    assert hit.reason == "new_in_range"
    assert hit.previous_price_cents is None


def test_first_sighting_out_of_range_is_recorded_silently(seeded):
    assert evaluate_hit(seeded, MONITOR, cand(passed=False), baseline_done=True, now=NOW) is None
    row = seeded.exec(select(MonitorHit)).one()
    assert row.in_range is False
    assert row.notified_at is None


def test_baseline_run_records_everything_and_announces_nothing(seeded):
    """Creating a rule must not dump the existing market into the inbox."""
    for i in range(3):
        assert (
            evaluate_hit(seeded, MONITOR, cand(item_id=f"i{i}"), baseline_done=False, now=NOW)
            is None
        )
    rows = seeded.exec(select(MonitorHit)).all()
    assert len(rows) == 3
    assert all(r.in_range for r in rows)
    # Stamped as already-known, NOT left NULL — see the next test for why.
    assert all(r.notified_at == NOW for r in rows)
    assert all(r.notified_price_cents == 300000 for r in rows)


def test_the_cycle_after_the_baseline_announces_nothing(seeded):
    """Regression, found on live data.

    A baseline row left with notified_at NULL is indistinguishable from "we
    decided to notify and the send failed", so the retry branch announced the
    whole baseline on the second cycle — 7 of 30 rows in the real run. The
    baseline then only delayed the flood by one cycle instead of preventing it.
    """
    for i in range(3):
        evaluate_hit(seeded, MONITOR, cand(item_id=f"i{i}"), baseline_done=False, now=NOW)
    seeded.commit()

    later = NOW + timedelta(minutes=settings.renotify_cooldown_minutes + 1)
    for i in range(3):
        assert (
            evaluate_hit(seeded, MONITOR, cand(item_id=f"i{i}"), baseline_done=True, now=later)
            is None
        )


def test_an_item_appearing_after_the_baseline_is_announced(seeded):
    """The flip side: the baseline must not silence genuinely new listings."""
    evaluate_hit(seeded, MONITOR, cand(item_id="old"), baseline_done=False, now=NOW)
    seeded.commit()

    later = NOW + timedelta(hours=1)
    fresh = evaluate_hit(seeded, MONITOR, cand(item_id="new"), baseline_done=True, now=later)
    assert fresh is not None and fresh.reason == "new_in_range"


def test_a_drop_below_the_baseline_price_is_announced(seeded):
    """Stamping the baseline makes its price the reference for later drops."""
    evaluate_hit(seeded, MONITOR, cand(300000, item_id="i1"), baseline_done=False, now=NOW)
    seeded.commit()

    later = NOW + timedelta(minutes=settings.renotify_cooldown_minutes + 1)
    dropped = int(300000 * (1 - settings.price_drop_ratio)) - 100
    hit = evaluate_hit(seeded, MONITOR, cand(dropped, item_id="i1"), baseline_done=True, now=later)
    assert hit is not None and hit.reason == "price_drop"
    assert hit.previous_price_cents == 300000


# --------------------------------------------------------------------------- #
# Case 2 — the one a naive implementation loses
# --------------------------------------------------------------------------- #


def test_item_seen_above_budget_then_dropping_into_range_is_announced(seeded):
    """The most common way a deal appears, and the reason dedup cannot be
    keyed on "have we seen this item".
    """
    first = evaluate_hit(seeded, MONITOR, cand(900000, passed=False), baseline_done=True, now=NOW)
    assert first is None
    seeded.commit()

    later = evaluate_hit(
        seeded, MONITOR, cand(300000, passed=True), baseline_done=True, now=NOW + timedelta(hours=1)
    )
    assert later is not None and later.reason == "new_in_range"


# --------------------------------------------------------------------------- #
# Case 3 — leaving and re-entering
# --------------------------------------------------------------------------- #


def test_rising_out_of_range_then_falling_back_is_announced_again(seeded):
    announced = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    assert announced is not None
    mark_notified(seeded, MONITOR, [announced], now=NOW)
    seeded.commit()

    # rises out
    assert (
        evaluate_hit(
            seeded,
            MONITOR,
            cand(900000, passed=False),
            baseline_done=True,
            now=NOW + timedelta(hours=1),
        )
        is None
    )
    seeded.commit()
    assert seeded.exec(select(MonitorHit)).one().in_range is False

    # comes back — a fresh entry, even though the ledger row already exists
    again = evaluate_hit(
        seeded, MONITOR, cand(310000), baseline_done=True, now=NOW + timedelta(hours=2)
    )
    assert again is not None and again.reason == "new_in_range"


# --------------------------------------------------------------------------- #
# Case 4 — price drop while already in range
# --------------------------------------------------------------------------- #


def test_drop_past_the_threshold_is_announced(seeded):
    first = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [first], now=NOW)
    seeded.commit()

    later = NOW + timedelta(minutes=settings.renotify_cooldown_minutes + 1)
    dropped = int(300000 * (1 - settings.price_drop_ratio)) - 100
    hit = evaluate_hit(seeded, MONITOR, cand(dropped), baseline_done=True, now=later)
    assert hit is not None
    assert hit.reason == "price_drop"
    assert hit.previous_price_cents == 300000


def test_drop_smaller_than_the_threshold_is_ignored(seeded):
    """Without this, ordinary haggling noise pushes a notification every cycle."""
    first = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [first], now=NOW)
    seeded.commit()

    later = NOW + timedelta(minutes=settings.renotify_cooldown_minutes + 1)
    assert evaluate_hit(seeded, MONITOR, cand(299900), baseline_done=True, now=later) is None


def test_price_rise_inside_the_range_is_not_announced(seeded):
    first = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [first], now=NOW)
    seeded.commit()

    later = NOW + timedelta(hours=5)
    assert evaluate_hit(seeded, MONITOR, cand(340000), baseline_done=True, now=later) is None


def test_same_price_is_not_announced_twice(seeded):
    first = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [first], now=NOW)
    seeded.commit()
    later = NOW + timedelta(hours=5)
    assert evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=later) is None


# --------------------------------------------------------------------------- #
# Cooldown and retry
# --------------------------------------------------------------------------- #


def test_cooldown_suppresses_boundary_flapping(seeded):
    first = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [first], now=NOW)
    seeded.commit()

    inside_cooldown = NOW + timedelta(minutes=settings.renotify_cooldown_minutes - 1)
    dropped = int(300000 * (1 - settings.price_drop_ratio)) - 100
    assert (
        evaluate_hit(seeded, MONITOR, cand(dropped), baseline_done=True, now=inside_cooldown)
        is None
    )


def test_a_hit_that_was_never_sent_is_retried(seeded):
    """A decided-but-unsent notification must not be lost: notified_at is only
    written after a successful send, so its absence means the send failed.
    """
    assert evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW) is not None
    seeded.commit()  # ledger row exists, notified_at still NULL

    retry = evaluate_hit(
        seeded, MONITOR, cand(300000), baseline_done=True, now=NOW + timedelta(minutes=5)
    )
    assert retry is not None and retry.reason == "new_in_range"


def test_mark_notified_records_the_announced_price(seeded):
    hit = evaluate_hit(seeded, MONITOR, cand(300000), baseline_done=True, now=NOW)
    mark_notified(seeded, MONITOR, [hit], now=NOW)
    seeded.commit()
    row = seeded.exec(select(MonitorHit)).one()
    assert row.notified_at == NOW
    assert row.notified_price_cents == 300000


# --------------------------------------------------------------------------- #
# Waived filters travel with the notification
# --------------------------------------------------------------------------- #


def test_waived_filter_labels_reach_the_notification(seeded):
    hit = evaluate_hit(
        seeded,
        MONITOR,
        cand(unverified=("region", "min_seller_credit")),
        baseline_done=True,
        now=NOW,
    )
    assert hit is not None
    assert "地区未知" in hit.unverified_labels
    assert "卖家信用未知" in hit.unverified_labels


def test_internal_pending_marker_is_not_shown_to_the_user(seeded):
    hit = evaluate_hit(
        seeded, MONITOR, cand(unverified=("seller_profile_pending",)), baseline_done=True, now=NOW
    )
    assert hit is not None and hit.unverified_labels == ()


# --------------------------------------------------------------------------- #
# Full cycle persistence
# --------------------------------------------------------------------------- #


def test_persist_cycle_writes_rows_in_dependency_order(session):
    candidates = [cand(300000, item_id="a"), cand(900000, item_id="b", passed=False)]
    hits = persist_cycle(session, MONITOR, candidates, baseline_done=True, now=NOW)
    session.commit()

    assert [h.item_id for h in hits] == ["a"]
    assert session.get(Seller, "s1") is not None
    assert {i.id for i in session.exec(select(Item)).all()} == {"a", "b"}
    # every first sighting gets a baseline snapshot, in range or not
    assert len(session.exec(select(PriceSnapshot)).all()) == 2


def test_snapshot_is_appended_only_when_the_price_moves(session):
    persist_cycle(session, MONITOR, [cand(300000)], baseline_done=True, now=NOW)
    session.commit()
    persist_cycle(
        session, MONITOR, [cand(300000)], baseline_done=True, now=NOW + timedelta(hours=1)
    )
    session.commit()
    assert len(session.exec(select(PriceSnapshot)).all()) == 1

    persist_cycle(
        session, MONITOR, [cand(280000)], baseline_done=True, now=NOW + timedelta(hours=2)
    )
    session.commit()
    prices = [s.price_cents for s in session.exec(select(PriceSnapshot)).all()]
    assert prices == [300000, 280000]


def test_last_seen_at_advances_every_cycle_even_without_a_snapshot(session):
    """Liveness is a timestamp, not a snapshot row -- which is what keeps the
    price history sparse. Note it is GLOBAL: per-keyword liveness is
    MonitorHit.last_hit_at, pinned by the test below."""
    persist_cycle(session, MONITOR, [cand(300000)], baseline_done=True, now=NOW)
    session.commit()
    later = NOW + timedelta(hours=6)
    persist_cycle(session, MONITOR, [cand(300000)], baseline_done=True, now=later)
    session.commit()

    item = session.get(Item, "i1")
    assert item is not None
    assert item.first_seen_at == NOW
    assert item.last_seen_at == later
    assert len(session.exec(select(PriceSnapshot)).all()) == 1


def test_last_hit_at_is_stamped_on_every_path_a_rule_sees_a_listing(session):
    """Seeing a listing is a different fact from "it qualifies",
    and `analytics.listing_duration` is timed by the first one.

    So the stamp has to land before any in-range reasoning: on the baseline
    cycle, on an out-of-budget candidate, and on both the new-row and the
    existing-row branch of evaluate_hit. Any path that returns early without
    stamping makes that listing look like it left the market on the last cycle
    that happened to like its price.

    And it must NOT advance for a listing this cycle did not return, because
    that is the entire signal: reverse-verify by deleting either stamp from
    `store.evaluate_hit` and the "not returned" case starts advancing too.
    """
    both = [cand(300000, item_id="cheap"), cand(900000, item_id="dear", passed=False)]

    # Baseline cycle: nothing is announced, everything is still SEEN.
    persist_cycle(session, MONITOR, both, baseline_done=False, now=NOW)
    session.commit()
    for item_id in ("cheap", "dear"):
        hit = session.get(MonitorHit, (MONITOR, item_id))
        assert hit is not None and hit.last_hit_at == NOW, item_id

    # Ordinary cycle: the existing-row branch, in range and out of it.
    later = NOW + timedelta(minutes=10)
    persist_cycle(session, MONITOR, both, baseline_done=True, now=later)
    session.commit()
    for item_id in ("cheap", "dear"):
        hit = session.get(MonitorHit, (MONITOR, item_id))
        assert hit is not None and hit.last_hit_at == later, item_id

    # A cycle that did not return "dear": its clock stops where it was.
    last = later + timedelta(minutes=10)
    persist_cycle(session, MONITOR, [both[0]], baseline_done=True, now=last)
    session.commit()
    assert session.get(MonitorHit, (MONITOR, "cheap")).last_hit_at == last
    assert session.get(MonitorHit, (MONITOR, "dear")).last_hit_at == later


def test_seller_reputation_from_the_search_row_is_stored(session):
    persist_cycle(
        session,
        MONITOR,
        [cand(300000, seller_is_shop=True, seller_review_count=8237, seller_positive_rate=53.0)],
        baseline_done=True,
        now=NOW,
    )
    session.commit()
    seller = session.get(Seller, "s1")
    assert seller is not None
    assert (seller.is_shop, seller.review_count, seller.positive_rate) == (True, 8237, 53.0)
    # profile itself was never fetched, and that distinction must survive
    assert seller.fetched_at is None
