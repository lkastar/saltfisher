"""Credential import — the endpoint that makes deployment possible.

A headless container cannot solve a slider, so long-lived login state has to
arrive as a paste from a human's browser. Everything here is about that paste
being parsed correctly and never echoed back.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.session import parse_cookie_header
from app.collector.session import UpstreamSession
from app.main import app

AUTH = {"Authorization": "Bearer testtoken123"}

# Shape of a real paste: base64-ish values with `=` padding inside them.
REAL_PASTE = (
    "cna=abc123; t=9f8e7d6c; _tb_token_=e73b1f5a3e7d1; "
    "cookie2=1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d; unb=2218219939144; "
    "sgcookie=E100abcdef==; _m_h5_tk=deadbeefcafe_1757000000000; "
    "_m_h5_tk_enc=0123456789abcdef"
)


class StubBrowser:
    def __init__(self) -> None:
        self.imported: list[dict[str, str]] = []
        self.cleared = 0

    async def import_cookies(self, cookies: dict[str, str]) -> None:
        self.imported.append(cookies)

    async def clear_cookies(self) -> None:
        self.cleared += 1


@pytest.fixture
def client():
    app.state.session = UpstreamSession()
    app.state.browser = StubBrowser()
    yield TestClient(app), app.state.session, app.state.browser


def test_auth_required(client):
    c, _, _ = client
    assert c.get("/api/session").status_code == 401
    assert c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}).status_code == 401


def test_values_containing_equals_survive_parsing():
    """Cookie values are base64-ish and carry `=` padding. Splitting on every
    `=` truncates them into something that looks imported and authenticates as
    nobody.
    """
    parsed = parse_cookie_header("sgcookie=E100abcdef==; unb=123")
    assert parsed["sgcookie"] == "E100abcdef=="
    assert parsed["unb"] == "123"


def test_parsing_tolerates_the_shapes_a_paste_actually_has():
    assert parse_cookie_header("a=1;b=2") == {"a": "1", "b": "2"}
    assert parse_cookie_header("  a=1 ;  b=2  ") == {"a": "1", "b": "2"}
    assert parse_cookie_header("a=1\nb=2") == {"a": "1", "b": "2"}
    assert parse_cookie_header("a=1;; ;b=2") == {"a": "1", "b": "2"}
    # No value, no name, and a bare flag are all dropped rather than stored
    # as an empty credential.
    assert parse_cookie_header("a=;=2;flag;b=2") == {"b": "2"}
    assert parse_cookie_header("") == {}


def test_import_adopts_the_session_and_persists_it(client):
    c, session, browser = client
    r = c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)
    assert r.status_code == 200

    assert browser.imported and browser.imported[0]["unb"] == "2218219939144"
    assert session.usable is True
    assert session.token == "deadbeefcafe"
    assert session.origin == "https://www.goofish.com"


def test_the_response_carries_names_and_never_values(client):
    """The whole point of the endpoint is accepting a secret. Echoing it back
    would put it in the browser's network log, the DOM, and any screenshot.
    """
    c, _, _ = client
    r = c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)
    body = r.text

    assert "cookie2" in r.json()["cookie_names"]
    for secret in (
        "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
        "2218219939144",
        "E100abcdef==",
        "deadbeefcafe_1757000000000",
        "e73b1f5a3e7d1",
    ):
        assert secret not in body, f"leaked {secret}"


def test_a_paste_with_no_cookies_is_400_not_500(client):
    c, _, _ = client
    r = c.post("/api/session/cookies", json={"cookie_header": "总之我什么都没复制"}, headers=AUTH)
    assert r.status_code == 400
    assert "cookie" in r.json()["detail"]


def test_a_paste_from_the_wrong_tab_says_so(client):
    """Cookies parsed but no login field: the user copied a header from some
    other site. A 200 here would report success and then collect nothing.
    """
    c, _, browser = client
    r = c.post(
        "/api/session/cookies",
        json={"cookie_header": "sessionid=abc; csrftoken=def"},
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "登录态" in r.json()["detail"]
    assert browser.imported == []


def test_import_without_the_short_lived_token_still_reports_why(client):
    """Login cookies without `_m_h5_tk` is a real paste: the collector can get
    a token itself. The session says what is missing rather than claiming to
    be healthy.
    """
    c, session, _ = client
    r = c.post(
        "/api/session/cookies",
        json={"cookie_header": "cookie2=abc; unb=999"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert session.usable is False
    assert r.json()["last_error"] == "no _m_h5_tk in adopted cookies"


def test_clear_forgets_the_session_in_memory_and_on_disk(client):
    c, session, browser = client
    c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)

    r = c.delete("/api/session/cookies", headers=AUTH)
    assert r.status_code == 200
    assert browser.cleared == 1
    assert session.cookies == {}
    assert session.usable is False
    assert r.json()["cookie_names"] == []


def test_clearing_also_reports_the_challenge_state_is_gone(client):
    c, session, _ = client
    c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)
    session.mark_challenged("mtop.taobao.idle.pc.detail", "RGV587_ERROR")
    assert c.get("/api/session", headers=AUTH).json()["needs_verification"] is True

    body = c.delete("/api/session/cookies", headers=AUTH).json()
    assert body["needs_verification"] is False
    assert body["challenged_apis"] == []


@pytest.mark.asyncio
async def test_a_restart_keeps_the_session_it_persisted():
    """Found in T8: cookies survived a restart inside the browser context but
    not in UpstreamSession, so the cheap mtop path came up unusable and the
    panel asked the user to import credentials they had already imported.

    Drives the real `restore_session` rather than re-implementing it, so
    deleting the fix fails this test.
    """
    from app.collector.browser import BrowserCollector

    stored = {"cookie2": "abc", "unb": "999", "_m_h5_tk": "deadbeefcafe_1757000000000"}

    class FakeContext:
        async def cookies(self):
            return [{"name": k, "value": v} for k, v in stored.items()]

    session = UpstreamSession()
    collector = BrowserCollector(session)
    collector._ctx = FakeContext()  # type: ignore[assignment]

    assert session.usable is False
    assert await collector.restore_session() == 3
    assert session.usable is True
    assert session.token == "deadbeefcafe"
    assert sorted(session.cookies) == ["_m_h5_tk", "cookie2", "unb"]


@pytest.mark.asyncio
async def test_a_first_run_with_no_stored_cookies_adopts_nothing():
    """An empty context must not produce a session that claims to be set up."""
    from app.collector.browser import BrowserCollector

    class EmptyContext:
        async def cookies(self):
            return []

    session = UpstreamSession()
    collector = BrowserCollector(session)
    collector._ctx = EmptyContext()  # type: ignore[assignment]

    assert await collector.restore_session() == 0
    assert session.usable is False
    assert session.origin == "none"


def test_transport_libraries_cannot_log_headers():
    """Found in T8: at DEBUG level httpcore printed whole response headers,
    including `Set-Cookie: _m_h5_tk=<live token>`, into the log file.

    Silencing httpx alone was not enough -- httpcore underneath is what logs
    the headers. Asserted rather than trusted, because the failure is invisible
    until someone reads a log with a real session in it.
    """
    import logging

    import app.main  # noqa: F401  (import applies the logging configuration)

    for name in ("httpx", "httpcore", "hpack", "h11"):
        logger = logging.getLogger(name)
        assert logger.level >= logging.WARNING, f"{name} may log headers at {logger.level}"
        assert not logger.isEnabledFor(logging.DEBUG), f"{name} is still DEBUG-enabled"
