"""The collection run log: every cycle exit writes exactly one row.

This table exists for one question the rest of the schema cannot answer: on a
given day, did we collect at all? Item and PriceSnapshot can only say what was
found, so "no new listings" and "the process was down" look identical in them.
Measured on the real database before this table existed: 4 items first seen on
2026-08-31, 312 on 2026-09-04, and three empty days in between that were
downtime — with nothing anywhere able to say so.

The whole value therefore rests on the FAILURE branches being logged. A run log
that records only successes is precisely the log that cannot answer the
question, so most of this file is failures.
"""

import asyncio
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app import scheduler
from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    RawItem,
    TransientCollectorError,
)
from app.models import CollectRun, Item, Monitor, PriceSnapshot, Seller, Watchlist, utcnow
from app.scheduler import CycleOutcome

pytestmark = pytest.mark.asyncio


def raw(item_id: str = "i1", price_cents: int = 300000) -> RawItem:
    return RawItem(
        item_id=item_id,
        title="iPhone 15 128G",
        price_cents=price_cents,
        seller_id="s1",
        seller_nick="老王",
        source="detail",
        status="on_sale",
    )


@pytest.fixture
def wired(monkeypatch):
    """A real in-memory database with one rule and one watched item."""
    from tests.conftest import memory_engine

    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr("app.store.settings", scheduler.settings)

    with Session(engine) as s:
        monitor = Monitor(name="rule", keyword="iPhone 15 128G", baseline_done=True)
        s.add(monitor)
        s.add(Seller(id="s1", nick="老王"))
        s.commit()
        s.add(Item(id="i1", title="iPhone 15 128G", seller_id="s1", seller_nick="老王"))
        s.add(PriceSnapshot(item_id="i1", price_cents=300000, status="on_sale", source="detail"))
        s.add(Watchlist(item_id="i1", added_price_cents=300000))
        s.commit()
        assert monitor.id is not None
        return engine, monitor.id


def runs(engine) -> list[CollectRun]:
    with Session(engine) as s:
        return list(s.exec(select(CollectRun).order_by(CollectRun.id)).all())


class FakePipeline:
    """Stands in for the real pipeline so no test touches the network."""

    def __init__(self, *, error: Exception | None = None, item: RawItem | None = None) -> None:
        self.error = error
        self.item = item

    async def collect_item(self, item_id: str):
        if self.error:
            raise self.error
        return self.item, None


# --------------------------------------------------------------------------- #
# Search cycles
# --------------------------------------------------------------------------- #


async def test_a_successful_search_cycle_is_logged_with_its_counts(wired, monkeypatch):
    engine, monitor_id = wired

    async def cycle(pipeline, monitor):
        return CycleOutcome(hits=[], collector="mtop", collected=30, passed=25)

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    await scheduler._run_and_record(object(), monitor_id)

    (row,) = runs(engine)
    assert row.monitor_id == monitor_id
    assert row.item_id is None
    assert row.ok is True
    assert row.item_count == 30  # how many listings the cycle saw, not how many passed
    assert row.collector == "mtop"
    assert row.error is None


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (TransientCollectorError("rate limited"), "transient"),
        (ChallengeError("captcha"), "needs verification"),
        (CollectorError("upstream said no"), "upstream said no"),
        (ValueError("our own normaliser is broken"), "ValueError"),
    ],
)
async def test_every_failing_search_branch_is_logged(wired, monkeypatch, error, fragment):
    """One case per `except` arm of _run_and_record.

    Parametrised rather than written once because the arms do genuinely
    different things — back off, ask for verification, disable, log a
    traceback — and it is entirely possible to wire the run log into three of
    them and miss the fourth.
    """
    engine, monitor_id = wired

    async def cycle(pipeline, monitor):
        raise error

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    await scheduler._run_and_record(object(), monitor_id)

    (row,) = runs(engine)
    assert row.ok is False
    assert row.error is not None
    assert fragment in row.error
    assert row.item_count == 0


async def test_a_bug_in_our_own_code_is_logged_and_the_loop_survives(wired, monkeypatch):
    """The bare `except Exception` in _run_and_record is the one the project
    allows. It must still leave a row behind — a swallowed bug that also
    vanishes from the run log is invisible twice over.
    """
    engine, monitor_id = wired

    async def cycle(pipeline, monitor):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    outcome = await scheduler._run_and_record(object(), monitor_id)  # must not raise

    assert outcome.collected == 0
    (row,) = runs(engine)
    assert row.ok is False
    assert "ZeroDivisionError" in (row.error or "")


async def test_the_run_row_is_stamped_when_the_cycle_started(wired, monkeypatch):
    """`started_at` must mean what it says.

    The parameter existed from the beginning and no caller passed it, so every
    row was stamped at completion instead. Milliseconds usually, but a cycle
    beginning 23:59:50 UTC then answered "did we collect that day" for the
    following day -- and a column whose name disagrees with its contents
    misleads every later reader regardless.

    The fake cycle sleeps so start and finish are distinguishable at all;
    without the fix the row lands after `inside`, not before it.
    """
    engine, monitor_id = wired
    inside: list[datetime] = []

    async def cycle(pipeline, monitor):
        await asyncio.sleep(0.05)
        inside.append(utcnow())
        return CycleOutcome(hits=[], collector="mtop", collected=3, passed=3)

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    await scheduler._run_and_record(object(), monitor_id)

    (row,) = runs(engine)
    assert row.started_at < inside[0], "stamped at completion, not at the start"


