"""The analytics endpoints: their contract, not their arithmetic.

The maths is covered in `test_analytics.py`. What matters here is the shape of
the contract the frontend is generated from — which parameters are rejected,
what an unknown keyword returns, and that the query count does not follow the
row count.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from app.db import get_session
from app.main import app
from app.models import CollectRun, Item, Monitor, MonitorHit, PriceSnapshot, Seller

AUTH = {"Authorization": "Bearer testtoken123"}
NOW = datetime.now(UTC)
KW = "iPhone 15 128G"

ENDPOINTS = ("price-distribution", "price-drops", "supply-trend")

# Twenty days: inside the default 30-day supply window -- which starts at
# now-29d, so a first sighting exactly 30 days old falls just outside it --
# while still leaving the 30-day-old snapshot below visible to the 7-day drop
# window. The three endpoints read `days` differently on purpose (window of
# observation, price-comparison point, chart width) and this one seed has to
# satisfy all three at once.
FIRST_HIT_DAYS_AGO = 20


def seed(engine, *, items: int = 3) -> None:
    """One rule on KW, `items` listings, each with a two-point price history
    old enough that the default seven-day drop window can see it."""
    with Session(engine) as s:
        s.add(Monitor(name="rule", keyword=KW, interval_seconds=300))
        s.add(Seller(id="s1", nick="老王"))
        s.commit()
        for i in range(items):
            s.add(
                Item(
                    id=f"i{i}",
                    title=f"{KW} 第{i}台",
                    seller_id="s1",
                    seller_nick="老王",
                    first_seen_at=NOW - timedelta(days=30),
                    last_seen_at=NOW,
                )
            )
        s.commit()
        for i in range(items):
            s.add(
                PriceSnapshot(
                    item_id=f"i{i}",
                    price_cents=400000 + i * 1000,
                    status="on_sale",
                    source="mtop",
                    captured_at=NOW - timedelta(days=30),
                )
            )
            s.add(
                PriceSnapshot(
                    item_id=f"i{i}",
                    price_cents=300000 + i * 1000,
                    status="on_sale",
                    source="mtop",
                    captured_at=NOW - timedelta(hours=1),
                )
            )
            s.add(
                MonitorHit(
                    monitor_id=1,
                    item_id=f"i{i}",
                    first_hit_at=NOW - timedelta(days=FIRST_HIT_DAYS_AGO),
                )
            )
        s.add(CollectRun(monitor_id=1, started_at=NOW, ok=True, item_count=items))
        s.commit()


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app), engine
    app.dependency_overrides.clear()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_auth_required(client, endpoint):
    c, _ = client
    assert c.get(f"/api/analytics/{endpoint}?keyword={KW}").status_code == 401


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_keyword_is_required(client, endpoint):
    c, _ = client
    assert c.get(f"/api/analytics/{endpoint}", headers=AUTH).status_code == 422
    assert c.get(f"/api/analytics/{endpoint}?keyword=", headers=AUTH).status_code == 422


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("days", [0, -1, 400])
def test_the_window_bounds_are_rejected_as_422_not_500(client, endpoint, days):
    """Expressed as Query(ge=1, le=365) rather than an `if` in the body, so
    FastAPI rejects it before the handler runs and the bound also reaches
    OpenAPI for the generated frontend types.
    """
    c, _ = client
    # No seeding: FastAPI rejects the value before the handler runs, which is
    # the whole point of expressing the bound as Query(ge=1, le=365).
    result = c.get(f"/api/analytics/{endpoint}?keyword={KW}&days={days}", headers=AUTH)
    assert result.status_code == 422


def test_the_drop_limit_is_bounded(client):
    c, engine = client
    seed(engine)
    assert (
        c.get(f"/api/analytics/price-drops?keyword={KW}&limit=0", headers=AUTH).status_code == 422
    )
    assert (
        c.get(f"/api/analytics/price-drops?keyword={KW}&limit=101", headers=AUTH).status_code == 422
    )
    assert (
        c.get(f"/api/analytics/price-drops?keyword={KW}&limit=100", headers=AUTH).status_code == 200
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_an_unknown_keyword_is_an_empty_result_not_a_404(client, endpoint):
    """A deleted rule or a stale shared link lands here. Returning 404 would
    make the frontend handle a special case for an ordinary state, and the
    honest answer is "nothing collected for that keyword".
    """
    c, engine = client
    seed(engine)
    result = c.get(f"/api/analytics/{endpoint}?keyword=nothing+here", headers=AUTH)
    assert result.status_code == 200
    body = result.json()
    assert body["sample_size"] == 0
    assert body["data_days"] == 0


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_every_response_reports_how_much_data_is_behind_it(client, endpoint):
    c, engine = client
    seed(engine)
    # 25 is the one window that satisfies all three endpoints at once, and
    # working that out is the second time `days` meaning three things has
    # tripped this file up: supply-trend needs >= 21 to reach a first sighting
    # 20 days back (its window starts at now - days + 1), while price-drops
    # needs <= 30 so the 30-day-old snapshot still counts as "before".
    body = c.get(f"/api/analytics/{endpoint}?keyword={KW}&days=25", headers=AUTH).json()

    assert body["sample_size"] > 0
    assert body["data_days"] == FIRST_HIT_DAYS_AGO + 1
    # `window_days` echoes the request, so asserting it is >= 1 only restates
    # Query(ge=1). Assert it round-trips the value actually sent instead.
    assert body["window_days"] == 25


def test_the_distribution_separates_window_size_from_live_size(client):
    c, engine = client
    seed(engine, items=3)
    body = c.get(f"/api/analytics/price-distribution?keyword={KW}", headers=AUTH).json()

    assert body["sample_size"] == 3
    assert body["fresh_size"] == 3
    assert body["quantiles"]["p50"] > 0
    assert sum(bucket["count"] for bucket in body["histogram"]) == 3


def test_the_distribution_omits_quantiles_below_two_samples(client):
    """The response still validates: the quantile fields are nullable, so a
    one-listing keyword renders as "not enough data" instead of erroring.
    """
    c, engine = client
    seed(engine, items=1)
    body = c.get(f"/api/analytics/price-distribution?keyword={KW}", headers=AUTH).json()

    assert body["sample_size"] == 1
    assert body["quantiles"]["p50"] is None


def test_the_drop_ranking_carries_what_a_row_needs_to_render(client):
    c, engine = client
    seed(engine, items=3)
    body = c.get(f"/api/analytics/price-drops?keyword={KW}&limit=2", headers=AUTH).json()

    assert body["sample_size"] == 3
    assert len(body["rows"]) == 2
    # Values, not presence. `response_model=PriceDrops` already guarantees
    # every field exists, so `assert field in row` would pass with all of them
    # wrong -- it tests Pydantic, not this endpoint.
    row = body["rows"][0]
    assert row["then_cents"] > row["now_cents"], "a drop must have fallen"
    assert row["drop_bps"] == (row["then_cents"] - row["now_cents"]) * 10000 // row["then_cents"]
    assert isinstance(row["drop_bps"], int)
    assert row["title"] and row["title"] != row["item_id"], "the row needs a real title"
    assert row["item_id"]
    # Deepest first, and the cap did not reorder them.
    assert body["rows"][0]["drop_bps"] >= body["rows"][1]["drop_bps"]


def test_the_supply_series_covers_every_day_in_the_window(client):
    c, engine = client
    seed(engine)
    body = c.get(f"/api/analytics/supply-trend?keyword={KW}&days=10", headers=AUTH).json()

    assert len(body["days"]) == 10
    assert body["days"][-1]["collected"] is True
    assert body["days"][0]["collected"] is False


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_query_count_does_not_grow_with_rows(client, endpoint):
    """Proved by EQUALITY, not by a bound.

    `assert count < 10` passes at 3 rows and regresses quietly at 30, which is
    exactly how a select-per-row creeps back into an explicit-query codebase.
    """
    c, engine = client
    seed(engine, items=3)
    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    url = f"/api/analytics/{endpoint}?keyword={KW}"
    event.listen(engine, "before_cursor_execute", record)
    try:
        c.get(url, headers=AUTH)
        few = len(statements)
        statements.clear()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    app.dependency_overrides.clear()
    big_engine = _reseed(30)
    event.listen(big_engine, "before_cursor_execute", record)
    try:
        c.get(url, headers=AUTH)
        many = len(statements)
    finally:
        event.remove(big_engine, "before_cursor_execute", record)

    assert few == many, f"{few} queries for 3 listings vs {many} for 30"


def _reseed(items: int):
    """A second database with more rows, wired into the same TestClient."""
    from tests.conftest import memory_engine

    engine = memory_engine()
    seed(engine, items=items)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    return engine


def test_an_unmatched_analytics_path_is_still_json_404(client):
    """The SPA catch-all must not swallow /api. Guarded globally in
    test_static.py; asserted here for this router's own prefix because a
    router registered after the mount would fail exactly this way.
    """
    c, _ = client
    result = c.get("/api/analytics/nope", headers=AUTH)
    assert result.status_code == 404
    assert result.headers["content-type"].startswith("application/json")
