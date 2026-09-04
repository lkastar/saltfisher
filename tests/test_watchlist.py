"""Watch cycle behaviour: price drops, disappearance, and decoupling.

The watchlist exists so the user can wait for one specific thing. Two
properties therefore matter more than anything else here:

  * a disappearance is announced — the thing being waited for is gone, which
    is as time-critical as any price change;
  * tracking does not depend on any monitor rule still matching the item.
"""

from datetime import timedelta

import pytest
from sqlmodel import select

from app.collector.base import RawItem, RawSeller
from app.config import settings
from app.models import Item, Monitor, MonitorHit, PriceSnapshot, Seller, Watchlist, utcnow
from app.store import (
    evaluate_watch,
    mark_watch_notified,
    persist_watch_observation,
    record_watch_failure,
    watch_baseline,
)

NOW = utcnow()


def raw(price_cents: int = 300000, status: str = "on_sale", **kw) -> RawItem:
    return RawItem(
        **{
            **dict(
                item_id="i1",
                title="iPhone 15 128G",
                price_cents=price_cents,
                seller_id="s1",
                seller_nick="老王",
                source="detail",
                status=status,
            ),
            **kw,
        }
    )


@pytest.fixture
def watched(session):
    session.add(Seller(id="s1", nick="老王"))
    session.commit()
    session.add(Item(id="i1", title="iPhone 15 128G", seller_id="s1", seller_nick="老王"))
    session.add(PriceSnapshot(item_id="i1", price_cents=300000, status="on_sale", source="detail"))
    session.add(Watchlist(item_id="i1", added_price_cents=300000, added_at=NOW))
    session.commit()
    return session


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_baseline_starts_at_the_price_when_added():
    """So a freshly watched item that falls is caught before any notification
    has ever been sent."""
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    assert watch_baseline(entry) == 300000


def test_baseline_moves_to_the_last_announced_price():
    entry = Watchlist(item_id="i1", added_price_cents=300000, notified_price_cents=280000)
    assert watch_baseline(entry) == 280000


# --------------------------------------------------------------------------- #
# Price drop
# --------------------------------------------------------------------------- #


def test_a_drop_past_the_threshold_is_announced():
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    dropped = int(300000 * (1 - settings.price_drop_ratio)) - 100
    hit = evaluate_watch(entry, raw(dropped), now=NOW)
    assert hit is not None and hit.reason == "price_drop"
    assert hit.previous_price_cents == 300000


def test_a_drop_smaller_than_the_threshold_is_ignored():
    """Otherwise ordinary haggling noise alerts on every single cycle."""
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    assert evaluate_watch(entry, raw(299900), now=NOW) is None


def test_a_price_rise_is_not_news():
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    assert evaluate_watch(entry, raw(340000), now=NOW) is None


def test_an_unchanged_price_is_not_news():
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    assert evaluate_watch(entry, raw(300000), now=NOW) is None


def test_each_announcement_raises_the_bar_for_the_next():
    """A single long slide must not produce an alert every cycle: after each
    announcement the new price becomes the reference.
    """
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    first_drop = int(300000 * (1 - settings.price_drop_ratio)) - 100
    hit = evaluate_watch(entry, raw(first_drop), now=NOW)
    assert hit is not None

    entry.notified_price_cents = hit.price_cents
    # the same price is now the baseline, so it is no longer a drop
    assert evaluate_watch(entry, raw(first_drop), now=NOW) is None
    # only a further threshold-sized fall qualifies
    second = int(first_drop * (1 - settings.price_drop_ratio)) - 100
    assert evaluate_watch(entry, raw(second), now=NOW) is not None


# --------------------------------------------------------------------------- #
# Disappearance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["sold", "removed"])
def test_disappearance_is_announced_immediately(status):
    """The thing the user was waiting for no longer exists. That is not an
    error to swallow — it is the alert they most need.
    """
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    hit = evaluate_watch(entry, raw(300000, status=status), now=NOW)
    assert hit is not None and hit.reason == "gone"


def test_disappearance_wins_over_a_price_drop():
    """A sold item's last price is not a buying opportunity."""
    entry = Watchlist(item_id="i1", added_price_cents=300000)
    hit = evaluate_watch(entry, raw(100000, status="sold"), now=NOW)
    assert hit is not None and hit.reason == "gone"


