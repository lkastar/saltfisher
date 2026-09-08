"""The bookmarklet path: a one-time ticket instead of the panel's API token.

The bookmarklet executes inside a page goofish serves, so everything it sends
is readable by that page. What it carries therefore has to be worth stealing as
little as possible: one cookie import, once, within ten minutes.

Everything here pins a decision that is invisible from the code alone -- that a
goofish-only paste passes validation (the documented trap), that a ticket opens
no other door, and that no cookie value leaves the process.
"""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from app.api.session import TICKET_HEADER, parse_cookie_header
from app.collector.fingerprint import Fingerprint
from app.collector.session import UpstreamSession
from app.main import app

AUTH = {"Authorization": "Bearer testtoken123"}
GOOFISH = "https://www.goofish.com"

# Only the cookies a script on a goofish page can actually read: no `_m_h5_tk`,
# which measurement found lives on `.taobao.com` alone. Values are shaped like
# the real ones (base64-ish, `=` padding) so the leak test has something to
# match on.
GOOFISH_ONLY_PASTE = (
    "cookie2=1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d; unb=2218219939144; "
    "_tb_token_=e73b1f5a3e7d1; sgcookie=E100abcdefGHIJ==; "
    "x5sec=7b22617365727665723b32223a22ff00; cna=abc123XYZ"
)


class StubBrowser:
    def __init__(self) -> None:
        self.imported: list[dict[str, str]] = []

    async def import_cookies(self, cookies: dict[str, str]) -> None:
        self.imported.append(cookies)

    async def clear_cookies(self) -> None:  # pragma: no cover - not exercised here
        pass


@pytest.fixture
def client(monkeypatch):
    from app import scheduler
    from tests.conftest import memory_engine

    monkeypatch.setattr(scheduler, "engine", memory_engine())
    app.state.session = UpstreamSession()
    app.state.browser = StubBrowser()
    app.state.fingerprint = Fingerprint()
    app.state.import_tickets = {}
    yield TestClient(app), app.state.session


def mint(c: TestClient) -> str:
    r = c.post("/api/session/import-ticket", headers=AUTH)
    assert r.status_code == 200
    return r.json()["ticket"]


def import_with(c: TestClient, ticket: str, paste: str = GOOFISH_ONLY_PASTE):
    return c.post(
        "/api/session/cookies",
        json={"cookie_header": paste, "origin": GOOFISH},
        headers={TICKET_HEADER: ticket},
    )


def test_a_ticket_works_exactly_once(client):
    """Single use is the whole security argument: the ticket is exposed to a
    third-party page the moment it runs, so its value has to be spent by then.
    """
    c, _ = client
    ticket = mint(c)

    assert import_with(c, ticket).status_code == 200

    again = import_with(c, ticket)
    assert again.status_code == 401
    assert "用过" in again.json()["detail"]


def test_a_ticket_is_spent_even_when_the_import_fails(client):
    """It authorises one attempt, not one success -- otherwise a stolen ticket
    is retryable forever by sending garbage first.
    """
    c, _ = client
    ticket = mint(c)

    assert import_with(c, ticket, paste="总之我什么都没复制").status_code == 400
    assert import_with(c, ticket).status_code == 401


def test_an_expired_ticket_says_expired_and_not_invalid(client):
    """A user who dragged yesterday's bookmarklet has to be told which of the
    two happened: "expired" means click the button again, "invalid" means
    something is wrong with where that bookmarklet came from.
    """
    c, _ = client
    ticket = mint(c)
    app.state.import_tickets[ticket] = time.monotonic() - 1

    r = import_with(c, ticket)
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "过期" in detail
    assert "无效" not in detail

    invented = import_with(c, "not-a-real-ticket-at-all")
    assert invented.status_code == 401
    assert "无效" in invented.json()["detail"]


def test_a_ticket_cannot_authenticate_anything_else(client):
    """A ticket buys one cookie import. Not the item list, not the LLM keys,
    not another ticket.
    """
    c, _ = client
    ticket = mint(c)
    headers = {TICKET_HEADER: ticket}

    for path in (
        "/api/whoami",
        "/api/session",
        "/api/monitors",
        "/api/llm/endpoints",
    ):
        assert c.get(path, headers=headers).status_code == 401, path
    assert c.post("/api/session/import-ticket", headers=headers).status_code == 401
    assert c.delete("/api/session/cookies", headers=headers).status_code == 401

    # ...and none of that spent it.
    assert import_with(c, ticket).status_code == 200


def test_every_session_route_still_requires_something(client):
    """The session router carries no blanket auth dependency any more (the
    import route has a second door), so a new route added there could silently
    ship unauthenticated. This is the guard for that.
    """
    c, _ = client
    # Read off the published schema rather than `app.routes`: included routers
    # are wrapped and their paths are not reachable from there.
    paths = {p for p in app.openapi()["paths"] if p.startswith("/api/session")}
    assert paths == {"/api/session", "/api/session/cookies", "/api/session/import-ticket"}

    assert c.get("/api/session").status_code == 401
    assert c.post("/api/session/import-ticket").status_code == 401
    assert c.delete("/api/session/cookies").status_code == 401
    assert c.post("/api/session/cookies", json={"cookie_header": "a=1"}).status_code == 401


