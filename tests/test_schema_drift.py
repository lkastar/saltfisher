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
from app.models import CollectRun, MonitorHit, Seller


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


def test_a_pre_paging_run_log_gains_its_pages_column():
    """P3 added CollectRun.pages to a table that already has rows on every
    running instance. Additive and nullable, so startup handles it — and NULL
    is the honest value for a cycle collected before paging existed, which is
    why the column is not `int = 0`.
    """
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE collectrun DROP COLUMN pages"))
        conn.execute(
            text(
                "INSERT INTO collectrun (started_at, ok, item_count) "
                "VALUES ('2026-09-04 00:00:00.000000', 1, 30)"
            )
        )

    applied = add_missing_columns(engine)

    assert any("collectrun" in ddl and "pages" in ddl for ddl in applied)
    assert "pages" in columns(engine, "collectrun")

    with Session(engine) as s:
        (row,) = s.exec(select(CollectRun)).all()
        assert row.item_count == 30
        # Unknown aperture, not zero pages: the row predates the question.
        assert row.pages is None


def test_a_pre_p5_seller_table_gains_its_listing_count_column():
    """P5 gave `Seller` the `listing_count` column its value had been parsed
    into and dropped from since M1 (`sellerDO.itemCount`). Additive and
    nullable, so startup handles it — and NULL is the honest value for a seller
    whose profile was fetched before the column existed, because nobody stored
    the count then.
    """
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE seller DROP COLUMN listing_count"))
        conn.execute(
            text("INSERT INTO seller (id, nick, credit_level) VALUES ('s1', '小顾数码', 5)")
        )

    applied = add_missing_columns(engine)

    assert any("seller" in ddl and "listing_count" in ddl for ddl in applied)
    with Session(engine) as s:
        seller = s.get(Seller, "s1")
        assert seller.credit_level == 5
        # Not fetched, not zero listings.
        assert seller.listing_count is None


def test_a_leftover_credit_score_column_is_harmless():
    """P5 deleted `Seller.credit_score` (goofish gives a LEVEL, never a score —
    see `test_detail_shape`), and `add_missing_columns` only ever adds, so the
    dead column stays behind on every already-deployed database.

    That is the stated reason no migration script was written, so it is
    asserted rather than assumed: SQLModel selects the columns it knows by
    name, so an extra one is invisible.
    """
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE seller ADD COLUMN credit_score INTEGER"))
        conn.execute(
            text(
                "INSERT INTO seller (id, nick, credit_level, credit_score) VALUES ('s1', 'x', 5, 7)"
            )
        )

    assert add_missing_columns(engine) == []
    with Session(engine) as s:
        (seller,) = s.exec(select(Seller)).all()
        assert seller.credit_level == 5
        assert not hasattr(seller, "credit_score")


def test_a_pre_paging_ledger_gains_its_last_hit_at_column():
    """The other column P3 added, and the one that actually bit: reading the
    real database before startup had run gave `no such column:
    monitorhit.last_hit_at`.

    NULL is load-bearing here rather than merely tolerated -- it is what
    `analytics.listing_duration` detects to fall back to the global
    `Item.last_seen_at` clock and to report `legacy_clock_rows`. A default of
    utcnow() would have claimed every historical listing was last seen at
    upgrade time, i.e. that nothing had ever left the observation range.
    """
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE monitorhit DROP COLUMN last_hit_at"))
        conn.execute(
            text(
                "INSERT INTO monitorhit (monitor_id, item_id, first_hit_at, in_range) "
                "VALUES (1, 'i1', '2026-09-04 00:00:00.000000', 1)"
            )
        )

    applied = add_missing_columns(engine)

    assert any("monitorhit" in ddl and "last_hit_at" in ddl for ddl in applied)
    with Session(engine) as s:
        (row,) = s.exec(select(MonitorHit)).all()
        assert row.item_id == "i1"
        # NULL, not 0 and not "now": the fallback depends on telling
        # "never stamped" apart from "stamped a long time ago".
        assert row.last_hit_at is None


def test_a_pre_p5_seller_table_gains_its_numeric_id_column():
    """P5/T2a added `Seller.numeric_id` -- the numeric `userId` the
    seller-listing API takes, which search never returns.

    Additive and nullable, so startup handles it and it stays OUT of
    `scripts/migrations/001`: only `monitor` needed a rebuild there. NULL is
    the honest value for every seller row that already exists, because the
    mapping was parsed and dropped before the column existed.
    """
    from tests.conftest import memory_engine

    engine = memory_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE seller DROP COLUMN numeric_id"))
        conn.execute(
            text(
                "INSERT INTO seller (id, nick, listing_count) VALUES ('opaque==', '小顾数码', 189)"
            )
        )

    applied = add_missing_columns(engine)

    assert any("seller" in ddl and "numeric_id" in ddl for ddl in applied)
    with Session(engine) as s:
        seller = s.get(Seller, "opaque==")
        assert seller.listing_count == 189
        # Unresolved, not "the same as id" -- guessing that would be wrong for
        # 251 of the 253 real rows.
        assert seller.numeric_id is None
