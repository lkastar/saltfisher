"""Hit list, item detail, and price history."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from app.db import get_session
from app.main import app
from app.models import Item, MonitorHit, PriceSnapshot, Seller

AUTH = {"Authorization": "Bearer testtoken123"}
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def seed(engine, count: int = 3, *, with_seller: bool = True) -> None:
    """`count` items, each with one snapshot, prices ascending by index."""
    with Session(engine) as s:
        if with_seller:
            s.add(
                Seller(
                    id="s1",
                    nick="阿芳阿芳",
                    is_shop=False,
                    credit_level=4,
                    review_count=0,
                    positive_rate=None,
                )
            )
            s.commit()
        for i in range(count):
            s.add(
                Item(
                    id=f"item{i}",
                    title=f"iPhone 13 128G 第{i}台",
                    seller_id="s1",
                    seller_nick="阿芳阿芳",
                    first_seen_at=T0 + timedelta(minutes=i),
                    last_seen_at=T0 + timedelta(hours=i),
                    status="on_sale" if i % 2 == 0 else "sold",
                    image_urls='["https://cdn/a.jpg", "https://cdn/b.jpg"]',
                )
            )
        s.commit()
        for i in range(count):
            s.add(
                PriceSnapshot(
                    item_id=f"item{i}",
                    price_cents=100000 + i * 10000,
                    status="on_sale",
                    source="mtop",
                    captured_at=T0 + timedelta(minutes=i),
                )
            )
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


def test_auth_required(client):
    c, _ = client
    assert c.get("/api/items").status_code == 401


def test_lists_items_with_their_latest_price(client):
    c, engine = client
    seed(engine)
    with Session(engine) as s:
        # A second observation must win over the first.
        s.add(
            PriceSnapshot(
                item_id="item0",
                price_cents=88000,
                status="on_sale",
                source="mtop",
                captured_at=T0 + timedelta(hours=5),
            )
        )
        s.commit()

    body = c.get("/api/items", headers=AUTH).json()
    prices = {row["id"]: row["price_cents"] for row in body}
    assert prices == {"item0": 88000, "item1": 110000, "item2": 120000}


def test_one_row_per_item_even_when_several_rules_matched(client):
    """Expanding by hit would show the same phone once per rule."""
    c, engine = client
    seed(engine, count=1)
    with Session(engine) as s:
        for monitor_id in (1, 2, 3):
            s.add(MonitorHit(monitor_id=monitor_id, item_id="item0", first_hit_at=T0))
        s.commit()

    body = c.get("/api/items", headers=AUTH).json()
    assert [row["id"] for row in body] == ["item0"]


def test_hit_fields_are_null_without_a_monitor_id(client):
    """`unverified_filters` belongs to one rule's match, not to the item, so
    there is no honest item-level value to report.
    """
    c, engine = client
    seed(engine, count=1)
    with Session(engine) as s:
        s.add(
            MonitorHit(
                monitor_id=7,
                item_id="item0",
                first_hit_at=T0,
                unverified_filters='["region", "free_shipping"]',
            )
        )
        s.commit()

    row = c.get("/api/items", headers=AUTH).json()[0]
    assert row["unverified_filters"] is None
    assert row["first_hit_at"] is None

    row = c.get("/api/items?monitor_id=7", headers=AUTH).json()[0]
    assert row["unverified_filters"] == ["地区未知", "是否包邮未知"]
    assert row["first_hit_at"] is not None
    assert row["in_range"] is True


def test_monitor_id_narrows_to_that_rules_matches(client):
    c, engine = client
    seed(engine)
    with Session(engine) as s:
        s.add(MonitorHit(monitor_id=1, item_id="item1", first_hit_at=T0))
        s.commit()

    body = c.get("/api/items?monitor_id=1", headers=AUTH).json()
    assert [row["id"] for row in body] == ["item1"]
    assert c.get("/api/items?monitor_id=999", headers=AUTH).json() == []


def test_price_and_status_filters(client):
    c, engine = client
    seed(engine)

    ids = lambda q: [r["id"] for r in c.get(f"/api/items?{q}", headers=AUTH).json()]  # noqa: E731
    assert ids("min_price_cents=110000") == ["item2", "item1"]
    assert ids("max_price_cents=110000") == ["item1", "item0"]
    assert ids("min_price_cents=110000&max_price_cents=110000") == ["item1"]
    assert ids("status=sold") == ["item1"]
    assert ids("status=on_sale") == ["item2", "item0"]


def test_sorting_and_paging(client):
    c, engine = client
    seed(engine)

    ids = lambda q: [r["id"] for r in c.get(f"/api/items?{q}", headers=AUTH).json()]  # noqa: E731
    assert ids("sort=price") == ["item0", "item1", "item2"]
    assert ids("sort=-price") == ["item2", "item1", "item0"]
    assert ids("sort=first_seen") == ["item0", "item1", "item2"]
    assert ids("sort=-first_seen") == ["item2", "item1", "item0"]
    assert ids("sort=-last_seen") == ["item2", "item1", "item0"]
    assert ids("sort=price&limit=2") == ["item0", "item1"]
    assert ids("sort=price&limit=2&offset=2") == ["item2"]


def test_paging_is_stable_when_the_sort_key_ties(client):
    """Every item shares first_seen_at here. Without the id tiebreak the two
    pages can overlap, showing one item twice and hiding another.
    """
    c, engine = client
    with Session(engine) as s:
        s.add(Seller(id="s1", nick="老王"))
        s.commit()
        for i in range(6):
            s.add(
                Item(
                    id=f"tie{i}",
                    title="同一时刻入库",
                    seller_id="s1",
                    seller_nick="老王",
                    first_seen_at=T0,
                    last_seen_at=T0,
                )
            )
            s.add(
                PriceSnapshot(
                    item_id=f"tie{i}", price_cents=50000, status="on_sale", source="mtop"
                )
            )
        s.commit()

    ids = lambda q: [r["id"] for r in c.get(f"/api/items?{q}", headers=AUTH).json()]  # noqa: E731
    page1 = ids("sort=-first_seen&limit=3")
    page2 = ids("sort=-first_seen&limit=3&offset=3")
    assert len(set(page1) | set(page2)) == 6


def test_bad_sort_is_422_not_500(client):
    c, engine = client
    seed(engine, count=1)
    r = c.get("/api/items?sort=price;drop", headers=AUTH)
    assert r.status_code == 422
    assert c.get("/api/items?status=nonsense", headers=AUTH).status_code == 422


def test_query_count_does_not_grow_with_rows(client):
    """The real assertion is EQUALITY, not "under some constant": a per-row
    query would pass a loose bound at 3 rows and quietly regress at 30.
    """
    c, engine = client
    seed(engine, count=30)

    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        c.get("/api/items?limit=3", headers=AUTH)
        few = len(statements)
        statements.clear()
        c.get("/api/items?limit=30", headers=AUTH)
        many = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert few == many, f"{few} queries for 3 rows vs {many} for 30"


def test_query_count_stays_flat_with_hit_fields(client):
    c, engine = client
    seed(engine, count=30)
    with Session(engine) as s:
        for i in range(30):
            s.add(MonitorHit(monitor_id=1, item_id=f"item{i}", first_hit_at=T0))
        s.commit()

    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        c.get("/api/items?monitor_id=1&limit=3", headers=AUTH)
        few = len(statements)
        statements.clear()
        c.get("/api/items?monitor_id=1&limit=30", headers=AUTH)
        many = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert few == many, f"{few} queries for 3 rows vs {many} for 30"


def test_detail_carries_images_and_seller_profile(client):
    c, engine = client
    seed(engine, count=1)
    row = c.get("/api/items/item0", headers=AUTH).json()
    assert row["image_urls"] == ["https://cdn/a.jpg", "https://cdn/b.jpg"]
    assert row["seller_nick"] == "阿芳阿芳"
    assert row["seller_credit_level"] == 4
    assert row["seller_review_count"] == 0


def test_unknown_seller_fields_are_null_not_zero(client):
    """0 reviews is a warning sign; unknown is missing data. Collapsing them
    would let the panel report a verdict it never made.
    """
    c, engine = client
    seed(engine, count=1, with_seller=False)
    row = c.get("/api/items/item0", headers=AUTH).json()
    assert row["seller_credit_level"] is None
    assert row["seller_review_count"] is None
    assert row["seller_positive_rate"] is None
    assert row["seller_is_shop"] is None


def test_missing_item_is_404_with_a_readable_detail(client):
    c, _ = client
    r = c.get("/api/items/nope", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"] == "item not found"
    assert c.get("/api/items/nope/prices", headers=AUTH).status_code == 404


def test_malformed_image_column_costs_one_field_not_the_page(client):
    c, engine = client
    seed(engine, count=1)
    with Session(engine) as s:
        item = s.get(Item, "item0")
        item.image_urls = "{not json"
        s.add(item)
        s.commit()

    r = c.get("/api/items/item0", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["image_urls"] == []


def test_price_history_is_ascending_by_time(client):
    c, engine = client
    seed(engine, count=1)
    with Session(engine) as s:
        for offset, price in ((2, 95000), (1, 98000)):
            s.add(
                PriceSnapshot(
                    item_id="item0",
                    price_cents=price,
                    status="on_sale",
                    source="mtop",
                    captured_at=T0 + timedelta(hours=offset),
                )
            )
        s.commit()

    points = c.get("/api/items/item0/prices", headers=AUTH).json()
    assert [p["price_cents"] for p in points] == [100000, 98000, 95000]
    assert [p["captured_at"] for p in points] == sorted(p["captured_at"] for p in points)


def test_price_history_limit_keeps_the_newest_points(client):
    """Capping an ascending query would return the OLDEST points and plot a
    chart that stops before the present.
    """
    c, engine = client
    seed(engine, count=1)
    with Session(engine) as s:
        for i in range(1, 6):
            s.add(
                PriceSnapshot(
                    item_id="item0",
                    price_cents=100000 - i * 1000,
                    status="on_sale",
                    source="mtop",
                    captured_at=T0 + timedelta(hours=i),
                )
            )
        s.commit()

    points = c.get("/api/items/item0/prices?limit=2", headers=AUTH).json()
    assert [p["price_cents"] for p in points] == [96000, 95000]


def test_current_price_is_the_newest_by_time_not_by_insert_order(client):
    """A snapshot inserted later but stamped earlier must not become "current".

    Both writers append in real time today, so id order happens to match time
    order -- which is exactly why keying on max(id) would pass every other
    test and still read the wrong row after a backfill or a repair script.
    """
    c, engine = client
    seed(engine, count=1)  # item0 at T0, 100000
    with Session(engine) as s:
        s.add(
            PriceSnapshot(
                item_id="item0",
                price_cents=88000,
                status="on_sale",
                source="mtop",
                captured_at=T0 + timedelta(hours=3),
            )
        )
        s.commit()
        # Inserted last, stamped BEFORE the one above.
        s.add(
            PriceSnapshot(
                item_id="item0",
                price_cents=999000,
                status="on_sale",
                source="mtop",
                captured_at=T0 + timedelta(hours=1),
            )
        )
        s.commit()

    assert c.get("/api/items", headers=AUTH).json()[0]["price_cents"] == 88000
    assert c.get("/api/items/item0", headers=AUTH).json()["price_cents"] == 88000
