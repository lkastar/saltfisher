"""Channel CRUD, secret handling, and the test-send endpoint."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_session
from app.main import app
from app.models import NotifyChannel, NotifyLog
from app.notify import build_registry

AUTH = {"Authorization": "Bearer testtoken123"}

TELEGRAM = {
    "kind": "telegram",
    "label": "my bot",
    "config": {"bot_token": "123456:SUPERSECRET", "chat_id": "42"},
}
EMAIL = {
    "kind": "email",
    "label": "inbox",
    "config": {
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "username": "u@example.com",
        "password": "hunter2",
        "from_addr": "u@example.com",
        "to_addrs": ["target@example.com"],
    },
}


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.state.notify_registry = build_registry(httpx.AsyncClient())
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def test_secrets_never_leave_the_app(client):
    """The single most consequential test in this file: a token echoed back
    lands in the browser, in any log that captures responses, and in a bug
    report screenshot.
    """
    c, _ = client
    created = c.post("/api/channels", json=TELEGRAM, headers=AUTH)
    assert created.status_code == 201
    assert created.json()["config"]["bot_token"] == "***"
    assert "SUPERSECRET" not in created.text

    listed = c.get("/api/channels", headers=AUTH)
    assert "SUPERSECRET" not in listed.text
    assert listed.json()[0]["config"]["chat_id"] == "42"


def test_email_password_is_redacted_too(client):
    c, _ = client
    body = c.post("/api/channels", json=EMAIL, headers=AUTH).json()
    assert body["config"]["password"] == "***"
    assert body["config"]["username"] == "u@example.com"


def test_the_real_secret_is_stored(client):
    c, engine = client
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    with Session(engine) as s:
        stored = json.loads(s.get(NotifyChannel, channel_id).config)
    assert stored["bot_token"] == "123456:SUPERSECRET"


def test_a_redacted_value_sent_back_does_not_overwrite_the_secret(client):
    """The page renders "***" and PATCHes the whole config back. Taking that
    literally would replace the token with three asterisks.
    """
    c, engine = client
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    c.patch(
        f"/api/channels/{channel_id}",
        json={"config": {"bot_token": "***", "chat_id": "99"}},
        headers=AUTH,
    )
    with Session(engine) as s:
        stored = json.loads(s.get(NotifyChannel, channel_id).config)
    assert stored["bot_token"] == "123456:SUPERSECRET"
    assert stored["chat_id"] == "99"


def test_a_new_secret_does_replace_the_old_one(client):
    c, engine = client
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    c.patch(
        f"/api/channels/{channel_id}",
        json={"config": {"bot_token": "999:ROTATED"}},
        headers=AUTH,
    )
    with Session(engine) as s:
        stored = json.loads(s.get(NotifyChannel, channel_id).config)
    assert stored["bot_token"] == "999:ROTATED"
    assert stored["chat_id"] == "42", "unrelated keys must survive a partial config patch"


def test_unknown_kind_is_rejected(client):
    c, _ = client
    r = c.post("/api/channels", json={**TELEGRAM, "kind": "carrier-pigeon"}, headers=AUTH)
    assert r.status_code == 422


def test_toggle_and_delete(client):
    c, engine = client
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    off = c.patch(f"/api/channels/{channel_id}", json={"enabled": False}, headers=AUTH)
    assert off.json()["enabled"] is False
    assert c.delete(f"/api/channels/{channel_id}", headers=AUTH).status_code == 204
    assert c.get("/api/channels", headers=AUTH).json() == []


def test_missing_channel_is_404(client):
    c, _ = client
    assert c.patch("/api/channels/999", json={"enabled": True}, headers=AUTH).status_code == 404
    assert c.delete("/api/channels/999", headers=AUTH).status_code == 404
    assert c.post("/api/channels/999/test", headers=AUTH).status_code == 404


def test_test_send_reports_failure_without_raising(client):
    """A misconfigured channel must produce a readable result, not a 500 —
    the whole point of the button is diagnosing exactly that.
    """
    c, engine = client
    channel_id = c.post(
        "/api/channels",
        json={"kind": "telegram", "label": "broken", "config": {"chat_id": "42"}},
        headers=AUTH,
    ).json()["id"]

    r = c.post(f"/api/channels/{channel_id}/test", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "invalid" in r.json()["error"]

    with Session(engine) as s:
        entry = s.exec(select(NotifyLog)).one()
        assert entry.kind == "test" and entry.ok is False


def test_test_send_succeeds_through_the_real_delivery_path(client):
    c, engine = client
    sent = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    app.state.notify_registry = build_registry(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    r = c.post(f"/api/channels/{channel_id}/test", headers=AUTH)
    assert r.json() == {"ok": True, "error": None}
    # the sample carries a waived-filter label so the user sees what one looks
    # like before it matters
    assert "卖家信用未知" in sent["body"]["text"]
    # and the sample's angle brackets are escaped, proving the same renderer ran
    assert "&lt;测试&gt;" in sent["body"]["text"]


def test_notify_logs_are_listed_newest_first(client):
    c, engine = client
    channel_id = c.post("/api/channels", json=TELEGRAM, headers=AUTH).json()["id"]
    with Session(engine) as s:
        for i in range(3):
            s.add(NotifyLog(channel_id=channel_id, kind="new_in_range", ok=True, item_count=i))
        s.commit()
    logs = c.get("/api/notify-logs", headers=AUTH).json()
    assert len(logs) >= 3
    assert logs == sorted(logs, key=lambda entry: entry["sent_at"], reverse=True)


def test_auth_required(client):
    c, _ = client
    assert c.get("/api/channels").status_code == 401
    assert c.post("/api/channels/1/test").status_code == 401
