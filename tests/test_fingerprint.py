"""The collecting end's identity: one source, two paths, nothing leaked.

The cookies come from a human's own browser. Before this, the requests that
carried them announced a *macOS Chrome 131* in *Asia/Shanghai* on a *1440x900*
screen, from two independent hardcoded copies of the same string — one in
`mtop.py`, one in `browser.py`. What is pinned here:

  - a snapshot changes both paths, and they read the same object (a test that
    only checked Playwright would have missed that mtop is the primary path);
  - no snapshot changes nothing at all, because the devtools paste cannot
    produce one and is the documented fallback;
  - a phone's snapshot is stored and deliberately not used;
  - the snapshot survives a restart, and never reaches a response.

Nothing here says anything about whether any of it affects risk control. That
is unverified and cannot be verified from a test.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.collector import browser as browser_mod
from app.collector.browser import BrowserCollector
from app.collector.fingerprint import (
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_UA,
    DEFAULT_VIEWPORT,
    Fingerprint,
    load_fingerprint,
)
from app.collector.mtop import MtopClient
from app.collector.session import UpstreamSession
from app.config import settings
from app.main import app
from tests.test_token_rotation import OK

AUTH = {"Authorization": "Bearer testtoken123"}
GOOFISH = "https://www.goofish.com"

# A real-shaped snapshot: Windows Chrome 141, which is exactly the machine the
# old hardcoded "macOS Chrome 131" contradicted. Every value is distinctive so
# the leak test below can look for each one individually.
WINDOWS_CHROME = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "platform": "Win32",
    "language": "zh-CN",
    "languages": ["zh-CN", "zh", "en-GB", "en"],
    "hardware_concurrency": 24,
    "device_memory": 8,
    "max_touch_points": 0,
    "ua_data": {
        # Chromium's GREASE padding entry first, as it really arrives.
        "brands": [
            {"brand": "Not?A_Brand", "version": "8"},
            {"brand": "Chromium", "version": "141"},
            {"brand": "Google Chrome", "version": "141"},
        ],
        "mobile": False,
        "platform": "Windows",
    },
    "screen_width": 1920,
    "screen_height": 1080,
    "device_pixel_ratio": 1.25,
    "color_depth": 24,
    "time_zone": "Europe/Berlin",
    "locale": "de-DE",
}

# An iPhone. Stored, never applied: every URL and XHR this project drives is
# the PC site.
IPHONE = {
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "platform": "iPhone",
    "language": "zh-CN",
    "languages": ["zh-CN"],
    "max_touch_points": 5,
    "screen_width": 390,
    "screen_height": 844,
    "device_pixel_ratio": 3,
    "time_zone": "Asia/Shanghai",
    "locale": "zh-CN",
}

# The Firefox case: no `userAgentData` at all, no `deviceMemory`. Absent, not
# null — the bookmarklet omits what the browser does not offer.
FIREFOX = {
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "platform": "Linux x86_64",
    "language": "en-US",
    "languages": ["en-US", "en"],
    "hardware_concurrency": 8,
    "max_touch_points": 0,
    "screen_width": 2560,
    "screen_height": 1440,
    "device_pixel_ratio": 1,
    "color_depth": 24,
    "time_zone": "Europe/London",
    "locale": "en-US",
}


# --------------------------------------------------------------------------- #
# No snapshot: today's behaviour, to the byte
# --------------------------------------------------------------------------- #


def test_without_a_snapshot_neither_path_changes_at_all():
    """The devtools paste flow gives no snapshot and is the documented
    fallback. It must not get worse for it — so these are asserted as whole
    dicts, not as "contains": an extra client hint on a request that never
    carried one is itself a new inconsistency.
    """
    fp = Fingerprint()

    assert fp.http_headers() == {"user-agent": DEFAULT_UA}
    assert fp.context_options() == {
        "user_agent": DEFAULT_UA,
        "locale": DEFAULT_LOCALE,
        "timezone_id": DEFAULT_TIMEZONE,
        "viewport": DEFAULT_VIEWPORT,
    }
    assert fp.summary() is None
    assert fp.applied is False


def test_the_defaults_are_the_literals_the_two_collectors_used_to_hold():
    """Pinned as literals so "merge the two copies" cannot quietly become
    "change what we send". The old value was wrong about the machine, but it
    was wrong the same way in both files, and this change is not the place to
    start guessing at a better lie.
    """
    assert DEFAULT_UA == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    assert DEFAULT_LOCALE == "zh-CN"
    assert DEFAULT_TIMEZONE == "Asia/Shanghai"
    assert DEFAULT_VIEWPORT == {"width": 1440, "height": 900}


# --------------------------------------------------------------------------- #
# Header reconstruction
# --------------------------------------------------------------------------- #


def test_sec_ch_ua_is_rebuilt_from_the_brand_array():
    """Given brands, expect the header string. Order is the browser's own:
    Chromium randomises where GREASE sits and how the real brands are ordered,
    so sorting would produce a list no browser ever sends.
    """
    headers = Fingerprint(WINDOWS_CHROME).http_headers()

    assert headers["sec-ch-ua"] == (
        '"Not?A_Brand";v="8", "Chromium";v="141", "Google Chrome";v="141"'
    )
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"Windows"'


def test_a_brand_entry_missing_its_version_is_dropped_not_guessed():
    fp = Fingerprint({"ua_data": {"brands": [{"brand": "Chromium"}, {"version": "9"}]}})
    assert "sec-ch-ua" not in fp.http_headers()


def test_accept_language_carries_the_q_weights_navigator_implies():
    """`navigator.languages` is an ordered list; the header needs the weights
    Chrome derives from that order, or the request says the user reads four
    languages equally well.
    """
    assert Fingerprint(WINDOWS_CHROME).http_headers()["accept-language"] == (
        "zh-CN,zh;q=0.9,en-GB;q=0.8,en;q=0.7"
    )


def test_a_lone_language_still_produces_the_header():
    fp = Fingerprint({"user_agent": "Mozilla/5.0 X", "language": "ja-JP"})
    assert fp.http_headers()["accept-language"] == "ja-JP"


def test_a_browser_without_user_agent_data_sends_no_client_hints():
    """Firefox has no `userAgentData`. Inventing `sec-ch-ua` for it would be a
    Chromium-only header on a Gecko user-agent — the exact class of
    self-contradiction this module exists to remove.
    """
    headers = Fingerprint(FIREFOX).http_headers()

    assert set(headers) == {"user-agent", "accept-language"}
    assert "Firefox/129.0" in headers["user-agent"]


def test_the_playwright_context_follows_the_same_snapshot():
    options = Fingerprint(WINDOWS_CHROME).context_options()

    assert options == {
        "user_agent": WINDOWS_CHROME["user_agent"],
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.25,
    }


# --------------------------------------------------------------------------- #
# One source for both paths
# --------------------------------------------------------------------------- #


def _fake_playwright(captured: dict):
    class FakeContext:
        async def route(self, *_args, **_kwargs) -> None:
            pass

        async def cookies(self) -> list[dict[str, str]]:
            return []

    class FakeBrowser:
        async def new_context(self, **kwargs):
            captured.update(kwargs)
            return FakeContext()

    class FakeChromium:
        async def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def start(self):
            return self

    return lambda: FakePlaywright()


@pytest.mark.asyncio
async def test_both_collectors_announce_the_same_browser(monkeypatch, tmp_path):
    """The acceptance criterion that matters most, because it is the one a
    refactor breaks silently: mtop (httpx, the primary path) and Playwright
    (the fallback) must derive their identity from the same object.

    Asserted through the two real collectors rather than by calling the
    fingerprint twice — the failure being guarded against is one of them
    keeping its own copy.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    fingerprint = Fingerprint(dict(WINDOWS_CHROME))

    context_kwargs: dict = {}
    monkeypatch.setattr(browser_mod, "async_playwright", _fake_playwright(context_kwargs))
    collector = BrowserCollector(UpstreamSession(), fingerprint)
    await collector.start()

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=OK)

    session = UpstreamSession()
    session.adopt({"_m_h5_tk": "tok_1", "cookie2": "x"}, origin="imported")
    client = MtopClient(session, fingerprint)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")
    await client.aclose()

    assert sent[0].headers["user-agent"] == context_kwargs["user_agent"]
    assert sent[0].headers["user-agent"] == WINDOWS_CHROME["user_agent"]
    assert sent[0].headers["sec-ch-ua-platform"] == '"Windows"'
    assert context_kwargs["timezone_id"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_a_snapshot_imported_later_reaches_the_next_mtop_call(monkeypatch):
    """mtop is the primary path, so it reads the shared object per request
    rather than baking headers into the client at construction. Without that,
    a credential import only took effect after a restart — on the one path
    that does most of the traffic.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=OK)

    session = UpstreamSession()
    session.adopt({"_m_h5_tk": "tok_1", "cookie2": "x"}, origin="imported")
    fingerprint = Fingerprint()
    client = MtopClient(session, fingerprint)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")
    assert sent[-1].headers["user-agent"] == DEFAULT_UA
    assert "sec-ch-ua" not in sent[-1].headers

    fingerprint.snapshot = dict(WINDOWS_CHROME)
    await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000001")
    await client.aclose()

    assert sent[-1].headers["user-agent"] == WINDOWS_CHROME["user_agent"]
    assert sent[-1].headers["sec-ch-ua"].endswith('"Google Chrome";v="141"')


# --------------------------------------------------------------------------- #
# Mobile: recorded, not applied
# --------------------------------------------------------------------------- #


def test_a_phone_snapshot_is_kept_and_never_applied():
    """Every URL and XHR name driven here is the PC site. A mobile user-agent
    would swap one inconsistency for a worse one, and `pc.search` may not fire
    at all — so the snapshot is recorded (the panel explains it) and the
    requests keep the built-in defaults.
    """
    fp = Fingerprint(dict(IPHONE))

    assert fp.mobile is True
    assert fp.applied is False
    assert fp.snapshot == IPHONE, "recorded, so the panel can say why it is unused"
    assert fp.summary() is not None
    assert fp.http_headers() == {"user-agent": DEFAULT_UA}
    assert fp.context_options()["timezone_id"] == DEFAULT_TIMEZONE
    assert fp.context_options()["viewport"] == DEFAULT_VIEWPORT
    assert "device_scale_factor" not in fp.context_options()


def test_the_ua_data_flag_is_believed_even_when_the_user_agent_looks_desktop():
    """Chromium's `mobile` hint is authoritative where it exists; a desktop-
    shaped UA string on a tablet is exactly what it is there for.
    """
    fp = Fingerprint({"user_agent": "Mozilla/5.0 (X11; Linux)", "ua_data": {"mobile": True}})
    assert fp.applied is False


@pytest.mark.parametrize("marker", ["Mobile", "Android", "iPhone"])
def test_the_user_agent_markers_are_enough_on_browsers_without_the_hint(marker):
    fp = Fingerprint({"user_agent": f"Mozilla/5.0 ({marker}; something)"})
    assert fp.mobile is True


# --------------------------------------------------------------------------- #
# It has to survive a restart
# --------------------------------------------------------------------------- #


def test_the_snapshot_survives_a_restart(monkeypatch, tmp_path):
    """Without this the identity reverts to the built-in defaults on every
    restart, which is a bigger change than never having imported one: it makes
    the machine we claim to be jump around.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    Fingerprint().adopt(dict(WINDOWS_CHROME))
    reloaded = load_fingerprint()

    assert reloaded.snapshot == WINDOWS_CHROME
    assert reloaded.http_headers() == Fingerprint(WINDOWS_CHROME).http_headers()


def test_the_snapshot_is_not_written_into_playwrights_state_file(monkeypatch, tmp_path):
    """`data/state.json` belongs to Playwright's serializer. A hand-written
    file it silently declines to load looks exactly like a session that was
    never imported.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    Fingerprint().adopt(dict(WINDOWS_CHROME))

    assert not (tmp_path / "state.json").exists()
    assert json.loads((tmp_path / "fingerprint.json").read_text("utf-8")) == WINDOWS_CHROME


def test_an_unreadable_file_falls_back_to_the_defaults(monkeypatch, tmp_path):
    """A corrupt fingerprint must not stop the app: it is not a credential,
    and the defaults are a perfectly good place to be.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "fingerprint.json").write_text("{not json", encoding="utf-8")

    assert load_fingerprint().http_headers() == {"user-agent": DEFAULT_UA}


