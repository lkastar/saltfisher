"""Serving the built frontend from the API process."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app, mount_frontend


def build(tmp_path, *, with_assets: bool = True):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>咸鱼监控</title>")
    if with_assets:
        (dist / "assets").mkdir()
        (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    return dist


def app_with(dist) -> TestClient:
    fresh = FastAPI()

    @fresh.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    mount_frontend(fresh, dist)
    return TestClient(fresh)


def test_missing_build_does_not_stop_the_backend(tmp_path):
    """Backend-only development and CI have no dist/. Refusing to start would
    turn "frontend not built" into "app is broken" -- and take 300 backend
    tests down with it.
    """
    fresh = FastAPI()
    assert mount_frontend(fresh, tmp_path / "nothing-here") is False
    assert TestClient(fresh).get("/anything").status_code == 404


def test_the_real_app_keeps_its_api_after_mounting():
    """Guards the real `app` object, whether or not web/dist happens to exist
    on this machine: the module-level mount_frontend(app) must neither raise
    nor shadow a route that was registered before it.
    """
    assert TestClient(app).get("/api/health").json() == {"status": "ok"}


def test_serves_the_app_shell_at_the_root(tmp_path):
    c = app_with(build(tmp_path))
    r = c.get("/")
    assert r.status_code == 200
    assert "咸鱼监控" in r.text


def test_deep_links_reload_into_the_app(tmp_path):
    """Reloading a client-side route must return the shell, not a 404."""
    c = app_with(build(tmp_path))
    for path in ("/items/1081966784098", "/monitors", "/watchlist?sort=-price"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "咸鱼监控" in r.text


def test_assets_are_served_as_files(tmp_path):
    c = app_with(build(tmp_path))
    r = c.get("/assets/index-abc.js")
    assert r.status_code == 200
    assert "console.log(1)" in r.text


def test_an_unmatched_api_path_stays_json(tmp_path):
    """HTML here would make the frontend's res.json() throw and disguise a
    wrong URL as a broken backend.
    """
    c = app_with(build(tmp_path))
    r = c.get("/api/nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}
    assert "html" not in r.headers.get("content-type", "")


def test_api_routes_still_win_over_the_catch_all(tmp_path):
    c = app_with(build(tmp_path))
    assert c.get("/api/health").json() == {"status": "ok"}


def test_traversal_cannot_reach_outside_the_build(tmp_path):
    """The catch-all never builds a path from the request, so this is not a
    sanitising question -- there is no path to sanitise.
    """
    (tmp_path / "secret.txt").write_text("do not serve me")
    c = app_with(build(tmp_path))
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        r = c.get(path)
        assert "do not serve me" not in r.text, path


def test_a_build_without_assets_still_serves_the_shell(tmp_path):
    c = app_with(build(tmp_path, with_assets=False))
    assert c.get("/").status_code == 200
    assert c.get("/assets/whatever.js").status_code == 200  # falls to the shell
