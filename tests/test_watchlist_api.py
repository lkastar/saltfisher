"""Watchlist endpoints, including the pasted-link path."""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.collector.base import CollectorError, ItemGoneError, RawItem, RawSeller
from app.db import get_session
from app.main import app
from app.models import Item, PriceSnapshot, Seller, Watchlist
from app.notify import build_registry

AUTH = {"Authorization": "Bearer testtoken123"}


def raw(item_id: str = "1081966784098", price_cents: int = 265000, **kw) -> RawItem:
    return RawItem(
        **{
            **dict(
                item_id=item_id,
                title="iPhone 15粉色 128 成色非常好",
                price_cents=price_cents,
                seller_id="2218219939144",
                seller_nick="彡灬念笙",
                source="detail",
                cover_url="https://cdn/a.jpg",
            ),
            **kw,
        }
    )


class StubPipeline:
    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[str] = []

    async def collect_item(self, item_id: str):
        self.calls.append(item_id)
        if self.raises:
            raise self.raises
        return raw(item_id), RawSeller(
            seller_id="2218219939144",
            nick="彡灬念笙",
            source="detail",
            credit_level=5,
            positive_rate=97.0,
            is_shop=False,
        )


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.state.pipeline = StubPipeline()
    app.state.notify_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    )
    app.state.notify_registry = build_registry(app.state.notify_client)
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def test_auth_required(client):
    c, _ = client
    assert c.get("/api/watchlist").status_code == 401


def test_add_by_pasted_link_fetches_a_baseline_immediately(client):
    """Without a baseline price and a title there is nothing to show and
    nothing to compare a later price against.
    """
    c, engine = client
    r = c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098", "note": "自用，能接受磕碰"},
        headers=AUTH,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["item_id"] == "1081966784098"
    assert body["price_cents"] == 265000
    assert body["added_price_cents"] == 265000
    assert body["note"] == "自用，能接受磕碰"
    assert body["seller_credit_level"] == 5

    with Session(engine) as s:
        assert s.get(Item, "1081966784098") is not None
        assert s.get(Seller, "2218219939144") is not None
        assert len(s.exec(select(PriceSnapshot)).all()) == 1


def test_add_by_item_id_for_an_item_already_known(client):
    c, engine = client
    with Session(engine) as s:
        s.add(Seller(id="s1", nick="老王"))
        s.commit()
        s.add(Item(id="known", title="iPhone", seller_id="s1", seller_nick="老王"))
        s.add(PriceSnapshot(item_id="known", price_cents=199900, status="on_sale", source="mtop"))
        s.commit()

    r = c.post("/api/watchlist", json={"item_id": "known"}, headers=AUTH)
    assert r.status_code == 201
    assert r.json()["added_price_cents"] == 199900
    assert app.state.pipeline.calls == [], "no fetch needed for a known item"


def test_exactly_one_source_is_required(client):
    c, _ = client
    assert c.post("/api/watchlist", json={}, headers=AUTH).status_code == 422
    assert (
        c.post("/api/watchlist", json={"item_id": "1", "url": "x"}, headers=AUTH).status_code == 422
    )


def test_unparseable_link_is_a_400_not_a_500(client):
    """Unparseable share text is ordinary user input, not a server fault."""
    c, _ = client
    r = c.post("/api/watchlist", json={"url": "这不是链接"}, headers=AUTH)
    assert r.status_code == 400
    assert "could not identify" in r.json()["detail"]


def test_a_listing_that_no_longer_exists_is_a_404(client):
    c, _ = client
    app.state.pipeline = StubPipeline(raises=ItemGoneError("deleted"))
    r = c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    assert r.status_code == 404
    assert "no longer exists" in r.json()["detail"]


def test_an_upstream_failure_is_a_502(client):
    c, _ = client
    app.state.pipeline = StubPipeline(raises=CollectorError("api down"))
    r = c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    assert r.status_code == 502


def test_adding_the_same_item_twice_is_a_409(client):
    c, _ = client
    payload = {"url": "https://www.goofish.com/item?id=1081966784098"}
    assert c.post("/api/watchlist", json=payload, headers=AUTH).status_code == 201
    r = c.post("/api/watchlist", json=payload, headers=AUTH)
    assert r.status_code == 409


