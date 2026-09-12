"""Monitor rule endpoints."""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func
from sqlmodel import select

from app.collector.base import ChallengeError, CollectorError
from app.db import SessionDep
from app.models import Monitor, MonitorChannel, MonitorHit, NotifyChannel, Seller, utcnow
from app.scheduler import COLLECT_SEMAPHORE, record_manual_run, run_monitor_cycle
from app.schemas import CycleResult, MonitorCreate, MonitorPublic, MonitorUpdate

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/monitors", tags=["monitors"])


def _channel_ids(session: SessionDep, monitor_id: int) -> list[int]:
    return sorted(
        link.channel_id
        for link in session.exec(
            select(MonitorChannel).where(MonitorChannel.monitor_id == monitor_id)
        ).all()
    )


def _hit_count(session: SessionDep, monitor_id: int) -> int:
    """How many listings this rule has ever matched, counted from the ledger.

    Not a stored column. `MonitorHit` IS the answer, and the covering index
    `ix_hit_monitor_time` makes the count an index scan -- cheaper than the
    class of bug a denormalised copy produced here for real.
    """
    return int(
        session.exec(
            select(func.count()).select_from(MonitorHit).where(MonitorHit.monitor_id == monitor_id)
        ).one()
    )


def _public(session: SessionDep, monitor: Monitor) -> MonitorPublic:
    """Monitor row plus its channel links.

    Built explicitly because `channel_ids` is not a column: returning the ORM
    row would serialise it as an empty list and quietly report that every rule
    notifies nobody.

    `seller_nick` is the same story for the other direction: `seller_id` is an
    opaque base64 token, so a seller rule with only that in the response is
    unlabellable in the UI.
    """
    assert monitor.id is not None
    seller = None if monitor.seller_id is None else session.get(Seller, monitor.seller_id)
    return MonitorPublic(
        **monitor.model_dump(),
        hit_count=_hit_count(session, monitor.id),
        channel_ids=_channel_ids(session, monitor.id),
        seller_nick=None if seller is None else seller.nick,
    )


def _require_seller(session: SessionDep, seller_id: str | None) -> None:
    """A rule may only point at a seller we have actually collected.

    Same shape and same status as `_set_channels`'s channel check. Skipping it
    would not even fail loudly: `PRAGMA foreign_keys` is per-connection and is
    not set on the pooled ones, so a typo'd id lands in the table and comes
    back as a rule with no name attached to it.
    """
    if seller_id is not None and session.get(Seller, seller_id) is None:
        raise HTTPException(status_code=422, detail=f"seller {seller_id} does not exist")


def _set_channels(session: SessionDep, monitor_id: int, channel_ids: list[int]) -> None:
    """Replace the link set. An empty list detaches everything, on purpose."""
    for link in session.exec(
        select(MonitorChannel).where(MonitorChannel.monitor_id == monitor_id)
    ).all():
        session.delete(link)
    for channel_id in dict.fromkeys(channel_ids):
        if session.get(NotifyChannel, channel_id) is None:
            raise HTTPException(status_code=422, detail=f"channel {channel_id} does not exist")
        session.add(MonitorChannel(monitor_id=monitor_id, channel_id=channel_id))


@router.get("", response_model=list[MonitorPublic])
def list_monitors(
    session: SessionDep,
    limit: Annotated[int, Query(le=200)] = 100,
    offset: int = 0,
) -> list[MonitorPublic]:
    stmt = select(Monitor).order_by(Monitor.id).offset(offset).limit(limit)  # type: ignore[arg-type]
    return [_public(session, monitor) for monitor in session.exec(stmt).all()]


@router.post("", response_model=MonitorPublic, status_code=201)
def create_monitor(payload: MonitorCreate, session: SessionDep) -> MonitorPublic:
    _require_seller(session, payload.seller_id)
    monitor = Monitor(**payload.model_dump(exclude={"channel_ids"}))
    session.add(monitor)
    session.flush()
    assert monitor.id is not None
    _set_channels(session, monitor.id, payload.channel_ids)
    session.commit()
    session.refresh(monitor)
    return _public(session, monitor)


@router.get("/{monitor_id}", response_model=MonitorPublic)
def read_monitor(monitor_id: int, session: SessionDep) -> MonitorPublic:
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    return _public(session, monitor)