def test_a_gone_item_stops_being_polled_but_stays_in_the_list(watched):
    """Nothing left to poll, yet the row remains as a price reference — the
    PRD requires sold entries not to vanish from the list.
    """
    hit = evaluate_watch(watched.get(Watchlist, "i1"), raw(300000, status="sold"), now=NOW)
    assert hit is not None
    mark_watch_notified(watched, hit)
    watched.commit()

    entry = watched.get(Watchlist, "i1")
    assert entry is not None, "the entry must not be deleted"
    assert entry.price_watch_enabled is False


def test_a_detail_fetch_reporting_the_item_missing_still_alerts(watched):
    """`gone=True` covers the case where the fetch itself said "no such item":
    there is no payload to store, but the disappearance is a real observation.
    """
    hit = persist_watch_observation(watched, "i1", None, None, gone=True, now=NOW)
    watched.commit()
    assert hit is not None and hit.reason == "gone"
    assert watched.get(Item, "i1").status == "removed"
    # the last known price is reported rather than a fabricated zero
    assert hit.price_cents == 300000


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def test_a_cycle_appends_a_snapshot_only_when_the_price_moved(watched):
    persist_watch_observation(watched, "i1", raw(300000), None, now=NOW)
    watched.commit()
    assert len(watched.exec(select(PriceSnapshot)).all()) == 1

    later = NOW + timedelta(hours=1)
    persist_watch_observation(watched, "i1", raw(280000), None, now=later)
    watched.commit()
    prices = [s.price_cents for s in watched.exec(select(PriceSnapshot)).all()]
    assert prices == [300000, 280000]


def test_last_seen_advances_even_without_a_snapshot(watched):
    later = NOW + timedelta(hours=6)
    persist_watch_observation(watched, "i1", raw(300000), None, now=later)
    watched.commit()
    assert watched.get(Item, "i1").last_seen_at == later
    assert len(watched.exec(select(PriceSnapshot)).all()) == 1


def test_the_seller_profile_is_keyed_on_the_id_the_item_points_at(watched):
    """The search and detail routes use different id spaces for one seller.
    Keying the profile on the detail id would create a second, unlinkable row.
    """
    profile = RawSeller(
        seller_id="2218219939144",  # numeric, from the detail route
        nick="小顾数码",
        source="detail",
        credit_level=5,
        positive_rate=97.0,
    )
    persist_watch_observation(watched, "i1", raw(300000), profile, now=NOW)
    watched.commit()

    sellers = watched.exec(select(Seller)).all()
    assert [s.id for s in sellers] == ["s1"], "no duplicate seller row"
    assert sellers[0].credit_level == 5
    assert sellers[0].nick == "小顾数码"


def test_a_successful_cycle_clears_the_failure_state(watched):
    entry = watched.get(Watchlist, "i1")
    entry.consecutive_failures = 3
    entry.last_error = "boom"
    watched.commit()

    persist_watch_observation(watched, "i1", raw(300000), None, now=NOW)
    watched.commit()
    entry = watched.get(Watchlist, "i1")
    assert entry.consecutive_failures == 0 and entry.last_error is None


def test_repeated_failures_disable_the_watch_with_a_reason(watched):
    for _ in range(settings.max_consecutive_failures):
        record_watch_failure(watched, "i1", "timeout")
    watched.commit()

    entry = watched.get(Watchlist, "i1")
    assert entry.price_watch_enabled is False
    assert "auto-disabled" in (entry.last_error or "")


def test_a_failure_below_the_limit_keeps_watching(watched):
    record_watch_failure(watched, "i1", "timeout")
    watched.commit()
    entry = watched.get(Watchlist, "i1")
    assert entry.price_watch_enabled is True
    assert entry.last_error == "timeout"


# --------------------------------------------------------------------------- #
# Decoupling from monitor rules
# --------------------------------------------------------------------------- #


def test_deleting_the_rule_that_found_an_item_does_not_stop_watching(watched):
    """The reason the watchlist has its own loop and its own primary key."""
    monitor = Monitor(name="rule", keyword="iPhone 15", baseline_done=True)
    watched.add(monitor)
    watched.commit()
    watched.add(MonitorHit(monitor_id=monitor.id, item_id="i1"))
    watched.commit()

    for hit in watched.exec(select(MonitorHit)).all():
        watched.delete(hit)
    watched.delete(monitor)
    watched.commit()

    entry = watched.get(Watchlist, "i1")
    assert entry is not None and entry.price_watch_enabled
    result = persist_watch_observation(watched, "i1", raw(200000), None, now=NOW)
    watched.commit()
    assert result is not None and result.reason == "price_drop"