def test_interval_floor_applies_to_watch_entries_too(client):
    """The anti-ban floor is not relaxed just because it is one item."""
    c, _ = client
    r = c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098", "interval_seconds": 30},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_list_reports_change_and_listing_duration(client):
    c, engine = client
    c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    with Session(engine) as s:
        s.add(
            PriceSnapshot(
                item_id="1081966784098", price_cents=239900, status="on_sale", source="detail"
            )
        )
        s.commit()

    row = c.get("/api/watchlist", headers=AUTH).json()[0]
    assert row["price_cents"] == 239900
    assert row["added_price_cents"] == 265000
    assert row["change_cents"] == -25100
    assert row["change_ratio"] < 0
    assert row["listed_days"] >= 0
    assert row["status"] == "on_sale"


def test_patch_note_and_toggle(client):
    c, _ = client
    c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    r = c.patch(
        "/api/watchlist/1081966784098",
        json={"note": "改主意了，只要全新", "price_watch_enabled": False},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["note"] == "改主意了，只要全新"
    assert r.json()["price_watch_enabled"] is False


def test_re_enabling_clears_the_failure_state(client):
    c, engine = client
    c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    with Session(engine) as s:
        entry = s.get(Watchlist, "1081966784098")
        entry.price_watch_enabled = False
        entry.consecutive_failures = 5
        entry.last_error = "auto-disabled"
        s.commit()

    body = c.patch(
        "/api/watchlist/1081966784098", json={"price_watch_enabled": True}, headers=AUTH
    ).json()
    assert body["price_watch_enabled"] is True
    assert body["last_error"] is None


def test_removing_an_entry_keeps_the_item_and_its_history(client):
    """Shared data and a useful price reference; only the watch goes away."""
    c, engine = client
    c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    assert c.delete("/api/watchlist/1081966784098", headers=AUTH).status_code == 204
    with Session(engine) as s:
        assert s.get(Watchlist, "1081966784098") is None
        assert s.get(Item, "1081966784098") is not None
        assert len(s.exec(select(PriceSnapshot)).all()) == 1


def test_missing_entry_is_404(client):
    c, _ = client
    assert c.patch("/api/watchlist/nope", json={"note": "x"}, headers=AUTH).status_code == 404
    assert c.delete("/api/watchlist/nope", headers=AUTH).status_code == 404


def test_the_note_is_the_llm_intent_input(client):
    """One field, two jobs: a reminder for the user and the {user_intent} the
    item advice needs. A second input box for the same sentence would be worse.
    """
    c, engine = client
    c.post(
        "/api/watchlist",
        json={
            "url": "https://www.goofish.com/item?id=1081966784098",
            "note": "自用不介意磕碰，预算 2500 内",
        },
        headers=AUTH,
    )
    with Session(engine) as s:
        assert "预算 2500 内" in s.get(Watchlist, "1081966784098").note


def test_the_profile_lands_on_the_row_the_item_points_at(client):
    """Found in T8. A search row carries an OPAQUE seller id; the detail
    response gives a NUMERIC one, and `upsert_item` never rewrites `seller_id`
    on an existing row. So writing the profile under the detail id put it on a
    row nothing references, and the watchlist showed 卖家信用未知 for a seller
    whose profile had just been fetched.

    For a link-added item whose seller no rule ever searches, no later cycle
    would ever fill the referenced row -- it stayed unknown forever.
    """
    c, engine = client
    with Session(engine) as s:
        s.add(Seller(id="opaque+id==", nick="彡灬念笙"))
        s.commit()
        s.add(
            Item(
                id="1081966784098",
                title="iPhone 15",
                seller_id="opaque+id==",
                seller_nick="彡灬念笙",
            )
        )
        s.add(
            PriceSnapshot(
                item_id="1081966784098", price_cents=265000, status="on_sale", source="mtop"
            )
        )
        s.commit()

    # The stub returns a RawSeller keyed by the numeric detail id.
    r = c.post(
        "/api/watchlist",
        json={"url": "https://www.goofish.com/item?id=1081966784098"},
        headers=AUTH,
    )
    assert r.status_code == 201

    body = r.json()
    assert body["seller_credit_level"] == 5, "the POST response must already show the profile"
    assert body["seller_positive_rate"] == 97.0

    with Session(engine) as s:
        referenced = s.get(Seller, "opaque+id==")
        assert referenced is not None
        assert referenced.credit_level == 5, "profile landed on the referenced row"
        # And no orphan appeared under the id the detail response used.
        assert s.get(Seller, "2218219939144") is None
