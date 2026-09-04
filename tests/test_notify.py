"""Delivery, batching, escaping, and failure isolation.

The properties under test are the ones whose breakage is silent: a muted
channel, a lost message, a 400 that reads like an auth error, or a dead SMTP
server that stops collection entirely.
"""

import json

import httpx
import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.models import MonitorChannel, NotifyChannel, NotifyLog
from app.notify import build_registry, deliver, group_by_reason, log_delivery, redact
from app.notify.base import ConfigError, Notification, describe
from app.notify.email import EmailConfig, _html_body, _plain_body
from app.notify.telegram import MAX_CHARS, TelegramNotifier, render
from app.store import NotifiableHit

pytestmark = pytest.mark.asyncio


def hit(price_cents: int = 299900, **kw) -> NotifiableHit:
    return NotifiableHit(
        **{
            **dict(
                item_id="i1",
                title="iPhone 15 128G",
                price_cents=price_cents,
                previous_price_cents=None,
                reason="new_in_range",
                url="https://www.goofish.com/item?id=i1",
                cover_url=None,
                seller_nick="老王",
                unverified_labels=(),
            ),
            **kw,
        }
    )


@pytest.fixture
def registry():
    return build_registry(httpx.AsyncClient())


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #


async def test_one_cycle_becomes_one_message_per_reason():
    """Ten matches must not be ten messages — that is how a channel gets muted
    or rate-limited. Mixing reasons in one message would force a vague title.
    """
    hits = [hit(item_id=str(i)) for i in range(10)]
    hits += [hit(item_id="d1", reason="price_drop", previous_price_cents=350000)]
    grouped = group_by_reason(hits)
    assert set(grouped) == {"new_in_range", "price_drop"}
    assert len(grouped["new_in_range"]) == 10


async def test_cheapest_first():
    n = Notification(kind="new_in_range", hits=[hit(300000), hit(100000), hit(200000)])
    assert [h.price_cents for h in n.sorted_hits()] == [100000, 200000, 300000]


async def test_title_reflects_the_reason_and_count():
    assert "新命中 2" in Notification(kind="new_in_range", hits=[hit(), hit()]).title
    assert "降价 1" in Notification(kind="price_drop", hits=[hit()]).title
    assert "下架" in Notification(kind="gone", hits=[hit()]).title
    assert "测试" in Notification(kind="test", hits=[hit()]).title


# --------------------------------------------------------------------------- #
# Waived filters must be visible
# --------------------------------------------------------------------------- #


async def test_waived_filter_labels_appear_in_every_channel_body():
    """Without this, "conservatively let it through" degrades into silently
    pushing items that do not meet the stated filters.
    """
    h = hit(unverified_labels=("卖家信用未知", "地区未知"))
    n = Notification(kind="new_in_range", hits=[h])
    assert "卖家信用未知" in describe(h)
    assert "卖家信用未知" in _plain_body(n)
    assert "卖家信用未知" in _html_body(n)
    assert "卖家信用未知" in render(n)


async def test_price_drop_shows_the_previous_price():
    h = hit(250000, reason="price_drop", previous_price_cents=300000)
    assert "↓500.00" in describe(h)
    assert "3000.00" in render(Notification(kind="price_drop", hits=[h]))


# --------------------------------------------------------------------------- #
# Telegram escaping and truncation
# --------------------------------------------------------------------------- #


async def test_seller_written_titles_are_escaped():
    """Real titles contain <, & and emoji. An unescaped one produces a 400 that
    reads exactly like a bad bot token.
    """
    h = hit(title='<b>清仓</b> & "捡漏" 🎉', seller_nick="老王 <VIP>")
    body = render(Notification(kind="new_in_range", hits=[h]))
    assert "&lt;b&gt;" in body
    assert "&amp;" in body
    assert "<b>清仓</b>" not in body
    assert "老王 &lt;VIP&gt;" in body
    # our own markup survives
    assert body.startswith("<b>")


async def test_long_batches_are_truncated_below_the_api_limit():
    hits = [hit(item_id=str(i), title=f"iPhone 15 极长标题 {'很' * 60} {i}") for i in range(200)]
    body = render(Notification(kind="new_in_range", hits=hits))
    assert len(body) <= MAX_CHARS
    assert "另有" in body


async def test_short_batches_are_not_truncated():
    body = render(Notification(kind="new_in_range", hits=[hit(), hit(item_id="i2")]))
    assert "另有" not in body


