"""The email path against a real SMTP conversation.

A mocked smtplib would prove nothing about header construction, MIME
structure, or non-ASCII encoding — the parts that actually break. This runs a
throwaway SMTP server on localhost and asserts on what arrives.

What it deliberately does NOT cover: a specific provider's auth and TLS. That
needs real credentials; see the task notes.
"""

import asyncio
import email
import socket
import threading
from email.message import Message

import pytest

from app.notify.base import Notification
from app.notify.email import EmailNotifier
from app.store import NotifiableHit

# Opens a loopback socket on purpose: a mocked smtplib would prove nothing
# about header construction or MIME structure. Nothing external is contacted.
pytestmark = [pytest.mark.asyncio, pytest.mark.localsocket]


class TinySMTP(threading.Thread):
    """Minimal SMTP sink: enough of the protocol for smtplib to complete."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.raw: str | None = None

    def run(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            f = conn.makefile("rwb")
            f.write(b"220 localhost TinySMTP\r\n")
            f.flush()
            body: list[str] = []
            in_data = False
            while True:
                line = f.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                if in_data:
                    if text.strip() == ".":
                        in_data = False
                        self.raw = "".join(body)
                        f.write(b"250 OK\r\n")
                        f.flush()
                        continue
                    body.append(text)
                    continue
                verb = text.split(" ", 1)[0].strip().upper()
                if verb == "EHLO":
                    f.write(b"250-localhost\r\n250 SIZE 10485760\r\n")
                elif verb == "DATA":
                    in_data = True
                    f.write(b"354 send data\r\n")
                elif verb == "QUIT":
                    f.write(b"221 bye\r\n")
                    f.flush()
                    break
                else:
                    f.write(b"250 OK\r\n")
                f.flush()
        self.sock.close()


def hit(**kw) -> NotifiableHit:
    return NotifiableHit(
        **{
            **dict(
                item_id="i1",
                title="苹果 iPhone 15 <128G> & 全新",
                price_cents=299900,
                previous_price_cents=329900,
                reason="price_drop",
                url="https://www.goofish.com/item?id=i1",
                cover_url=None,
                seller_nick="老王 & Co",
                unverified_labels=("卖家信用未知",),
            ),
            **kw,
        }
    )


@pytest.fixture
def smtp_server():
    server = TinySMTP()
    server.start()
    yield server


async def test_email_reaches_an_smtp_server_intact(smtp_server):
    notification = Notification(kind="price_drop", hits=[hit()], monitor_name="iPhone15 捡漏")
    await EmailNotifier().send(
        notification,
        {
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_server.port,
            "username": "sender@example.com",
            "password": "",
            "from_addr": "sender@example.com",
            "to_addrs": ["target@example.com"],
            "use_ssl": False,
            "use_starttls": False,
        },
    )
    await asyncio.sleep(0.2)
    assert smtp_server.raw, "server received nothing"

    parsed: Message = email.message_from_string(smtp_server.raw)
    # Subject is RFC2047-encoded because it is Chinese; decode before asserting.
    subject = str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
    assert "降价" in subject and "iPhone15 捡漏" in subject
    assert parsed["To"] == "target@example.com"
    assert parsed.is_multipart()

    parts = {
        p.get_content_type(): p.get_payload(decode=True).decode()
        for p in parsed.walk()
        if p.get_payload(decode=True)
    }
    plain, html = parts["text/plain"], parts["text/html"]

    # the drop is visible in both alternatives
    assert "2999.00" in plain and "3299.00" in html
    # the waived-filter label survives the whole pipeline
    assert "卖家信用未知" in plain and "卖家信用未知" in html
    # seller-written angle brackets are escaped in HTML but literal in text
    assert "&lt;128G&gt;" in html
    assert "<128G>" in plain
    assert "&amp;" in html
    assert "https://www.goofish.com/item?id=i1" in plain


async def test_missing_smtp_host_is_a_config_error():
    from app.notify.base import ConfigError

    with pytest.raises(ConfigError):
        await EmailNotifier().send(
            Notification(kind="test", hits=[hit()]), {"username": "u", "to_addrs": ["a@b.c"]}
        )
