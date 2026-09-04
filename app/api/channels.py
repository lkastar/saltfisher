"""Notification channel endpoints."""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import select

from app.db import SessionDep
from app.models import MonitorChannel, NotifyChannel, NotifyLog
from app.notify import deliver, log_delivery, redact
from app.notify.base import Notification
from app.schemas import (
    ChannelCreate,
    ChannelPublic,
    ChannelUpdate,
    NotifyLogPublic,
    TestSendResult,
)
from app.store import NotifiableHit, item_url

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["channels"])


def _public(channel: NotifyChannel, registry: dict) -> ChannelPublic:
    assert channel.id is not None
    try:
        stored = json.loads(channel.config or "{}")
    except json.JSONDecodeError:
        stored = {}
    return ChannelPublic(
        id=channel.id,
        kind=channel.kind,
        label=channel.label,
        config=redact(channel.kind, stored, registry),
        enabled=channel.enabled,
        created_at=channel.created_at,
    )


@router.get("/channels", response_model=list[ChannelPublic])
def list_channels(session: SessionDep, request: Request) -> list[ChannelPublic]:
    registry = request.app.state.notify_registry
    channels = session.exec(select(NotifyChannel).order_by(NotifyChannel.id)).all()  # type: ignore[arg-type]
    return [_public(c, registry) for c in channels]


@router.post("/channels", response_model=ChannelPublic, status_code=201)
def create_channel(payload: ChannelCreate, session: SessionDep, request: Request) -> ChannelPublic:
    channel = NotifyChannel(
        kind=payload.kind,
        label=payload.label,
        config=json.dumps(payload.config, ensure_ascii=False),
        enabled=payload.enabled,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return _public(channel, request.app.state.notify_registry)


@router.patch("/channels/{channel_id}", response_model=ChannelPublic)
def update_channel(
    channel_id: int, payload: ChannelUpdate, session: SessionDep, request: Request
) -> ChannelPublic:
    channel = session.get(NotifyChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    changes = payload.model_dump(exclude_unset=True)
    if "config" in changes and changes["config"] is not None:
        # A redacted value coming back from the page must not overwrite the
        # real secret with the literal "***".
        stored = json.loads(channel.config or "{}")
        merged = {**stored}
        for key, value in changes["config"].items():
            if value == "***":
                continue
            merged[key] = value
        channel.config = json.dumps(merged, ensure_ascii=False)
        changes.pop("config")
    for key, value in changes.items():
        setattr(channel, key, value)
    session.commit()
    session.refresh(channel)
    return _public(channel, request.app.state.notify_registry)


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int, session: SessionDep) -> None:
    channel = session.get(NotifyChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    for link in session.exec(
        select(MonitorChannel).where(MonitorChannel.channel_id == channel_id)
    ).all():
        session.delete(link)
    for entry in session.exec(select(NotifyLog).where(NotifyLog.channel_id == channel_id)).all():
        session.delete(entry)
    session.delete(channel)
    session.commit()


@router.post("/channels/{channel_id}/test", response_model=TestSendResult)
async def test_channel(channel_id: int, session: SessionDep, request: Request) -> TestSendResult:
    """Send a sample message through the real delivery path.

    Deliberately the same `deliver()` used by the scheduler: a test that took a
    shortcut would pass while real notifications fail.
    """
    channel = session.get(NotifyChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    registry = request.app.state.notify_registry
    notification = Notification(kind="test", hits=[_sample_hit()])
    error = await deliver(registry, channel, notification)
    log_delivery(session, channel_id, notification, None, error)
    session.commit()
    return TestSendResult(ok=error is None, error=error)


def _sample_hit() -> NotifiableHit:
    """A realistic sample: it carries a waived-filter label so the user can see
    what that looks like before it matters.
    """
    return NotifiableHit(
        item_id="0",
        title="示例商品 iPhone 15 128G <测试>",
        price_cents=299900,
        previous_price_cents=329900,
        reason="test",
        url=item_url("0"),
        cover_url=None,
        seller_nick="示例卖家 & Co",
        unverified_labels=("卖家信用未知",),
    )


@router.get("/notify-logs", response_model=list[NotifyLogPublic])
def list_notify_logs(
    session: SessionDep,
    limit: Annotated[int, Query(le=200)] = 50,
    channel_id: int | None = None,
) -> list[NotifyLog]:
    stmt = select(NotifyLog).order_by(NotifyLog.sent_at.desc()).limit(limit)  # type: ignore[attr-defined]
    if channel_id is not None:
        stmt = stmt.where(NotifyLog.channel_id == channel_id)
    return list(session.exec(stmt).all())