# --------------------------------------------------------------------------- #
# The summary is the only thing allowed out
# --------------------------------------------------------------------------- #


def test_the_summary_names_the_browser_and_not_the_grease_padding():
    assert Fingerprint(WINDOWS_CHROME).summary() == (
        "Google Chrome 141 · Windows · 1920×1080 · Europe/Berlin"
    )


def test_a_browser_without_client_hints_still_gets_a_label():
    assert Fingerprint(FIREFOX).summary() == (
        "Firefox 129 · Linux x86_64 · 2560×1440 · Europe/London"
    )
    # Safari calls itself `Version/17.5`; "Version 17" is not a browser name.
    assert Fingerprint(IPHONE).summary() == "Safari 17 · iPhone · 390×844 · Asia/Shanghai"


# --------------------------------------------------------------------------- #
# End to end through the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import scheduler
    from app.collector.session import UpstreamSession as Upstream
    from tests.conftest import memory_engine
    from tests.test_session_api import StubBrowser

    monkeypatch.setattr(scheduler, "engine", memory_engine())
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app.state.session = Upstream()
    app.state.browser = StubBrowser()
    app.state.fingerprint = Fingerprint()
    yield TestClient(app), app.state.fingerprint


PASTE = "cookie2=1a2b3c; unb=2218219939144; _tb_token_=e73b; sgcookie=E100abc=="