async def test_telegram_error_never_echoes_the_url():
    """The URL embeds the bot token; a leaked error message leaks the token."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(client)
    with pytest.raises(RuntimeError) as exc:
        await notifier.send(
            Notification(kind="test", hits=[hit()]),
            {"bot_token": "123456:SUPERSECRETTOKEN", "chat_id": "42"},
        )
    assert "SUPERSECRETTOKEN" not in str(exc.value)
    assert "401" in str(exc.value)


async def test_telegram_success_path():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await TelegramNotifier(client).send(
        Notification(kind="test", hits=[hit()]), {"bot_token": "123456:abc", "chat_id": "42"}
    )
    assert seen["body"]["parse_mode"] == "HTML"
    assert seen["body"]["chat_id"] == "42"


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


async def test_bad_channel_config_is_a_config_error_not_a_crash(registry):
    channel = NotifyChannel(kind="telegram", label="x", config=json.dumps({"chat_id": "42"}))
    error = await deliver(registry, channel, Notification(kind="test", hits=[hit()]))
    assert error is not None and "invalid" in error


async def test_unparseable_stored_config_is_reported(registry):
    channel = NotifyChannel(kind="telegram", label="x", config="{not json")
    error = await deliver(registry, channel, Notification(kind="test", hits=[hit()]))
    assert error is not None and "not valid JSON" in error


async def test_unknown_channel_kind_is_reported(registry):
    channel = NotifyChannel(kind="carrier-pigeon", label="x", config="{}")
    error = await deliver(registry, channel, Notification(kind="test", hits=[hit()]))
    assert error is not None and "unknown channel kind" in error


async def test_email_config_requires_a_recipient():
    with pytest.raises(ValidationError):
        EmailConfig.model_validate(
            {
                "smtp_host": "smtp.example.com",
                "username": "u",
                "password": "p",
                "from_addr": "a@b.c",
                "to_addrs": [],
            }
        )


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #


async def test_delivery_failure_is_returned_never_raised(registry):
    """dispatch must survive a dead channel: the poll loop has to outlive an
    unreachable SMTP server indefinitely.
    """

    class Exploding:
        kind = "boom"
        secret_fields = ()

        async def send(self, notification, config):
            raise OSError("connection refused")

    channel = NotifyChannel(kind="boom", label="x", config="{}")
    error = await deliver({"boom": Exploding()}, channel, Notification(kind="test", hits=[hit()]))
    assert error is not None and "connection refused" in error


async def test_config_error_is_distinguished_from_a_delivery_failure(registry):
    class BadConfig:
        kind = "bad"
        secret_fields = ()

        async def send(self, notification, config):
            raise ConfigError("missing token")

    channel = NotifyChannel(kind="bad", label="x", config="{}")
    error = await deliver({"bad": BadConfig()}, channel, Notification(kind="test", hits=[hit()]))
    assert error == "missing token"


async def test_failures_are_logged_to_the_database(session):
    channel = NotifyChannel(kind="email", label="x", config="{}")
    session.add(channel)
    session.commit()
    assert channel.id is not None

    n = Notification(kind="new_in_range", hits=[hit(), hit(item_id="i2")])
    log_delivery(session, channel.id, n, monitor_id=7, error="smtp timeout")
    session.commit()

    entry = session.exec(select(NotifyLog)).one()
    assert entry.ok is False
    assert entry.error == "smtp timeout"
    assert entry.item_count == 2
    assert entry.monitor_id == 7


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #


async def test_secrets_are_redacted_but_presence_is_reported(registry):
    shown = redact("telegram", {"bot_token": "123:abc", "chat_id": "42"}, registry)
    assert shown == {"bot_token": "***", "chat_id": "42"}

    shown = redact("email", {"password": "hunter2", "username": "u"}, registry)
    assert shown["password"] == "***"
    assert shown["username"] == "u"


async def test_an_unset_secret_is_not_shown_as_configured(registry):
    """ "***" for an empty value would claim a token exists when none does."""
    assert redact("telegram", {"bot_token": "", "chat_id": "42"}, registry)["bot_token"] == ""


async def test_channel_binding_selects_only_enabled_channels(session):
    from app.notify import channels_for_monitor

    on = NotifyChannel(kind="email", label="on", config="{}", enabled=True)
    off = NotifyChannel(kind="email", label="off", config="{}", enabled=False)
    session.add(on)
    session.add(off)
    session.commit()
    session.add(MonitorChannel(monitor_id=1, channel_id=on.id))
    session.add(MonitorChannel(monitor_id=1, channel_id=off.id))
    session.commit()

    assert [c.label for c in channels_for_monitor(session, 1)] == ["on"]