def test_a_goofish_only_import_passes_validation_and_proves_nothing(client):
    """The documented trap, pinned so nobody "fixes" it into a rejection.

    All four `LOGIN_COOKIE_NAMES` live on `.goofish.com`, so a bookmarklet
    import always passes validation -- with no `_m_h5_tk`, which arrives later
    via the browser fallback (see the test at the bottom of this file).
    Passing therefore says nothing about whether collection works:
    `docs/operations.md`, 判定成功看行为，不看 cookie 名单.
    """
    c, session = client
    r = import_with(c, mint(c))
    assert r.status_code == 200

    body = r.json()
    assert "_m_h5_tk" not in body["cookie_names"]
    assert body["usable"] is False, "no signing token yet; the browser path fetches one"
    assert body["proven"] is False, "and nothing upstream has succeeded"
    assert body["last_success_at"] is None
    assert session.proven is False


def test_no_cookie_value_reaches_a_response_or_a_log(client, caplog):
    """Asserted over every value in the paste rather than one hand-picked
    string: M1's lesson is that a check pinned to one known value passes while
    the next field leaks. The ticket is checked the same way -- it is a
    credential too.
    """
    c, _ = client
    caplog.set_level(logging.DEBUG)
    ticket = mint(c)
    body = import_with(c, ticket).text

    haystack = body + "\n" + "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for name, value in parse_cookie_header(GOOFISH_ONLY_PASTE).items():
        assert value not in haystack, f"leaked the value of {name}"
    assert ticket not in haystack, "leaked the import ticket"
    # The names are fine and are the point of the response.
    assert "cookie2" in body


def test_the_bookmarklets_origin_may_preflight_and_post(client):
    """Without both, the bookmarklet cannot read its own answer and reports a
    bare network error for everything, including "your ticket expired".
    """
    c, _ = client
    ticket = mint(c)
    origin = {"Origin": GOOFISH}

    pre = c.options(
        "/api/session/cookies",
        headers={
            **origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": TICKET_HEADER,
        },
    )
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == GOOFISH
    assert TICKET_HEADER in pre.headers["access-control-allow-headers"]
    # goofish is public and the panel is on a private address, so this is a
    # Private Network Access request; without the grant Chrome drops the call
    # before the handler ever runs.
    assert pre.headers["access-control-allow-private-network"] == "true"

    r = c.post(
        "/api/session/cookies",
        json={"cookie_header": GOOFISH_ONLY_PASTE, "origin": GOOFISH},
        headers={**origin, TICKET_HEADER: ticket},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == GOOFISH


def test_a_failing_import_still_carries_the_cors_header(client):
    """The failure messages are the ones the user needs most."""
    c, _ = client
    r = c.post(
        "/api/session/cookies",
        json={"cookie_header": GOOFISH_ONLY_PASTE, "origin": GOOFISH},
        headers={"Origin": GOOFISH, TICKET_HEADER: "expired-or-invented"},
    )
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == GOOFISH


def test_any_other_origin_gets_no_cors_grant(client):
    """No header is how CORS refuses: the browser will not hand the response to
    the page that asked. Checked for the preflight too, which is where a real
    cross-origin attempt dies first.
    """
    c, _ = client
    evil = {"Origin": "https://evil.example"}

    pre = c.options(
        "/api/session/cookies",
        headers={**evil, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in pre.headers

    r = c.post(
        "/api/session/cookies",
        json={"cookie_header": GOOFISH_ONLY_PASTE, "origin": GOOFISH},
        headers={**evil, TICKET_HEADER: mint(c)},
    )
    assert "access-control-allow-origin" not in r.headers


def test_the_allowlist_covers_only_goofish(client):
    """Scoped to the import route as well as to the origin: the panel's other
    routes must stay same-origin even for goofish itself.
    """
    c, _ = client
    r = c.get("/api/session", headers={"Origin": GOOFISH, **AUTH})
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.asyncio
async def test_a_tokenless_import_routes_to_the_browser_and_recovers_there():
    """The whole premise of the bookmarklet, pinned.

    A script on a goofish page cannot read `_m_h5_tk` -- it lives on
    `.taobao.com` (`research/cookie-domains.md`). That does NOT come back
    through token rotation: `MtopClient` refuses to sign without a token at all
    (`mtop.py`, "no _m_h5_tk: session not established"), and rotation only
    replaces one that already exists.

    What recovers it is the browser fallback. `Pipeline` routes an unusable
    session to `BrowserCollector`, whose page load lets goofish's own JS mint a
    token, and `_adopt_session` hands it back. So the first cycle after a
    bookmarklet import is a slower page-1-only browser cycle, and every cycle
    after it is the cheap path again.

    If this ever stops being true the bookmarklet becomes a trap: it would
    report success and then never collect.
    """
    from app.collector.pipeline import Pipeline
    from tests.test_pipeline import StubBrowser as PipelineBrowser
    from tests.test_pipeline import StubMtop

    session = UpstreamSession()
    session.adopt(parse_cookie_header(GOOFISH_ONLY_PASTE), "https://www.goofish.com")
    assert session.usable is False
    assert session.last_error == "no _m_h5_tk in adopted cookies"

    mtop, browser = StubMtop(), PipelineBrowser()
    result = await Pipeline(mtop, browser, session).collect_search("iPhone")
    assert (mtop.search_calls, browser.search_calls) == (0, 1), "must not sign without a token"
    assert result.pages == 1, "browser covers page 1 only, and the aperture must say so"

    # What the browser does at the end of a real search: hand its cookies back.
    session.adopt({**session.cookies, "_m_h5_tk": "tok_from_browser_page"}, "browser")
    assert session.usable is True
    assert session.last_error is None
