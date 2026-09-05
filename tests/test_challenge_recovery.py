"""Risk control challenged the session — does anyone find out, and what heals it?

Before this, a challenge did exactly two things: stamped `Monitor.last_error`
and switched the rule off. Both are only visible to someone who happens to open
the panel, so in practice collection stopped and stayed stopped. The properties
below are the ones whose breakage restores that silence:

  - the alert is sent at all, to every enabled channel, and exactly once per
    episode (two loops times N rules share one challenge);
  - a dead channel cannot take the cycle down with it;
  - an import puts back the rules the challenge took away, and NOTHING else;
  - none of it ever carries a credential.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import scheduler
from app.collector.base import ChallengeError, CollectorError
from app.collector.session import UpstreamSession
from app.main import app
from app.models import Monitor, MonitorChannel, NotifyChannel, NotifyLog
from app.notify.base import Notification, challenge_body
from app.notify.email import _html_body, _plain_body
from app.notify.telegram import render
from app.scheduler import CycleOutcome
from tests.conftest import memory_engine
from tests.test_session_api import AUTH, REAL_PASTE, StubBrowser

pytestmark = pytest.mark.asyncio

SEARCH_API = "mtop.taobao.idle.web.search"


class StubNotifier:
    """Records what it was handed, or blows up on demand.

    `kind` has to match NotifyChannel.kind for the registry lookup in
    `deliver` to find it.
    """

    secret_fields = ()

    def __init__(self, kind: str, *, boom: bool = False) -> None:
        self.kind = kind
        self.boom = boom
        self.sent: list[Notification] = []

    async def send(self, notification: Notification, config: dict) -> None:
        if self.boom:
            raise RuntimeError("SMTP is on fire")
        self.sent.append(notification)


class FakePipeline:
    """Only the one attribute the challenge path reads off the pipeline."""

    def __init__(self, upstream: UpstreamSession) -> None:
        self.session = upstream


@pytest.fixture
def wired(monkeypatch):
    """One rule, one bound channel, one channel bound to nothing."""
    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)

    with Session(engine) as s:
        monitor = Monitor(name="rule", keyword="iPhone 15 128G", baseline_done=True)
        bound = NotifyChannel(kind="stub", label="bound", config="{}")
        # The channel the M1-era code would have missed: enabled, but no rule
        # points at it. Collection stopping is not one rule's problem.
        loose = NotifyChannel(kind="loose", label="not bound to anything", config="{}")
        s.add_all([monitor, bound, loose])
        s.commit()
        s.add(MonitorChannel(monitor_id=monitor.id, channel_id=bound.id))
        s.commit()
        return engine, monitor.id


def logs(engine) -> list[NotifyLog]:
    with Session(engine) as s:
        return list(s.exec(select(NotifyLog).order_by(NotifyLog.id)).all())


async def challenged_cycle(monkeypatch, upstream, registry, monitor_id, exc=None):
    async def cycle(pipeline, monitor):
        raise exc or ChallengeError("RGV587_ERROR::SM::哎哟喂,被挤爆啦")

    monkeypatch.setattr(scheduler, "run_monitor_cycle", cycle)
    return await scheduler._run_and_record(FakePipeline(upstream), monitor_id, registry)


# --------------------------------------------------------------------------- #
# One episode, one message
# --------------------------------------------------------------------------- #


async def test_a_challenge_notifies_once_and_the_next_cycle_stays_quiet(wired, monkeypatch):
    """The whole point of the edge detector.

    Two loops times N rules all hit the same challenge within seconds. Without
    the notice being claimed once per episode this is a burst that gets the
    channel muted, which is the same silence by a different route.
    """
    engine, monitor_id = wired
    upstream = UpstreamSession()
    stub = StubNotifier("stub")
    registry = {"stub": stub, "loose": StubNotifier("loose")}

    await challenged_cycle(monkeypatch, upstream, registry, monitor_id)
    assert len(stub.sent) == 1

    # The rule is disabled now, so re-enable it to prove the SECOND challenged
    # cycle is silenced by the session flag rather than by the rule being off.
    with Session(engine) as s:
        s.get(Monitor, monitor_id).enabled = True
        s.commit()
    await challenged_cycle(monkeypatch, upstream, registry, monitor_id)
    assert len(stub.sent) == 1, "a second cycle in the same episode must not re-announce"
    assert [row.kind for row in logs(engine)] == ["challenge", "challenge"]


async def test_a_successful_collection_re_arms_the_alert(wired, monkeypatch):
    """An episode ends at a transition OUT of the challenged state. If the flag
    never re-armed, the SECOND outage of the process's life would be silent."""
    engine, monitor_id = wired
    upstream = UpstreamSession()
    stub = StubNotifier("stub")
    registry = {"stub": stub, "loose": StubNotifier("loose")}

    await challenged_cycle(monkeypatch, upstream, registry, monitor_id)
    upstream.mark_success()  # what mtop calls after one real response
    with Session(engine) as s:
        s.get(Monitor, monitor_id).enabled = True
        s.commit()
    await challenged_cycle(monkeypatch, upstream, registry, monitor_id)
    assert len(stub.sent) == 2