async def test_a_successful_manual_run_is_logged(wired):
    """A manual run really did observe the market, so it counts as coverage.

    Its FAILURES are deliberately not logged: run_monitor_now reports those to
    the user as a 502 and never calls record_manual_run, to keep a probe from
    counting toward auto-disable. Excluding them keeps `runs_failed` meaning
    "the schedule had trouble" rather than "someone clicked test".
    """
    engine, monitor_id = wired
    await asyncio.to_thread(
        scheduler.record_manual_run, monitor_id, collector="browser", item_count=30
    )

    (row,) = runs(engine)
    assert row.monitor_id == monitor_id
    assert row.ok is True
    assert row.collector == "browser"
    # Asserted because the first live drill caught exactly this: the endpoint
    # reported 30 collected and the row said 0, since record_manual_run did
    # not forward the count. A run row claiming zero listings is not a
    # cosmetic error -- it is indistinguishable from an empty market.
    assert row.item_count == 30


# --------------------------------------------------------------------------- #
# Watch cycles
# --------------------------------------------------------------------------- #


async def test_a_successful_watch_cycle_is_logged_against_the_item(wired):
    engine, _ = wired
    await scheduler.run_watch_cycle(FakePipeline(item=raw(price_cents=280000)), "i1")

    (row,) = runs(engine)
    assert row.item_id == "i1"
    assert row.monitor_id is None
    assert row.ok is True
    assert row.item_count == 1
    assert row.collector == "detail"


async def test_a_gone_listing_is_a_successful_cycle_not_a_failure(wired):
    """The disappearance IS the observation. Recording it as a failed cycle
    would make the supply chart read a sold-out item as downtime.
    """
    engine, _ = wired
    await scheduler.run_watch_cycle(FakePipeline(error=ItemGoneError("deleted")), "i1")

    (row,) = runs(engine)
    assert row.ok is True
    assert row.item_count == 0  # nothing on sale to count
    assert row.error is None


@pytest.mark.parametrize(
    "error",
    [
        TransientCollectorError("rate limited"),
        ChallengeError("captcha"),
        CollectorError("upstream said no"),
        ValueError("our own parser is broken"),
    ],
)
async def test_every_failing_watch_branch_is_logged(wired, error):
    engine, _ = wired
    await scheduler.run_watch_cycle(FakePipeline(error=error), "i1")

    (row,) = runs(engine)
    assert row.item_id == "i1"
    assert row.ok is False
    assert row.error


# --------------------------------------------------------------------------- #
# The log must never be able to break what it observes
# --------------------------------------------------------------------------- #


async def test_a_broken_run_log_does_not_break_the_cycle(wired, monkeypatch):
    """Reverse verification of the isolation, not of the happy path.

    CollectRun is replaced with something that raises on construction, so the
    real _sync_record_run body — its own session, its own catch — is what is
    under test. If the write ever shares the caller's transaction, the rule's
    own state stops being persisted and this fails.
    """
    engine, monitor_id = wired

    def explode(**kwargs):
        raise RuntimeError("disk full")

    async def cycle(pipeline, monitor):
        return CycleOutcome(hits=[], collector="mtop", collected=7, passed=7)

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    monkeypatch.setattr(scheduler, "CollectRun", explode)

    await scheduler._run_and_record(object(), monitor_id)  # must not raise

    with Session(engine) as s:
        monitor = s.get(Monitor, monitor_id)
        assert monitor is not None
        assert monitor.last_run_at is not None, "the business write was rolled back with the log"
        assert monitor.last_error is None
        assert monitor.consecutive_failures == 0


async def test_a_broken_run_log_does_not_break_a_watch_cycle(wired, monkeypatch):
    engine, _ = wired

    def explode(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(scheduler, "CollectRun", explode)
    await scheduler.run_watch_cycle(FakePipeline(item=raw(price_cents=280000)), "i1")

    with Session(engine) as s:
        entry = s.get(Watchlist, "i1")
        assert entry is not None
        assert entry.last_run_at is not None


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


async def test_the_table_is_created_from_the_models(wired):
    """A new table is what create_all() actually handles — unlike a new column,
    which needs add_missing_columns (see spec/backend/database-guidelines.md).
    Asserting it here keeps "the model exists" from being confused with "the
    table exists on an already-deployed database".
    """
    engine, _ = wired
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "collectrun" in names


# --------------------------------------------------------------------------- #
# Seller rules, before their collection path exists (P5/T2a)
# --------------------------------------------------------------------------- #


async def test_a_seller_rule_is_refused_before_the_pipeline_is_touched():
    """`keyword` became nullable in T2a; the seller collection path is T2b.

    `pipeline=None` is the assertion: the guard has to fire before anything
    reaches the collector, because the alternative is `collect_search(None)`
    formatting the literal string "None" into a search URL.
    """
    monitor = Monitor(id=1, name="小顾数码的新货", seller_id="opaque==")

    with pytest.raises(CollectorError, match="seller collection is not implemented"):
        await scheduler.run_monitor_cycle(None, monitor)  # type: ignore[arg-type]


async def test_a_seller_rule_says_why_on_the_management_page(wired, monkeypatch):
    """Not silently skipped. A rule that never runs and never says why is the
    exact failure the health columns exist to prevent, so it goes down the
    ordinary CollectorError branch: last_error set, one run row, ok=False."""
    engine, _ = wired
    with Session(engine) as s:
        rule = Monitor(name="小顾数码的新货", seller_id="s1")
        s.add(rule)
        s.commit()
        seller_rule_id = rule.id
    assert seller_rule_id is not None

    await scheduler._run_and_record(object(), seller_rule_id)

    with Session(engine) as s:
        stored = s.get(Monitor, seller_rule_id)
        assert stored is not None
        assert "seller collection is not implemented" in (stored.last_error or "")
        assert stored.consecutive_failures == 1
    (row,) = [r for r in runs(engine) if r.monitor_id == seller_rule_id]
    assert row.ok is False
