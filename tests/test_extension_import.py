"""The one claim the Chrome extension is allowed to make, pinned.

`tests/test_import_ticket.py` pins the bookmarklet's premise: a script on a
goofish page cannot read `_m_h5_tk` (`.taobao.com`-only), so the session it
imports is unusable and the first cycle afterwards is a slower, page-1-only
browser cycle that mints the token.

The extension holds host permissions for BOTH domains, so `chrome.cookies` can
read that token and the flattened header carries it. This file is the mirror
image of that test: same endpoint, same payload shape, `_m_h5_tk` present, and
the browser is never reached.

That is the entire benefit claimed for the extension. Whether any of this makes
a risk-control challenge less likely is not knowable here and is not asserted.

Nothing about the panel changed for this path -- the extension posts the same
`cookie_header` + `env` body the bookmarklet does, with the bearer token
instead of a ticket, so it also exercises that no new intake was built.
"""

import pytest
from fastapi.testclient import TestClient

from app.collector.fingerprint import Fingerprint
from app.collector.session import UpstreamSession
from app.config import settings
from app.main import app

AUTH = {"Authorization": "Bearer testtoken123"}
GOOFISH = "https://www.goofish.com"

# What `flattenCookies` in `extension/lib/cookies.js` produces: both domains'
# cookies in one header, deduped by name, goofish winning the collisions --
# and `_m_h5_tk`, which only the taobao host permission can reach.
EXTENSION_HEADER = (
    "cookie2=1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d; unb=2218219939144; "
    "_tb_token_=e73b1f5a3e7d1; sgcookie=E100abcdef==; "
    "_m_h5_tk=deadbeefcafe_1757000000000; _m_h5_tk_enc=0123456789abcdef"
)
# The taobao-only half of the same paste, i.e. what the bookmarklet can reach.
BOOKMARKLET_HEADER = "; ".join(
    part for part in EXTENSION_HEADER.split("; ") if not part.startswith("_m_h5_tk=")
)

CHROME_ENV = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0",
    "ua_data": {
        "brands": [{"brand": "Google Chrome", "version": "141"}],
        "mobile": False,
        "platform": "Windows",
    },
    "screen_width": 1920,
    "screen_height": 1080,
    "time_zone": "Asia/Shanghai",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import scheduler
    from tests.conftest import memory_engine
    from tests.test_session_api import StubBrowser

    monkeypatch.setattr(scheduler, "engine", memory_engine())
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app.state.session = UpstreamSession()
    app.state.browser = StubBrowser()
    app.state.fingerprint = Fingerprint()
    yield TestClient(app), app.state.session


def post(c: TestClient, header: str):
    return c.post(
        "/api/session/cookies",
        json={"cookie_header": header, "origin": GOOFISH, "env": CHROME_ENV},
        headers=AUTH,
    )


@pytest.mark.asyncio
async def test_an_import_carrying_the_taobao_token_needs_no_browser_cycle(client):
    """The extension's reason to exist, end to end.

    Pinned against the pipeline rather than against `usable` alone: `usable` is
    an implementation detail, "the next cycle does not have to launch a
    browser" is the thing the user was promised.
    """
    from app.collector.pipeline import Pipeline
    from tests.test_pipeline import StubBrowser as PipelineBrowser
    from tests.test_pipeline import StubMtop

    c, session = client
    body = post(c, EXTENSION_HEADER).json()

    assert body["usable"] is True
    assert session.token == "deadbeefcafe"
    assert session.last_error is None

    mtop, browser = StubMtop(), PipelineBrowser()
    result = await Pipeline(mtop, browser, session).collect_search("iPhone", pages=1)

    assert (mtop.search_calls, browser.search_calls) == (1, 0)
    assert result.items[0].source == "mtop"
    # The browser route reports `pages == 1` because it can only see page one.
    # Here the number means a page was actually fetched over mtop, which the
    # call count above has already established.
    assert result.pages == 1


@pytest.mark.asyncio
async def test_the_same_import_without_that_token_still_goes_through_the_browser(client):
    """The contrast that makes the claim mean something.

    Byte-for-byte the same request minus `_m_h5_tk`, which is exactly what the
    bookmarklet is able to send. Without this the test above would keep passing
    if the pipeline stopped consulting the session at all.
    """
    from app.collector.pipeline import Pipeline
    from tests.test_pipeline import StubBrowser as PipelineBrowser
    from tests.test_pipeline import StubMtop

    c, session = client
    body = post(c, BOOKMARKLET_HEADER).json()

    assert body["usable"] is False
    assert session.last_error == "no _m_h5_tk in adopted cookies"

    mtop, browser = StubMtop(), PipelineBrowser()
    result = await Pipeline(mtop, browser, session).collect_search("iPhone", pages=3)

    assert (mtop.search_calls, browser.search_calls) == (0, 1)
    assert result.pages == 1, "three pages were asked for and the browser can only give one"


def test_the_extension_reuses_the_intake_the_bookmarklet_already_had(client):
    """No second receiving path, and no CORS involved.

    The service worker is not in a page, so it carries the panel's own bearer
    token and never touches the ticket route or `ALLOWED_IMPORT_ORIGINS`. The
    environment snapshot lands through p7's `env` field unchanged.
    """
    c, _ = client
    response = post(c, EXTENSION_HEADER)
    body = response.json()

    assert response.status_code == 200
    # The route answered a bearer-token caller with no CORS headers at all,
    # which is what "the extension needed no CORS change" means.
    assert "access-control-allow-origin" not in response.headers
    assert body["fingerprint"] == "Google Chrome 141 · Windows · 1920×1080 · Asia/Shanghai"
    assert body["fingerprint_applied"] is True
    # Names only, in and out. The extension shows no value either.
    assert body["cookie_names"] == sorted(
        [
            "_m_h5_tk",
            "_m_h5_tk_enc",
            "_tb_token_",
            "cookie2",
            "sgcookie",
            "unb",
        ]
    )
    assert "deadbeefcafe" not in response.text
