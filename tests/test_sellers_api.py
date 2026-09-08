"""POST /api/sellers/{id}/refresh: on-demand profile fetch.

Never touches the network: the pipeline is a stub, same as the watchlist API
tests. What matters is the TTL short-circuit (no upstream call for a fresh
profile), that the profile lands under the id the ITEMS point at, and that a
failure is a readable 502 recorded on the seller row.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.collector.base import RawSeller
from app.db import get_session
from app.main import app
from app.models import Item, Seller, utcnow

AUTH = {"Authorization": "Bearer testtoken123"}
NOW = utcnow()
OPAQUE_ID = "6uSUGlb2aPN1F6kZNEnN9Q=="


class StubPipeline:
    """Answers collect_seller_via_item like the real one: (seller, None) on
    success, (None, reason) on any failure — it never raises."""

    def __init__(self, seller: RawSeller | None = None, reason: str | None = None) -> None:
        self.seller = seller
        self.reason = reason
        self.calls: list[str] = []

    async def collect_seller_via_item(self, item_id: str):
        self.calls.append(item_id)
        return self.seller, self.reason


def profile() -> RawSeller:
    return RawSeller(
        # The detail response's OWN id space, deliberately different from
        # OPAQUE_ID: the route must re-key onto the id the items point at.
        seller_id="2218219939144",
        nick="彡灬念笙",
        source="detail",
        credit_level=5,
        positive_rate=97.0,
        sold_count=120,
        is_shop=False,
    )


def seed(engine, *, fetched_at=None) -> None:
    with Session(engine) as s:
        s.add(Seller(id=OPAQUE_ID, nick="彡灬念笙", fetched_at=fetched_at))
        s.commit()
        s.add(
            Item(
                id="1081966784098",
                title="iPhone 15粉色 128",
                seller_id=OPAQUE_ID,
                seller_nick="彡灬念笙",
                last_seen_at=NOW,
            )
        )
        s.add(
            Item(
                id="1081966784099",
                title="旧一点的",
                seller_id=OPAQUE_ID,
                seller_nick="彡灬念笙",
                last_seen_at=NOW - timedelta(days=3),
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
    app.state.pipeline = StubPipeline(seller=profile())
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def test_auth_required(client):
    c, _ = client
    assert c.post(f"/api/sellers/{OPAQUE_ID}/refresh").status_code == 401


def test_unknown_seller_is_404(client):
    c, _ = client
    assert c.post("/api/sellers/nope/refresh", headers=AUTH).status_code == 404


def test_a_fresh_profile_skips_the_upstream_call(client):
    """The TTL exists to keep request volume down; the button must not become
    a way around it."""
    c, engine = client
    seed(engine, fetched_at=NOW)  # well inside seller_profile_ttl_days

    r = c.post(f"/api/sellers/{OPAQUE_ID}/refresh", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["refreshed"] is False
    assert body["seller_id"] == OPAQUE_ID
    assert app.state.pipeline.calls == [], "no upstream request for a fresh profile"


def test_a_stale_profile_is_fetched_through_the_newest_item(client):
    c, engine = client
    seed(engine, fetched_at=NOW - timedelta(days=30))

    r = c.post(f"/api/sellers/{OPAQUE_ID}/refresh", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["refreshed"] is True
    assert body["seller_credit_level"] == 5
    assert body["seller_sold_count"] == 120
    assert body["fetch_error"] is None
    # One upstream request, through the most recently seen item.
    assert app.state.pipeline.calls == ["1081966784098"]

    with Session(engine) as s:
        stored = s.get(Seller, OPAQUE_ID)
        assert stored is not None and stored.credit_level == 5
        assert stored.fetched_at is not None
        # Re-keyed onto the id the items point at — no orphan row under the
        # detail response's numeric id.
        assert s.get(Seller, "2218219939144") is None


def test_a_never_fetched_profile_counts_as_stale(client):
    c, engine = client
    seed(engine, fetched_at=None)
    r = c.post(f"/api/sellers/{OPAQUE_ID}/refresh", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["refreshed"] is True


def test_an_unavailable_profile_is_a_502_and_recorded(client):
    """Same error shape as the other collector-touching endpoints, plus the
    reason lands on Seller.fetch_error where the panel shows it."""
    c, engine = client
    seed(engine)
    app.state.pipeline = StubPipeline(seller=None, reason="ChallengeError: 需要验证")

    r = c.post(f"/api/sellers/{OPAQUE_ID}/refresh", headers=AUTH)
    assert r.status_code == 502
    assert "需要验证" in r.json()["detail"]
    with Session(engine) as s:
        stored = s.get(Seller, OPAQUE_ID)
        assert stored is not None and stored.fetch_error == "ChallengeError: 需要验证"


def test_a_seller_with_no_items_is_409_not_500(client):
    """The only upstream route to a profile is an item detail; without an item
    there is nothing to fetch through, and that is a client-visible state."""
    c, engine = client
    with Session(engine) as s:
        s.add(Seller(id="lonely", nick="没货"))
        s.commit()
    r = c.post("/api/sellers/lonely/refresh", headers=AUTH)
    assert r.status_code == 409
    assert app.state.pipeline.calls == []
