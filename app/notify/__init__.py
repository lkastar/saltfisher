"""Channel registry and dispatch.

Two rules shape this module:

1. **A channel failure must never break collection.** Every send is isolated,
   logged, and recorded in NotifyLog; the poll loop survives a dead SMTP
   server indefinitely.
2. **Delivery is recorded only after a successful send.** Writing it first
   loses a crashed message forever; writing it after risks at most one
   duplicate. For an alerting tool that is the correct direction to fail in.
"""

import json
import logging

import httpx
from sqlmodel import Session, select

from app.models import MonitorChannel, NotifyChannel, NotifyLog
from app.notify.base import ConfigError, Notification, Notifier
from app.notify.email import EmailNotifier
from app.notify.telegram import TelegramNotifier
from app.store import NotifiableHit

log = logging.getLogger(__name__)


def build_registry(client: httpx.AsyncClient) -> dict[str, Notifier]:
    """A dict, not a plugin loader. Two channels do not need discovery."""
    return {
        EmailNotifier.kind: EmailNotifier(),
        TelegramNotifier.kind: TelegramNotifier(client),
    }


def redact(kind: str, config: dict, registry: dict[str, Notifier]) -> dict:
    """Replace secret values with a presence marker.

    The management page needs to know WHETHER a secret is set, never what it
    is. Returning the value would leak it into the browser and any log that
    captures responses.
    """
    notifier = registry.get(kind)
    secrets = notifier.secret_fields if notifier else ()
    return {k: ("***" if k in secrets and v else v) for k, v in config.items()}


def channels_for_monitor(session: Session, monitor_id: int) -> list[NotifyChannel]:
    stmt = (
        select(NotifyChannel)
        .join(MonitorChannel, MonitorChannel.channel_id == NotifyChannel.id)  # type: ignore[arg-type]
        .where(MonitorChannel.monitor_id == monitor_id)
        .where(NotifyChannel.enabled)
    )
    return list(session.exec(stmt).all())


def enabled_channels(session: Session) -> list[NotifyChannel]:
    """Every enabled channel.

    Used for watchlist alerts: a watched item is not bound to any rule, so
    there is no rule-level channel list to consult.
    """
    return list(session.exec(select(NotifyChannel).where(NotifyChannel.enabled)).all())


async def deliver(
    registry: dict[str, Notifier],
    channel: NotifyChannel,
    notification: Notification,
) -> str | None:
    """Send to one channel. Returns an error string, or None on success."""
    notifier = registry.get(channel.kind)
    if notifier is None:
        return f"unknown channel kind: {channel.kind}"
    try:
        config = json.loads(channel.config or "{}")
    except json.JSONDecodeError as exc:
        return f"stored config is not valid JSON: {exc}"
    try:
        await notifier.send(notification, config)
    except ConfigError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        return f"{type(exc).__name__}: {exc}"
    return None


def log_delivery(
    session: Session,
    channel_id: int,
    notification: Notification,
    monitor_id: int | None,
    error: str | None,
) -> None:
    session.add(
        NotifyLog(
            channel_id=channel_id,
            kind=notification.kind,
            monitor_id=monitor_id,
            item_count=len(notification.hits),
            ok=error is None,
            error=error,
        )
    )


def group_by_reason(hits: list[NotifiableHit]) -> dict[str, list[NotifiableHit]]:
    """One message per reason, so a subject line stays meaningful.

    A cycle can produce both new matches and price drops; merging them would
    force a vague title, and splitting per item would get the channel muted.
    """
    grouped: dict[str, list[NotifiableHit]] = {}
    for hit in hits:
        grouped.setdefault(hit.reason, []).append(hit)
    return grouped
