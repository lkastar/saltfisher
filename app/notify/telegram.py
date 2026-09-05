"""Telegram bot delivery."""

import logging
from html import escape
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.notify.base import ConfigError, Notification, yuan

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects the whole message above this, so it must be truncated by us
# rather than discovered as a 400 that looks like a token problem.
MAX_CHARS = 4096
TRUNCATION_MARGIN = 120


class TelegramConfig(BaseModel):
    bot_token: str = Field(min_length=10)
    chat_id: str = Field(min_length=1)


class TelegramNotifier:
    kind: ClassVar[str] = "telegram"
    secret_fields: ClassVar[tuple[str, ...]] = ("bot_token",)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, notification: Notification, config: dict) -> None:
        try:
            cfg = TelegramConfig.model_validate(config)
        except ValidationError as exc:
            raise ConfigError(f"telegram config invalid: {exc.error_count()} problem(s)") from exc

        response = await self._client.post(
            API.format(token=cfg.bot_token),
            json={
                "chat_id": cfg.chat_id,
                "text": render(notification),
                "parse_mode": "HTML",
                # The preview IS the item photo, which is most of the value.
                "disable_web_page_preview": False,
            },
        )
        if response.status_code != 200:
            # Never echo the URL: it contains the bot token.
            detail = response.text[:200]
            raise RuntimeError(f"telegram sendMessage returned {response.status_code}: {detail}")


def render(notification: Notification) -> str:
    """Build the HTML message, truncating to Telegram's limit.

    Every seller-authored string is escaped: real titles contain `<`, `&` and
    emoji, and an unescaped one produces a 400 that reads like an auth error.
    """
    header = f"<b>{escape(notification.title)}</b>"
    lines: list[str] = [header]
    if notification.body:
        lines.append(escape(notification.body))
    hits = notification.sorted_hits()

    for index, hit in enumerate(hits):
        line = _render_hit(hit)
        projected = len("\n".join([*lines, line]))
        if projected > MAX_CHARS - TRUNCATION_MARGIN:
            remaining = len(hits) - index
            lines.append(f"… 另有 {remaining} 条未列出")
            break
        lines.append(line)
    return "\n".join(lines)


def _render_hit(hit) -> str:  # noqa: ANN001 - NotifiableHit, avoids a cycle
    price = f"<b>¥{yuan(hit.price_cents)}</b>"
    if hit.previous_price_cents and hit.previous_price_cents > hit.price_cents:
        price += f" <s>¥{yuan(hit.previous_price_cents)}</s>"
    parts = [price, f'<a href="{escape(hit.url)}">{escape(hit.title)}</a>']
    if hit.seller_nick:
        parts.append(f"<i>{escape(hit.seller_nick)}</i>")
    if hit.unverified_labels:
        parts.append(f"⚠️ {escape('/'.join(hit.unverified_labels))}")
    return " · ".join(parts)
