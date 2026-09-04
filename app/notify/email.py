"""Email delivery over SMTP."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.notify.base import ConfigError, Notification, describe, yuan

log = logging.getLogger(__name__)


class EmailConfig(BaseModel):
    smtp_host: str = Field(min_length=1)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    username: str = Field(min_length=1)
    # Empty means "relay does not authenticate" — valid for a LAN relay.
    password: str = ""
    from_addr: str = Field(min_length=3)
    to_addrs: list[str] = Field(min_length=1)
    use_ssl: bool = True
    # A LAN relay without TLS is a real deployment (a NAS forwarding mail),
    # and it is the only way to run this path against a local server.
    use_starttls: bool = True


class EmailNotifier:
    kind: ClassVar[str] = "email"
    secret_fields: ClassVar[tuple[str, ...]] = ("password",)

    async def send(self, notification: Notification, config: dict) -> None:
        try:
            cfg = EmailConfig.model_validate(config)
        except ValidationError as exc:
            raise ConfigError(f"email config invalid: {exc.error_count()} problem(s)") from exc
        # smtplib is blocking; a plain call here would stall the event loop and
        # with it the API. One call site does not justify an async SMTP dep.
        await asyncio.to_thread(_sync_send_email, cfg, notification)


def _sync_send_email(cfg: EmailConfig, notification: Notification) -> None:
    message = EmailMessage()
    message["Subject"] = notification.title
    message["From"] = cfg.from_addr
    message["To"] = ", ".join(cfg.to_addrs)
    message.set_content(_plain_body(notification))
    message.add_alternative(_html_body(notification), subtype="html")

    if cfg.use_ssl:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20) as smtp:
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as smtp:
            if cfg.use_starttls:
                smtp.starttls()
            if cfg.password:
                smtp.login(cfg.username, cfg.password)
            smtp.send_message(message)


def _plain_body(notification: Notification) -> str:
    lines = [notification.title, ""]
    for hit in notification.sorted_hits():
        lines.append(describe(hit))
        lines.append(f"  {hit.url}")
    lines += ["", "— saltfish-digger"]
    return "\n".join(lines)


def _html_body(notification: Notification) -> str:
    from html import escape

    rows = []
    for hit in notification.sorted_hits():
        price = f"¥{yuan(hit.price_cents)}"
        if hit.previous_price_cents and hit.previous_price_cents > hit.price_cents:
            price += f' <s style="color:#999">¥{yuan(hit.previous_price_cents)}</s>'
        labels = (
            f'<div style="color:#c60;font-size:12px">'
            f"{escape('/'.join(hit.unverified_labels))}</div>"
            if hit.unverified_labels
            else ""
        )
        # referrerpolicy is not honoured by mail clients, so a blocked image
        # must simply not break the row — hence the text-first layout.
        rows.append(
            f'<tr><td style="padding:8px 12px;white-space:nowrap;font-weight:600">{price}</td>'
            f'<td style="padding:8px 12px"><a href="{escape(hit.url)}">{escape(hit.title)}</a>'
            f'<div style="color:#666;font-size:12px">{escape(hit.seller_nick)}</div>{labels}</td>'
            f"</tr>"
        )
    return (
        f'<div style="font-family:system-ui,sans-serif">'
        f"<h3>{escape(notification.title)}</h3>"
        f'<table style="border-collapse:collapse">{"".join(rows)}</table>'
        f'<p style="color:#999;font-size:12px">— saltfish-digger</p></div>'
    )
