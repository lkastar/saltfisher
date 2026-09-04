"""Additive schema reconciliation on startup.

`create_all()` creates missing TABLES and never alters existing ones, so a new
model field left every already-deployed database without that column. Tests
cannot reproduce that by accident: they build the schema fresh from the current
models every run, which is exactly why the drift shipped unnoticed and broke
/api/watchlist. These tests build the stale schema on purpose.
"""

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, select

from app.db import add_missing_columns
from app.models import Seller


@pytest.fixture
def stale_engine():
    """An engine whose `seller` table predates review_count/positive_rate."""
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        for column in ("review_count", "positive_rate"):
            conn.execute(text(f"ALTER TABLE seller DROP COLUMN {column}"))
    return engine


def columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_the_stale_schema_really_is_broken(stale_engine):
    """Guards the fixture. If this ever passes, the test below proves nothing."""
    with Session(stale_engine) as s, pytest.raises(Exception, match="review_count"):
        s.exec(select(Seller)).all()


def test_create_all_does_not_add_the_missing_columns(stale_engine):
    """The claim that motivated all of this, asserted rather than assumed."""
    SQLModel.metadata.create_all(stale_engine)
    assert "review_count" not in columns(stale_engine, "seller")


def test_missing_nullable_columns_are_added(stale_engine):
    applied = add_missing_columns(stale_engine)

    assert columns(stale_engine, "seller") >= {"review_count", "positive_rate"}
    assert len(applied) == 2
    with Session(stale_engine) as s:
        s.add(Seller(id="s1", nick="老王", review_count=12, positive_rate=98.5))
        s.commit()
        seller = s.get(Seller, "s1")
        assert seller.review_count == 12
        assert seller.positive_rate == 98.5


def test_existing_rows_keep_their_data_and_read_null(stale_engine):
    """The point of adding rather than recreating: history survives."""
    with stale_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO seller (id, nick, credit_level) VALUES ('old', '阿芳阿芳', 4)")
        )

    add_missing_columns(stale_engine)

    with Session(stale_engine) as s:
        seller = s.get(Seller, "old")
        assert seller.nick == "阿芳阿芳"
        assert seller.credit_level == 4
        # Unknown, not zero.
        assert seller.review_count is None
        assert seller.positive_rate is None


def test_running_twice_changes_nothing(stale_engine):
    assert len(add_missing_columns(stale_engine)) == 2
    assert add_missing_columns(stale_engine) == []


def test_an_up_to_date_database_is_left_alone():
    from tests.conftest import memory_engine

    assert add_missing_columns(memory_engine()) == []
