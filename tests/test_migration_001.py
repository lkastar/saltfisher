"""The project's first migration: `monitor` rebuilt for seller rules.

Same reason `test_schema_drift.py` exists, one step further. That file's
opening still applies verbatim -- **tests build their schema fresh from the
current models every run, so they can never reproduce drift by accident** --
and a migration tested against a database `create_all()` just built from the
post-change models proves precisely nothing: it would already be migrated.

So the `monitor` table here is created from the **pre-P5 DDL, written out by
hand** below: `keyword` NOT NULL, no `seller_id`, no `rule_target_xor`. The
other tables come from the current models on purpose -- this migration does
not touch them, and their real foreign keys into `monitor` are the thing the
cross-table assertions need to be genuine.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

ROOT = Path(__file__).resolve().parent.parent


def _load_migration():
    """`001_...` is not an importable name, which is the point of the digits."""
    path = ROOT / "scripts" / "migrations" / "001_monitor_seller_rules.py"
    spec = importlib.util.spec_from_file_location("migration_001", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


m001 = _load_migration()


# The monitor table as it shipped through P4, transcribed rather than
# generated. Three differences from the current model, and every acceptance
# criterion is about one of them: keyword is NOT NULL, seller_id does not
# exist, and interval_floor is the only CHECK.
STALE_MONITOR_DDL = """
CREATE TABLE monitor (
    id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    keyword VARCHAR NOT NULL,
    exclude_words VARCHAR NOT NULL,
    price_min_cents INTEGER,
    price_max_cents INTEGER,
    published_within_hours INTEGER,
    region VARCHAR,
    condition VARCHAR,
    free_shipping BOOLEAN,
    min_seller_credit INTEGER,
    exclude_shop BOOLEAN NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL,
    baseline_done BOOLEAN NOT NULL,
    last_run_at DATETIME,
    last_error VARCHAR,
    last_collector VARCHAR,
    consecutive_failures INTEGER NOT NULL,
    hit_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT interval_floor CHECK (interval_seconds >= 60)
)
"""

# Two rules with every column populated differently, so "each column is equal
# row by row" can actually fail if the copy shifts a value one column over.
SEED_MONITORS = """
INSERT INTO monitor (
    id, name, keyword, exclude_words, price_min_cents, price_max_cents,
    published_within_hours, region, condition, free_shipping,
    min_seller_credit, exclude_shop, interval_seconds, enabled,
    baseline_done, last_run_at, last_error, last_collector,
    consecutive_failures, hit_count, created_at
) VALUES
    (1, 'iPhone 15', 'iPhone 15', '碎屏 主板', 200000, 500000, 48, '上海',
     '几乎全新', 1, 4, 0, 300, 1, 1, '2026-09-04 10:00:00.000000', NULL,
     'mtop', 0, 264, '2026-08-31 01:02:03.000000'),
    (2, '窄的那条', 'iPhone 15 128G', '', NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, 1, 600, 0, 0, NULL, 'auto-disabled after 5 failures: boom', NULL,
     5, 35, '2026-09-04 09:00:00.000000')