@router.patch("/{monitor_id}", response_model=MonitorPublic)
def update_monitor(monitor_id: int, payload: MonitorUpdate, session: SessionDep) -> MonitorPublic:
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    changes = payload.model_dump(exclude_unset=True)
    channel_ids = changes.pop("channel_ids", None)
    lo = changes.get("price_min_cents", monitor.price_min_cents)
    hi = changes.get("price_max_cents", monitor.price_max_cents)
    if lo is not None and hi is not None and lo > hi:
        raise HTTPException(
            status_code=422, detail="price_min_cents must not exceed price_max_cents"
        )
    # The merged rule still has to target exactly one thing, and only this
    # layer can see both halves: MonitorUpdate rejects "both in one request",
    # but "clear the keyword and leave seller_id null" only looks wrong next
    # to the stored row. Without this the database CHECK would answer, as a
    # 500.
    keyword = changes.get("keyword", monitor.keyword)
    seller_id = changes.get("seller_id", monitor.seller_id)
    if (keyword is None) == (seller_id is None):
        raise HTTPException(status_code=422, detail="provide exactly one of keyword or seller_id")
    _require_seller(session, seller_id)
    for key, value in changes.items():
        setattr(monitor, key, value)
    # Re-enabling by hand is an explicit "try again": clear the failure state,
    # otherwise one more failure immediately re-trips the auto-disable.
    if changes.get("enabled") is True:
        monitor.consecutive_failures = 0
        monitor.last_error = None
    if channel_ids is not None:
        _set_channels(session, monitor_id, channel_ids)
    session.commit()
    session.refresh(monitor)
    return _public(session, monitor)


@router.delete("/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: int, session: SessionDep) -> None:
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    # No ON DELETE CASCADE on SQLite by default, so the dependents go first.
    for hit in session.exec(select(MonitorHit).where(MonitorHit.monitor_id == monitor_id)).all():
        session.delete(hit)
    for link in session.exec(
        select(MonitorChannel).where(MonitorChannel.monitor_id == monitor_id)
    ).all():
        session.delete(link)
    session.delete(monitor)
    session.commit()


@router.post("/{monitor_id}/run", response_model=CycleResult)
async def run_monitor_now(monitor_id: int, request: Request, session: SessionDep) -> CycleResult:
    """Run one cycle immediately.

    Takes the same semaphore as the scheduler: a manual run must not double the
    request rate against the upstream. Notifications are wired in T4; this
    returns the counts so a user can tell whether a rule is configured sanely.
    """
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    was_baseline = not monitor.baseline_done
    session.expunge(monitor)

    pipeline = request.app.state.pipeline
    started_at = utcnow()
    async with COLLECT_SEMAPHORE:
        try:
            outcome = await run_monitor_cycle(pipeline, monitor)
        except ChallengeError as exc:
            raise HTTPException(
                status_code=503,
                detail="upstream requires verification: import a fresh cookie session",
            ) from exc
        except CollectorError as exc:
            raise HTTPException(status_code=502, detail=f"collection failed: {exc}") from exc

    # Record the run the same way the scheduler does. Without this the manual
    # path persisted items and hits but left last_run_at and last_collector
    # untouched, so the management page showed "路径 —" right after a
    # successful mtop cycle -- and for a DISABLED rule, which the scheduler
    # never touches, that column stayed empty forever.
    #
    # Deliberately success-only: a failure already reaches the user as a 502
    # from this endpoint, and letting a manual probe count toward the
    # auto-disable streak would let someone switch off their own rule by
    # testing it.
    await asyncio.to_thread(
        record_manual_run,
        monitor_id,
        collector=outcome.collector,
        item_count=outcome.collected,
        pages=outcome.pages,
        partial_error=outcome.partial_error,
        started_at=started_at,
    )

    return CycleResult(
        collected=outcome.collected,
        passed=outcome.passed,
        notifiable=len(outcome.hits),
        collector=outcome.collector,
        # A page that failed inside an otherwise working cycle: the user asked
        # for this run, so they get to see that the aperture was short.
        error=outcome.partial_error,
        baseline=was_baseline,
    )
