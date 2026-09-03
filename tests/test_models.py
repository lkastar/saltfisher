"""Guards for the invariants the rest of the codebase assumes.

Not a schema snapshot test — these assert the four field-level constraints
that T1's review gate calls out, because a wrong choice here is expensive to
change once real data exists.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from app import models
from app.models import Item, Monitor, PriceSnapshot, Seller, Watchlist

EXPECTED_TABLES = {
    "monitor",
    "item",
    "pricesnapshot",
    "monitorhit",
    "watchlist",
    "seller",
    "notifychannel",
    "monitorchannel",
    "notifylog",
    "llmendpoint",
    "llmscenarioconfig",
}


def test_all_tables_registered():
    assert EXPECTED_TABLES <= set(SQLModel.metadata.tables)


def test_money_columns_are_integers():
    """Float prices produce phantom price-drop alerts."""
    money = [
        (cls, name)
        for cls in (Monitor, PriceSnapshot, Watchlist)
        for name in cls.model_fields
        if name.endswith("_cents")
    ]
    assert money, "no *_cents columns found — naming convention broken"
    for cls, name in money:
        anno = str(cls.model_fields[name].annotation)
        assert "float" not in anno, f"{cls.__name__}.{name} is {anno}"
        assert "int" in anno, f"{cls.__name__}.{name} is {anno}"


def test_upstream_ids_are_strings():
    """goofish ids exceed 32-bit range and appear zero-padded."""
    assert Item.model_fields["id"].annotation is str
    assert Seller.model_fields["id"].annotation is str
    assert Item.model_fields["seller_id"].annotation is str
    assert PriceSnapshot.model_fields["item_id"].annotation is str


def test_seller_profile_fields_are_optional():
    """None ("not fetched") must be distinguishable from 0."""
    for name in ("is_shop", "credit_level", "credit_score", "verified", "sold_count"):
        assert type(None) in models.Seller.model_fields[name].annotation.__args__, name
    assert Seller(id="s1", nick="x").credit_level is None


def test_interval_floor_is_enforced_by_storage(session):
    """The 60s floor is an anti-ban rule, so no write path may bypass it.

    SQLModel skips Pydantic validation on table=True models, so Field(ge=...)
    does NOT reject a bad value here — a CHECK constraint does. Request-level
    422s come from the non-table request schemas added in T3.
    """
    session.add(Monitor(name="ok", keyword="x", interval_seconds=60))
    session.commit()

    session.add(Monitor(name="too fast", keyword="x", interval_seconds=30))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(Seller(id="s9", nick="n"))
    session.add(Item(id="i9", title="t", seller_id="s9", seller_nick="n"))
    session.commit()
    session.add(Watchlist(item_id="i9", added_price_cents=1, interval_seconds=59))
    with pytest.raises(IntegrityError):
        session.commit()


def test_timestamps_survive_a_round_trip_as_utc_aware(session):
    """SQLite returns naive datetimes; UtcDateTime coerces them back.

    Without this, arithmetic between a DB value and utcnow() raises TypeError
    somewhere far from the cause.
    """
    session.add(Seller(id="s1", nick="seller"))
    session.add(Item(id="i1", title="t", seller_id="s1", seller_nick="seller"))
    session.commit()
    session.expire_all()

    item = session.exec(select(Item)).one()
    assert item.first_seen_at.tzinfo is not None
    assert item.first_seen_at.utcoffset() == timedelta(0)
    # the actual thing this protects: comparing with utcnow() must not raise
    assert datetime.now(UTC) - item.first_seen_at >= timedelta(0)


def test_aware_input_is_stored_and_returned_as_utc(session):
    """A collector parsing publish_time with a +08:00 offset must not shift."""
    shanghai = timezone(timedelta(hours=8))
    published = datetime(2026, 9, 4, 10, 0, tzinfo=shanghai)
    session.add(Seller(id="s2", nick="seller"))
    session.add(
        Item(id="i2", title="t", seller_id="s2", seller_nick="seller", publish_time=published)
    )
    session.commit()
    session.expire_all()

    item = session.exec(select(Item).where(Item.id == "i2")).one()
    assert item.publish_time == published
    assert item.publish_time.utcoffset() == timedelta(0)  # normalised to UTC


def test_price_snapshot_is_append_only_shaped(session):
    """Two observations of one item coexist; nothing forces an update-in-place."""
    session.add(Seller(id="s1", nick="seller"))
    session.add(Item(id="i1", title="t", seller_id="s1", seller_nick="seller"))
    session.add(PriceSnapshot(item_id="i1", price_cents=1000, status="on_sale", source="mtop"))
    session.add(PriceSnapshot(item_id="i1", price_cents=900, status="on_sale", source="mtop"))
    session.commit()
    assert len(session.exec(select(PriceSnapshot)).all()) == 2