async def test_the_alert_reaches_every_enabled_channel_not_just_bound_ones(wired, monkeypatch):
    _, monitor_id = wired
    upstream = UpstreamSession()
    bound, loose = StubNotifier("stub"), StubNotifier("loose")

    await challenged_cycle(monkeypatch, upstream, {"stub": bound, "loose": loose}, monitor_id)
    assert len(bound.sent) == 1
    assert len(loose.sent) == 1, "a rule with no channel bound would otherwise be silent"


async def test_a_disabled_channel_is_skipped(wired, monkeypatch):
    engine, monitor_id = wired
    with Session(engine) as s:
        s.exec(select(NotifyChannel).where(NotifyChannel.kind == "loose")).one().enabled = False
        s.commit()
    upstream = UpstreamSession()
    loose = StubNotifier("loose")
    await challenged_cycle(
        monkeypatch, upstream, {"stub": StubNotifier("stub"), "loose": loose}, monitor_id
    )
    assert loose.sent == []


async def test_a_channel_that_throws_does_not_break_the_cycle(wired, monkeypatch):
    """A dead SMTP server must not become a collection failure — the failure
    goes to NotifyLog and the other channels still get the message."""
    engine, monitor_id = wired
    upstream = UpstreamSession()
    alive = StubNotifier("loose")

    outcome = await challenged_cycle(
        monkeypatch, upstream, {"stub": StubNotifier("stub", boom=True), "loose": alive}, monitor_id
    )
    assert isinstance(outcome, CycleOutcome)
    assert len(alive.sent) == 1

    rows = {row.channel_id: row for row in logs(engine)}
    assert len(rows) == 2
    failed = [r for r in rows.values() if not r.ok]
    assert len(failed) == 1
    assert "SMTP is on fire" in failed[0].error


async def test_the_watch_loop_shares_the_same_episode(wired, monkeypatch):
    """Both loops hit the same upstream. Two independent edge detectors would
    mean two messages for one outage."""
    _, monitor_id = wired
    upstream = UpstreamSession()
    stub = StubNotifier("stub")
    registry = {"stub": stub, "loose": StubNotifier("loose")}

    await challenged_cycle(monkeypatch, upstream, registry, monitor_id)

    class GonePipeline(FakePipeline):
        async def collect_item(self, item_id):
            raise ChallengeError("FAIL_SYS_USER_VALIDATE")

    await scheduler.run_watch_cycle(GonePipeline(upstream), "i1", registry)
    assert len(stub.sent) == 1


async def test_no_channel_enabled_is_not_a_crash(wired, monkeypatch):
    engine, monitor_id = wired
    with Session(engine) as s:
        for channel in s.exec(select(NotifyChannel)).all():
            channel.enabled = False
        s.commit()
    await challenged_cycle(monkeypatch, UpstreamSession(), {}, monitor_id)
    assert logs(engine) == []


# --------------------------------------------------------------------------- #
# The message itself
# --------------------------------------------------------------------------- #


async def test_the_body_says_what_to_do_not_just_what_broke():
    body = challenge_body(SEARCH_API)
    assert SEARCH_API in body
    # A human has to pass the slider in their own browser, and the concrete
    # steps have to travel with the alert — an alert that only says "challenged"
    # sends the user hunting for the runbook.
    assert "滑块" in body
    assert "「设置」" in body and "导入" in body
    # docs/operations.md:「判定成功看行为，不看 cookie 名单」 — the import is not
    # the proof, the next successful collection is.
    assert "立即运行" in body


async def test_every_channel_actually_renders_the_body():
    """A challenge has no hits, so a renderer that only walks `hits` produces a
    subject line and an empty message."""
    n = Notification(kind="challenge", hits=[], body=challenge_body(SEARCH_API))
    assert "验证" in n.title
    for rendered in (_plain_body(n), _html_body(n), render(n)):
        assert "滑块" in rendered
        assert SEARCH_API in rendered


async def test_upstream_text_cannot_run_away_with_the_message():
    """`detail` can be the upstream's own words. A risk-control interstitial is
    a whole HTML page, and forwarding it verbatim into an email is not an
    alert."""
    assert len(challenge_body("x" * 10_000)) < 2_000


# --------------------------------------------------------------------------- #
# Nothing here may carry a credential
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch):
    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)
    app.state.session = UpstreamSession()
    app.state.browser = StubBrowser()
    return TestClient(app), app.state.session, engine


# Anything shaped like a credential handed back to us, matched by SHAPE rather
# than by one known value: the M1 report's lesson is that a redaction test
# pinned to a single literal passes happily while a different field leaks.
CREDENTIAL_SHAPE = re.compile(
    r"(_m_h5_tk|_m_h5_tk_enc|cookie2|unb|sgcookie|_tb_token_|Bearer|Authorization)", re.I
)


