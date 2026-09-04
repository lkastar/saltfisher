"""Monitor CRUD and the validation that the storage layer cannot express.

SQLModel skips pydantic validation on table models, so the 60-second interval
floor produces a CHECK-constraint 500 rather than a readable 422 unless the
request models enforce it. These tests pin the 422s.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_session
from app.main import app
from app.models import Item, Monitor, MonitorHit, Seller

AUTH = {"Authorization": "Bearer testtoken123"}

VALID = {
    "name": "iPhone 15 捡漏",
    "keyword": "iPhone 15",
    "exclude_words": "壳 膜 报废",
    "price_min_cents": 200000,
    "price_max_cents": 350000,
    "interval_seconds": 300,
    "exclude_shop": True,
}


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    # TestClient without a context manager does not run the lifespan, so no
    # browser is launched here.
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def test_auth_is_required(client):
    c, _ = client
    assert c.get("/api/monitors").status_code == 401


def test_create_and_read(client):
    c, _ = client
    created = c.post("/api/monitors", json=VALID, headers=AUTH)
    assert created.status_code == 201
    body = created.json()
    assert body["keyword"] == "iPhone 15"
    assert body["enabled"] is True
    # a fresh rule has not established its baseline yet, so its first cycle
    # must stay silent
    assert body["baseline_done"] is False
    assert body["hit_count"] == 0

    listed = c.get("/api/monitors", headers=AUTH).json()
    assert [m["id"] for m in listed] == [body["id"]]


@pytest.mark.parametrize("interval", [0, 1, 30, 59, -10])
def test_interval_below_the_floor_is_rejected_with_422(client, interval):
    """The floor is an anti-ban rule. A 500 from a CHECK constraint would be
    both unreadable and a sign the API let a bad value through.
    """
    c, _ = client
    r = c.post("/api/monitors", json={**VALID, "interval_seconds": interval}, headers=AUTH)
    assert r.status_code == 422


def test_interval_at_the_floor_is_accepted(client):
    c, _ = client
    r = c.post("/api/monitors", json={**VALID, "interval_seconds": 60}, headers=AUTH)
    assert r.status_code == 201


def test_inverted_price_range_is_rejected(client):
    c, _ = client
    r = c.post(
        "/api/monitors",
        json={**VALID, "price_min_cents": 400000, "price_max_cents": 100000},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_patch_cannot_sneak_an_inverted_range_past_validation(client):
    """Each half is valid alone; only the combination with the stored value is
    wrong, so the check has to happen against the merged state.
    """
    c, _ = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    r = c.patch(f"/api/monitors/{monitor_id}", json={"price_min_cents": 900000}, headers=AUTH)
    assert r.status_code == 422


def test_patch_below_the_interval_floor_is_rejected(client):
    c, _ = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    r = c.patch(f"/api/monitors/{monitor_id}", json={"interval_seconds": 5}, headers=AUTH)
    assert r.status_code == 422


def test_empty_keyword_is_rejected(client):
    c, _ = client
    assert c.post("/api/monitors", json={**VALID, "keyword": ""}, headers=AUTH).status_code == 422


def test_toggle_enabled(client):
    c, _ = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    off = c.patch(f"/api/monitors/{monitor_id}", json={"enabled": False}, headers=AUTH)
    assert off.status_code == 200 and off.json()["enabled"] is False


def test_re_enabling_clears_the_failure_state(client):
    """Re-enabling by hand means "try again". Leaving the failure counter alone
    would re-trip the auto-disable on the very next error.
    """
    c, engine = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    with Session(engine) as s:
        m = s.get(Monitor, monitor_id)
        m.enabled = False
        m.consecutive_failures = 5
        m.last_error = "auto-disabled after 5 failures"
        s.commit()

    body = c.patch(f"/api/monitors/{monitor_id}", json={"enabled": True}, headers=AUTH).json()
    assert body["enabled"] is True
    assert body["consecutive_failures"] == 0
    assert body["last_error"] is None


def test_missing_monitor_is_404_not_500(client):
    c, _ = client
    assert c.get("/api/monitors/999", headers=AUTH).status_code == 404
    assert c.patch("/api/monitors/999", json={"enabled": True}, headers=AUTH).status_code == 404
    assert c.delete("/api/monitors/999", headers=AUTH).status_code == 404
    assert c.post("/api/monitors/999/run", headers=AUTH).status_code == 404


def test_delete_takes_its_hits_with_it(client):
    """SQLite does not cascade by default; an orphan hit row would break the
    hit list query with a dangling monitor_id.
    """
    c, engine = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    with Session(engine) as s:
        s.add(Seller(id="s1", nick="老王"))
        s.add(Item(id="i1", title="t", seller_id="s1", seller_nick="老王"))
        s.add(MonitorHit(monitor_id=monitor_id, item_id="i1"))
        s.commit()

    assert c.delete(f"/api/monitors/{monitor_id}", headers=AUTH).status_code == 204
    with Session(engine) as s:
        assert s.exec(select(MonitorHit)).all() == []
        # the item itself is shared data and must survive
        assert s.get(Item, "i1") is not None


def test_health_fields_are_exposed_for_the_management_page(client):
    """A monitoring tool whose failures are invisible is worse than one that
    stopped. See design.md section 12.
    """
    c, _ = client
    body = c.post("/api/monitors", json=VALID, headers=AUTH).json()
    for field in (
        "last_run_at",
        "last_error",
        "last_collector",
        "consecutive_failures",
        "hit_count",
        "baseline_done",
    ):
        assert field in body


def test_a_manual_run_records_which_collector_served_it(client, monkeypatch):
    """Found in T8: the manual path persisted items and hits but never wrote
    last_run_at or last_collector, so the management page showed "路径 —" right
    after a successful mtop cycle. For a DISABLED rule -- which the scheduler
    never touches -- that column stayed empty forever.
    """
    from app import scheduler
    from app.scheduler import CycleOutcome

    c, engine = client
    monkeypatch.setattr(scheduler, "engine", engine)
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]

    async def fake_cycle(pipeline, monitor):
        return CycleOutcome(hits=[], collector="mtop", collected=30, passed=25)

    monkeypatch.setattr("app.api.monitors.run_monitor_cycle", fake_cycle)
    app.state.pipeline = object()

    result = c.post(f"/api/monitors/{monitor_id}/run", headers=AUTH)
    assert result.status_code == 200
    assert result.json()["collector"] == "mtop"

    row = c.get(f"/api/monitors/{monitor_id}", headers=AUTH).json()
    assert row["last_collector"] == "mtop"
    assert row["last_run_at"] is not None
    assert row["last_error"] is None


def test_a_failed_manual_run_does_not_count_toward_auto_disable(client, monkeypatch):
    """Deliberate asymmetry: the failure already reaches the user as a 502, and
    counting a manual probe toward the streak would let someone switch off
    their own rule by testing it.
    """
    from app import scheduler
    from app.collector.base import CollectorError

    c, engine = client
    monkeypatch.setattr(scheduler, "engine", engine)
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]

    async def failing_cycle(pipeline, monitor):
        raise CollectorError("upstream said no")

    monkeypatch.setattr("app.api.monitors.run_monitor_cycle", failing_cycle)
    app.state.pipeline = object()

    assert c.post(f"/api/monitors/{monitor_id}/run", headers=AUTH).status_code == 502

    row = c.get(f"/api/monitors/{monitor_id}", headers=AUTH).json()
    assert row["consecutive_failures"] == 0
    assert row["enabled"] is True


def channel(engine, label: str = "邮件") -> int:
    """A channel row, inserted directly.

    Going through /api/channels would need app.state.notify_registry, which
    belongs to the channel tests. These tests are about the monitor-to-channel
    link, so the row is all that is needed.
    """
    from app.models import NotifyChannel

    with Session(engine) as s:
        row = NotifyChannel(kind="email", label=label, config="{}", enabled=True)
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.id is not None
        return row.id


def make_monitor(c, channel_ids: list[int]) -> dict:
    return c.post("/api/monitors", json={**VALID, "channel_ids": channel_ids}, headers=AUTH).json()


def test_a_rule_reports_which_channels_it_notifies(client):
    """Found in T8: channel_ids was write-once at creation and absent from
    every response, and the panel created every rule with none -- so a monitor
    rule could never notify anyone, which is the entire product.
    """
    c, engine = client
    first, second = channel(engine, "邮件"), channel(engine, "备用")

    created = make_monitor(c, [first, second])
    assert created["channel_ids"] == sorted([first, second])

    read = c.get(f"/api/monitors/{created['id']}", headers=AUTH).json()
    assert read["channel_ids"] == sorted([first, second])
    listed = c.get("/api/monitors", headers=AUTH).json()
    assert listed[0]["channel_ids"] == sorted([first, second])


def test_channels_can_be_changed_after_creation(client):
    c, engine = client
    first, second = channel(engine, "邮件"), channel(engine, "备用")
    monitor_id = make_monitor(c, [first])["id"]

    patched = c.patch(
        f"/api/monitors/{monitor_id}", json={"channel_ids": [second]}, headers=AUTH
    ).json()
    assert patched["channel_ids"] == [second]


def test_omitting_channel_ids_leaves_them_alone(client):
    """PATCH semantics: absent means "do not touch". Otherwise every unrelated
    edit -- renaming a rule, changing its interval -- would silently detach the
    channels and stop the notifications.
    """
    c, engine = client
    first = channel(engine)
    monitor_id = make_monitor(c, [first])["id"]

    patched = c.patch(f"/api/monitors/{monitor_id}", json={"name": "改个名"}, headers=AUTH).json()
    assert patched["channel_ids"] == [first]


def test_an_empty_list_detaches_every_channel(client):
    """Distinct from omission: [] is a real instruction to notify nobody."""
    c, engine = client
    first = channel(engine)
    monitor_id = make_monitor(c, [first])["id"]

    patched = c.patch(f"/api/monitors/{monitor_id}", json={"channel_ids": []}, headers=AUTH).json()
    assert patched["channel_ids"] == []


def test_attaching_a_channel_that_does_not_exist_is_422(client):
    c, _ = client
    monitor_id = c.post("/api/monitors", json=VALID, headers=AUTH).json()["id"]
    r = c.patch(f"/api/monitors/{monitor_id}", json={"channel_ids": [999]}, headers=AUTH)
    assert r.status_code == 422
    assert "999" in r.json()["detail"]


def test_the_scheduler_sees_the_channels_the_api_wrote(client):
    """The two sides must agree: the API writes MonitorChannel rows and
    channels_for_monitor reads them. A mismatch is invisible until a hit is
    silently not delivered.
    """
    from app.notify import channels_for_monitor

    c, engine = client
    first = channel(engine)
    monitor_id = make_monitor(c, [first])["id"]

    with Session(engine) as s:
        assert [ch.id for ch in channels_for_monitor(s, monitor_id)] == [first]