def post_import(c: TestClient, env=None):
    body = {"cookie_header": PASTE, "origin": GOOFISH}
    if env is not None:
        body["env"] = env
    return c.post("/api/session/cookies", json=body, headers=AUTH)


def test_a_devtools_paste_leaves_the_identity_exactly_where_it_was(client):
    """The paste flow cannot produce a snapshot, and it stays the recommended
    fallback. So an import without one changes nothing about what goes out.
    """
    c, fingerprint = client

    body = post_import(c).json()

    assert body["fingerprint"] is None
    assert body["fingerprint_applied"] is False
    assert fingerprint.snapshot == {}
    assert fingerprint.http_headers() == {"user-agent": DEFAULT_UA}


def test_the_bookmarklets_snapshot_is_adopted_and_persisted(client, tmp_path):
    c, fingerprint = client

    body = post_import(c, WINDOWS_CHROME).json()

    assert body["fingerprint"] == "Google Chrome 141 · Windows · 1920×1080 · Europe/Berlin"
    assert body["fingerprint_applied"] is True
    assert fingerprint.http_headers()["user-agent"] == WINDOWS_CHROME["user_agent"]
    assert load_fingerprint().snapshot == WINDOWS_CHROME


def test_a_mobile_import_says_so_instead_of_pretending(client):
    c, _ = client

    body = post_import(c, IPHONE).json()

    assert body["fingerprint"] is not None, "recorded"
    assert body["fingerprint_applied"] is False, "and not used"


