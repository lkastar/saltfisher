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