"""


def _stale_db(tmp_path: Path) -> Path:
    """Every table from the current models, then `monitor` swapped for the
    version this migration is supposed to find."""
    path = tmp_path / "app.db"
    import app.models  # noqa: F401  (register tables before create_all)

    SQLModel.metadata.create_all(create_engine(f"sqlite:///{path}"))

    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("DROP TABLE monitor")
        con.execute(STALE_MONITOR_DDL)
        con.execute(SEED_MONITORS)
        # A seller and an item so the dependents below have somewhere to point.
        con.execute("INSERT INTO seller (id, nick) VALUES ('c2FsdA==', '小顾数码')")
        con.execute(
            "INSERT INTO item (id, title, seller_id, seller_nick, first_seen_at, "
            "last_seen_at, status) VALUES ('i1', '标题', 'c2FsdA==', '小顾数码', "
            "'2026-09-04 00:00:00.000000', '2026-09-04 00:00:00.000000', 'on_sale')"
        )
        con.execute(
            "INSERT INTO notifychannel (id, kind, label, config, enabled, created_at) "
            "VALUES (7, 'email', '我的邮箱', '{}', 1, '2026-09-01 00:00:00.000000')"
        )
        # The three tables whose foreign keys point at monitor.id.
        con.execute(
            "INSERT INTO monitorhit (monitor_id, item_id, first_hit_at, in_range) "
            "VALUES (1, 'i1', '2026-09-04 00:00:00.000000', 1)"
        )
        con.execute("INSERT INTO monitorchannel (monitor_id, channel_id) VALUES (1, 7)")
        con.execute(
            "INSERT INTO collectrun (id, monitor_id, started_at, ok, item_count, pages) "
            "VALUES (1, 2, '2026-09-04 00:00:00.000000', 1, 30, 1)"
        )
    finally:
        con.close()
    return path


@pytest.fixture
def stale_db(tmp_path: Path) -> Path:
    return _stale_db(tmp_path)


def schema_sql(path: Path, table: str = "monitor") -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def columns(path: Path, table: str = "monitor") -> dict[str, tuple]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[1]: row for row in con.execute(f'PRAGMA table_info("{table}")')}
    finally:
        con.close()


def query(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(con.execute(sql))
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# The fixture is the experiment. Guard it first.
# --------------------------------------------------------------------------- #


def test_the_stale_schema_really_is_pre_p5(stale_db):
    """If this ever passes, every test below proves nothing."""
    info = columns(stale_db)
    assert "seller_id" not in info
    assert info["keyword"][3] == 1, "keyword should still be NOT NULL"
    assert "rule_target_xor" not in schema_sql(stale_db)


def test_the_stale_schema_rejects_what_a_seller_rule_needs(stale_db):
    con = sqlite3.connect(stale_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            con.execute(
                "INSERT INTO monitor (id, name, keyword, exclude_words, exclude_shop, "
                "interval_seconds, enabled, baseline_done, consecutive_failures, "
                "hit_count, created_at) VALUES (9, 'x', NULL, '', 0, 300, 1, 0, 0, 0, "
                "'2026-09-08 00:00:00.000000')"
            )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# What the migration changes
# --------------------------------------------------------------------------- #


def test_all_three_schema_changes_land(stale_db, tmp_path):
    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    info = columns(stale_db)
    assert info["keyword"][3] == 0, "keyword must be nullable"
    assert "seller_id" in info
    assert "rule_target_xor" in schema_sql(stale_db)
    # The old constraint is not collateral damage.
    assert "interval_floor" in schema_sql(stale_db)


def test_not_one_row_and_not_one_column_is_lost(stale_db, tmp_path):
    """Row count AND every column of every row, asserted rather than eyeballed.

    The comparison is per column and not `count(*)`: a shifted INSERT ... SELECT
    keeps the count perfectly and moves 'iPhone 15' into `exclude_words`.
    """
    old_columns = [c for c in columns(stale_db)]
    before = query(stale_db, f"SELECT {', '.join(old_columns)} FROM monitor ORDER BY id")
    assert len(before) == 2

    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    after = query(stale_db, f"SELECT {', '.join(old_columns)} FROM monitor ORDER BY id")
    assert len(after) == len(before)
    assert after == before
    # And the column that did not exist before reads NULL, not '' -- these are
    # keyword rules and they target no seller.
    assert query(stale_db, "SELECT seller_id FROM monitor ORDER BY id") == [(None,), (None,)]


def test_the_index_on_the_new_column_exists(stale_db, tmp_path):
    m001.migrate(stale_db, backup_dir=tmp_path / "backups")
    names = {row[0] for row in query(stale_db, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_monitor_seller_id" in names


def test_an_index_the_old_table_carried_is_replayed(stale_db, tmp_path):
    """DROP TABLE takes a table's indexes with it, so they are captured and
    re-run. A stock monitor table has none; a hand-added one on a real
    deployment must not be the migration's casualty."""
    con = sqlite3.connect(stale_db, isolation_level=None)
    try:
        con.execute("CREATE INDEX ix_monitor_hand_rolled ON monitor (keyword)")
    finally:
        con.close()

    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    names = {row[0] for row in query(stale_db, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_monitor_hand_rolled" in names


def test_the_dependents_still_point_at_their_rule(stale_db, tmp_path):
    """`monitor` is referenced by three tables, so asserting on `monitor`
    alone would miss the failure that matters: a rebuild that leaves
    monitorhit / monitorchannel / collectrun orphaned."""
    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    assert query(stale_db, "PRAGMA foreign_key_check") == []
    # Not just "the pragma is happy" -- the joins have to still resolve.
    assert query(
        stale_db,
        "SELECT h.item_id FROM monitorhit h JOIN monitor m ON m.id = h.monitor_id",
    ) == [("i1",)]
    assert query(
        stale_db,
        "SELECT c.channel_id FROM monitorchannel c JOIN monitor m ON m.id = c.monitor_id",
    ) == [(7,)]
    assert query(
        stale_db,
        "SELECT r.item_count FROM collectrun r JOIN monitor m ON m.id = r.monitor_id",
    ) == [(30,)]
    # And with enforcement back on, a write through those keys still works.
    con = sqlite3.connect(stale_db, isolation_level=None)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("INSERT INTO monitorchannel (monitor_id, channel_id) VALUES (2, 7)")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO monitorchannel (monitor_id, channel_id) VALUES (404, 7)")
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_running_twice_migrates_once(stale_db, tmp_path):
    first = m001.migrate(stale_db, backup_dir=tmp_path / "backups")
    second = m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    assert "rebuilt monitor" in first
    assert "already migrated" in second
    assert query(stale_db, "SELECT count(*) FROM monitor") == [(2,)]
    # The no-op does not even take a backup: there is nothing to protect.
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_a_half_migrated_table_is_still_rebuilt(stale_db, tmp_path):
    """The state a real database is actually in.

    `seller_id` is nullable, so `db.add_missing_columns()` adds it on the very
    next startup -- while `keyword` stays NOT NULL and the CHECK stays absent,
    because that function cannot do either. An idempotency test that asked
    only "does seller_id exist" would skip every deployed database.
    """
    con = sqlite3.connect(stale_db, isolation_level=None)
    try:
        con.execute('ALTER TABLE monitor ADD COLUMN "seller_id" VARCHAR')
    finally:
        con.close()

    probe = sqlite3.connect(stale_db)
    try:
        assert m001.is_migrated(probe) is False
    finally:
        probe.close()

    m001.migrate(stale_db, backup_dir=tmp_path / "backups")
    assert columns(stale_db)["keyword"][3] == 0
    assert "rule_target_xor" in schema_sql(stale_db)


def test_add_missing_columns_really_cannot_do_this(stale_db):
    """The claim the whole script rests on, asserted rather than assumed."""
    from app.db import add_missing_columns

    engine = create_engine(f"sqlite:///{stale_db}")
    add_missing_columns(engine)
    engine.dispose()

    # It adds the nullable column, and that is all it can do.
    assert "seller_id" in columns(stale_db)
    assert columns(stale_db)["keyword"][3] == 1
    assert "rule_target_xor" not in schema_sql(stale_db)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_backup_is_written_before_the_rebuild(stale_db, tmp_path):
    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    (copy,) = (tmp_path / "backups").glob("pre-001-*.db")
    # The pre-migration table, complete, in the file the rollback plan names.
    assert query(copy, "SELECT count(*) FROM monitor") == [(2,)]
    assert "seller_id" not in columns(copy)


def test_a_failed_backup_aborts_the_migration(stale_db, tmp_path):
    """Not best-effort. A migration with no rollback does not run."""
    wall = tmp_path / "not-a-dir"
    wall.write_text("this is a file, so mkdir under it cannot work")

    with pytest.raises(m001.Refused, match="backup"):
        m001.migrate(stale_db, backup_dir=wall / "backups")

    # Untouched, not half-done.
    assert "seller_id" not in columns(stale_db)
    assert query(stale_db, "SELECT count(*) FROM monitor") == [(2,)]


def test_a_database_in_use_is_refused(stale_db, tmp_path):
    holder = sqlite3.connect(stale_db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(m001.Refused, match="in use"):
            m001.migrate(stale_db, backup_dir=tmp_path / "backups")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert "seller_id" not in columns(stale_db)
    assert not list((tmp_path / "backups").glob("*.db"))


def test_the_cli_refuses_without_the_explicit_flag(stale_db, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["001_monitor_seller_rules.py", "--db", str(stale_db)])
    assert m001.main() == 1
    assert "--i-stopped-the-app" in capsys.readouterr().err
    assert "seller_id" not in columns(stale_db)


def test_the_cli_migrates_and_then_says_so(stale_db, capsys, monkeypatch):
    argv = ["001_monitor_seller_rules.py", "--db", str(stale_db), "--i-stopped-the-app"]
    monkeypatch.setattr(sys, "argv", argv)

    assert m001.main() == 0
    assert "rebuilt monitor" in capsys.readouterr().out
    assert m001.main() == 0
    assert "already migrated" in capsys.readouterr().out


def test_a_database_without_the_table_is_refused(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(m001.Refused, match="no monitor table"):
        m001.migrate(empty, backup_dir=tmp_path / "backups")


# --------------------------------------------------------------------------- #
# The constraint itself, at the layer that has to hold it
# --------------------------------------------------------------------------- #


@pytest.fixture(params=["migrated", "fresh"])
def xor_db(request, tmp_path) -> Path:
    """Both ways a database can arrive at the current schema.

    A migrated database and a `create_all()` one have to enforce the same
    thing, and only the second is what tests would otherwise ever see.
    """
    if request.param == "fresh":
        path = tmp_path / "fresh.db"
        import app.models  # noqa: F401

        engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()
        con = sqlite3.connect(path, isolation_level=None)
        try:
            con.execute("INSERT INTO seller (id, nick) VALUES ('c2FsdA==', '小顾数码')")
        finally:
            con.close()
        return path
    path = _stale_db(tmp_path)
    m001.migrate(path, backup_dir=tmp_path / "backups")
    return path


def _insert(path: Path, keyword: str | None, seller_id: str | None) -> None:
    """A rule written by hand through sqlite3, bypassing every app layer.

    `hit_count` is read off the ACTUAL schema rather than hardcoded, because
    the two fixtures below are at different schema versions on purpose: the
    `migrated` database stops at 001 and still carries the column (NOT NULL,
    no default, so it cannot simply be omitted), while `fresh` is built from
    today's models, where 002 dropped it. Both must still prove the CHECK.
    """
    con = sqlite3.connect(path, isolation_level=None)
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(monitor)")]
        extra = ", hit_count" if "hit_count" in columns else ""
        extra_value = ", 0" if "hit_count" in columns else ""
        con.execute(
            "INSERT INTO monitor (name, keyword, seller_id, exclude_words, exclude_shop, "
            f"interval_seconds, enabled, baseline_done, consecutive_failures{extra}, "
            f"created_at) VALUES ('x', ?, ?, '', 0, 300, 1, 0, 0{extra_value}, "
            "'2026-09-08 00:00:00.000000')",
            (keyword, seller_id),
        )
    finally:
        con.close()


def test_the_database_itself_rejects_a_rule_with_both_targets(xor_db):
    with pytest.raises(sqlite3.IntegrityError, match="rule_target_xor"):
        _insert(xor_db, "iPhone 15", "c2FsdA==")


def test_the_database_itself_rejects_a_rule_with_neither_target(xor_db):
    with pytest.raises(sqlite3.IntegrityError, match="rule_target_xor"):
        _insert(xor_db, None, None)


def test_the_database_accepts_each_kind_of_rule_on_its_own(xor_db):
    _insert(xor_db, "iPhone 15", None)
    _insert(xor_db, None, "c2FsdA==")
    assert query(xor_db, "SELECT count(*) FROM monitor WHERE keyword IS NOT NULL")[0][0] >= 1
    assert query(xor_db, "SELECT count(*) FROM monitor WHERE seller_id IS NOT NULL")[0][0] == 1


def test_a_blank_keyword_satisfies_the_check_which_is_why_422_is_elsewhere(xor_db):
    """Documented because it looks like a hole and is not one.

    `keyword = ''` is NOT NULL, so the CHECK passes it: this constraint means
    "exactly one of the two", never "non-blank". Rejecting a blank keyword is
    `MonitorBase`'s job precisely so it comes back as a 422 rather than as a
    500 from here. See test_monitors_api's blank-keyword test.
    """
    _insert(xor_db, "", None)
    assert query(xor_db, "SELECT count(*) FROM monitor WHERE keyword = ''") == [(1,)]


def test_a_pre_existing_orphan_stops_the_migration(stale_db, tmp_path):
    """`PRAGMA foreign_key_check` is a gate, not a comment.

    A ledger row pointing at a rule that does not exist is planted BEFORE the
    rebuild, so the inconsistency is not the migration's doing -- and that is
    exactly why it must not commit over it. The last free ROLLBACK is here.
    """
    con = sqlite3.connect(stale_db, isolation_level=None)
    try:
        con.execute(
            "INSERT INTO monitorhit (monitor_id, item_id, first_hit_at, in_range) "
            "VALUES (404, 'i1', '2026-09-04 00:00:00.000000', 1)"
        )
    finally:
        con.close()

    with pytest.raises(m001.Refused, match="orphaned row"):
        m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    # Rolled back whole: the old table, with its old constraints and all of
    # its rows, is still the one in the file.
    assert "seller_id" not in columns(stale_db)
    assert columns(stale_db)["keyword"][3] == 1
    assert query(stale_db, "SELECT count(*) FROM monitor") == [(2,)]
    assert query(stale_db, "SELECT name FROM sqlite_master WHERE name='monitor_new'") == []


def test_the_interval_floor_is_carried_over_rather_than_reset(stale_db, tmp_path, monkeypatch):
    """`interval_floor` comes from `SFD_MIN_INTERVAL_SECONDS`, so a literal 60
    in the frozen DDL would quietly relax an anti-ban floor a deployment had
    raised -- while migrating something else entirely."""
    monkeypatch.setattr(m001.settings, "min_interval_seconds", 120)

    m001.migrate(stale_db, backup_dir=tmp_path / "backups")

    assert "interval_seconds >= 120" in schema_sql(stale_db)
    con = sqlite3.connect(stale_db, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="interval_floor"):
            con.execute("UPDATE monitor SET interval_seconds = 60 WHERE id = 1")
    finally:
        con.close()
