from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_needs_no_token():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_protected_route_rejects_missing_and_wrong_tokens():
    assert client.get("/api/whoami").status_code == 401
    assert client.get("/api/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
    # a bare token without the Bearer scheme must also fail
    assert client.get("/api/whoami", headers={"Authorization": "testtoken123"}).status_code == 401


def test_protected_route_accepts_correct_token():
    r = client.get("/api/whoami", headers={"Authorization": "Bearer testtoken123"})
    assert r.status_code == 200 and r.json() == {"authenticated": True}
