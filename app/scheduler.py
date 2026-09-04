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

from sqlmodel import Session, select

from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    RawItem,
    RawSeller,
    TransientCollectorError,
)
from app.collector.filters import RuleFilters
from app.collector.pipeline import Pipeline
from app.config import settings
from app.db import engine
from app.models import CollectRun, Monitor, NotifyChannel, Watchlist, utcnow
from app.notify import (
    channels_for_monitor,
    deliver,
    enabled_channels,
    group_by_reason,
    log_delivery,
)
from app.notify.base import Notification, Notifier
from app.store import (
    NotifiableHit,
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
        ok=error is None,
        item_count=item_count,
        collector=collector,
        error=error,
        started_at=started_at,
    )


def record_manual_run(
    monitor_id: int,
    *,
    collector: str | None,
    item_count: int = 0,
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


async def run_monitor_cycle(pipeline: Pipeline, monitor: Monitor) -> CycleOutcome:
    """One collection cycle for one rule.

    Order is fixed (design.md section 5): collect, screen, persist, then hand
    the notifiable list back. Deciding "should we notify?" is a database
    question answered inside persist_cycle — answering it from in-memory state
    re-announces everything after a restart.
    """
    assert monitor.id is not None
    items = await pipeline.collect_search(monitor.keyword, rows=30)
    candidates = await pipeline.screen(items, rule_filters(monitor))
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
            "found": len(items),
            "passed": passed,
            "notifiable": len(hits),
        },
    )
    return CycleOutcome(hits=hits, collector=source, collected=len(items), passed=passed)


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
            error=f"needs verification: {exc}",
            disable=True,
            started_at=started_at,
        )
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


def _sync_watch_channels() -> list[NotifyChannel]:
    """Every enabled channel: a watched item is not bound to a rule, so there
    is no rule-level channel list to consult."""
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
    channels = await asyncio.to_thread(_sync_watch_channels)
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
            record_watch_failure_sync, item_id, f"needs verification: {exc}", started_at
        )
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