async def test_nothing_the_challenge_path_writes_contains_a_credential(client, wired, monkeypatch):
    c, upstream, _ = client
    engine, monitor_id = wired  # `wired` repoints scheduler.engine after `client`

    c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)
    assert upstream.cookies, "the paste must actually have been adopted"

    stub, loose = StubNotifier("stub"), StubNotifier("loose")
    upstream.mark_challenged(SEARCH_API, "RGV587_ERROR")
    await challenged_cycle(monkeypatch, upstream, {"stub": stub, "loose": loose}, monitor_id)

    blob = "\n".join(
        [json.dumps([r.model_dump() for r in logs(engine)], default=str)]
        + [_plain_body(n) + _html_body(n) + render(n) for n in stub.sent]
    )
    assert blob

    for name, value in upstream.cookies.items():
        assert value not in blob, f"cookie {name} leaked its VALUE"
    assert "testtoken123" not in blob, "the API token leaked"
    assert not CREDENTIAL_SHAPE.search(blob), CREDENTIAL_SHAPE.search(blob)


async def test_the_notify_log_row_carries_no_secret_even_when_delivery_fails(
    client, wired, monkeypatch
):
    """The error string is the easy place to leak one: it comes from an
    exception nobody wrote with redaction in mind."""
    c, upstream, _ = client
    engine, monitor_id = wired
    c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)

    await challenged_cycle(
        monkeypatch,
        upstream,
        {"stub": StubNotifier("stub", boom=True), "loose": StubNotifier("loose", boom=True)},
        monitor_id,
    )
    blob = json.dumps([r.model_dump() for r in logs(engine)], default=str)
    for value in upstream.cookies.values():
        assert value not in blob


# --------------------------------------------------------------------------- #
# Recovery on import
# --------------------------------------------------------------------------- #


async def test_import_clears_the_challenge_flags(client):
    c, upstream, _ = client
    upstream.mark_challenged(SEARCH_API, "RGV587_ERROR")
    assert upstream.needs_verification is True

    body = c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH).json()
    assert body["needs_verification"] is False
    assert body["challenged_apis"] == []
    # And an import is NOT proof: only a real collection sets `proven`.
    assert body["proven"] is False


async def test_import_resumes_only_the_rules_the_challenge_disabled(client):
    """The three ways a rule ends up off, and only one of them is a state that
    importing cookies actually fixes."""
    c, _, engine = client
    with Session(engine) as s:
        s.add_all(
            [
                Monitor(
                    name="challenged",
                    keyword="a",
                    enabled=False,
                    consecutive_failures=1,
                    last_error="auto-disabled after 1 failures: needs verification: RGV587_ERROR",
                ),
                Monitor(
                    name="five failures",
                    keyword="b",
                    enabled=False,
                    consecutive_failures=5,
                    last_error="auto-disabled after 5 failures: upstream returned nothing",
                ),
                # Switched off by hand: update_monitor writes no last_error.
                Monitor(name="by hand", keyword="c", enabled=False),
                Monitor(name="healthy", keyword="d", enabled=True),
            ]
        )
        s.commit()

    c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH)

    with Session(engine) as s:
        state = {
            m.name: (m.enabled, m.last_error, m.consecutive_failures)
            for m in s.exec(select(Monitor)).all()
        }
    assert state["challenged"] == (True, None, 0), "the failure state must be cleared too"
    assert state["five failures"][0] is False
    assert state["by hand"][0] is False
    assert state["healthy"][0] is True


async def test_import_does_not_kick_off_a_collection_sweep(client, monkeypatch):
    """Every resumed rule firing at once IS the request burst that gets a fresh
    session re-flagged. The normal sweep picks them up one at a time."""
    c, _, engine = client
    with Session(engine) as s:
        s.add(
            Monitor(
                name="challenged",
                keyword="a",
                enabled=False,
                last_error="auto-disabled after 1 failures: needs verification: RGV587_ERROR",
            )
        )
        s.commit()

    async def explode(*a, **kw):
        raise AssertionError("import must not reach the upstream")

    monkeypatch.setattr(scheduler, "run_monitor_cycle", explode)
    monkeypatch.setattr(scheduler, "_run_and_record", explode)
    assert (
        c.post("/api/session/cookies", json={"cookie_header": REAL_PASTE}, headers=AUTH).status_code
        == 200
    )


async def test_a_plain_collector_error_is_not_treated_as_a_challenge(wired, monkeypatch):
    """The resume keys on a marker the challenge branch writes. A rule dying of
    ordinary failures must not inherit it, and must not notify."""
    engine, monitor_id = wired
    stub = StubNotifier("stub")
    await challenged_cycle(
        monkeypatch,
        UpstreamSession(),
        {"stub": stub, "loose": StubNotifier("loose")},
        monitor_id,
        exc=CollectorError("upstream said no"),
    )
    assert stub.sent == []
    with Session(engine) as s:
        monitor = s.get(Monitor, monitor_id)
        assert scheduler.NEEDS_VERIFICATION not in (monitor.last_error or "")
    assert scheduler.resume_challenge_disabled() == []