def test_an_absent_field_stays_absent_rather_than_becoming_null(client):
    """`userAgentData` does not exist in Firefox, `deviceMemory` outside
    Chromium. The bookmarklet omits them; the backend must store the omission
    rather than a null it would later have to second-guess.
    """
    c, fingerprint = client

    post_import(c, FIREFOX)

    assert "ua_data" not in fingerprint.snapshot
    assert "device_memory" not in fingerprint.snapshot
    assert None not in fingerprint.snapshot.values()
    assert load_fingerprint().snapshot == FIREFOX


def _walk(obj, prefix=""):
    """Every (path, leaf) pair in a nested structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, f"{prefix}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _walk(value, f"{prefix}[{index}]")
    else:
        yield prefix, obj


def test_no_part_of_the_snapshot_reaches_a_response(client):
    """Checked over EVERY field of the snapshot, name and value, not one
    hand-picked string — M1's lesson is that a check pinned to one known value
    keeps passing while the next field leaks.

    The one sanctioned channel is `fingerprint`, and it is pinned to an exact
    string here so widening it is a failure rather than a silent extra.
    """
    c, _ = client
    post_import(c, WINDOWS_CHROME)

    for response in (c.get("/api/session", headers=AUTH), post_import(c, WINDOWS_CHROME)):
        body = response.json()
        summary = body.pop("fingerprint")
        assert summary == "Google Chrome 141 · Windows · 1920×1080 · Europe/Berlin"

        rest = json.dumps(body, ensure_ascii=False)
        response_keys = {path.rsplit(".", 1)[-1] for path, _ in _walk(body)}
        # Typed, because `0 == False` in Python and `max_touch_points` is 0:
        # an untyped set match would report a leak into `usable`.
        response_values = {(type(v).__name__, v) for _, v in _walk(body)}

        for path, value in _walk(WINDOWS_CHROME):
            name = path.rsplit(".", 1)[-1]
            assert name not in response_keys, f"leaked the field name {path}"
            assert name not in rest, f"leaked the field name {path}"
            if not isinstance(value, bool):
                # A bare `True`/`False` collides with every flag the response
                # legitimately carries, so for `ua_data.mobile` the field-name
                # check above is the one that means anything.
                assert (type(value).__name__, value) not in response_values, (
                    f"leaked the value of {path}"
                )
            if isinstance(value, str) and len(value) >= 4:
                assert value not in rest, f"leaked the value of {path}"


def test_the_published_schema_has_no_place_to_put_a_raw_field():
    """What the page can render is bounded by what the API can send. Asserting
    the schema rather than one rendered page is what makes that true for every
    consumer, including the next one.
    """
    properties = app.openapi()["components"]["schemas"]["SessionState"]["properties"]

    assert "fingerprint" in properties
    for path, _ in _walk(WINDOWS_CHROME):
        assert path.rsplit(".", 1)[-1] not in properties
