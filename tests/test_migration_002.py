"""002 — dropping `monitor.hit_count`.

The column held a count that drifted from the ledger it duplicated, so the
tests that matter are about the migration REFUSING when it cannot prove it is
safe, and about every rule surviving. The drop itself is one statement.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrations" / "002_drop_monitor_hit_count.py"

# Digits make the filename non-importable, so it is loaded by path -- the same
# approach test_migration_001.py uses.
spec = importlib.util.spec_from_file_location("m002", SCRIPT)
assert spec is not None and spec.loader is not None
m002 = importlib.util.module_from_spec(spec)
sys.modules["m002"] = m002
spec.loader.exec_module(m002)

# The shape as of 001, which is what a real database looks like before this
# migration. Hand-written on purpose: a shipped migration is tested against the
# schema it was written for, not against whatever the models become later.
OLD_SCHEMA = """
CREATE TABLE monitor (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR NOT NULL,
    keyword VARCHAR,
    seller_id VARCHAR,
    exclude_words VARCHAR NOT NULL DEFAULT '',
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    baseline_done BOOLEAN NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT rule_target_xor CHECK (
        (keyword IS NOT NULL AND seller_id IS NULL)
        OR (keyword IS NULL AND seller_id IS NOT NULL)
    )
);
CREATE TABLE monitorhit (
    monitor_id INTEGER NOT NULL,
    item_id VARCHAR NOT NULL,
    first_hit_at DATETIME NOT NULL,
    in_range BOOLEAN NOT NULL DEFAULT 1,
    PRIMARY KEY (monitor_id, item_id),
    FOREIGN KEY (monitor_id) REFERENCES monitor (id)
);
"""


@pytest.fixture
def old_db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.executescript(OLD_SCHEMA)
        # hit_count deliberately DISAGREES with the ledger, because that is the
        # state this migration exists to end: rule 1 notified 2 of its 3 hits,
        # rule 2 is fresh off a baseline cycle and notified none of its 2.
        for rule_id, keyword, stored in ((1, "iPhone 15", 2), (2, "索尼 a7c2", 0)):
            con.execute(
                "INSERT INTO monitor (id, name, keyword, exclude_words, hit_count, created_at) "
                "VALUES (?, ?, ?, '', ?, '2026-09-12 00:00:00.000000')",
                (rule_id, f"rule {rule_id}", keyword, stored),
            )
        for rule_id, count in ((1, 3), (2, 2)):
            for n in range(count):
                con.execute(
                    "INSERT INTO monitorhit (monitor_id, item_id, first_hit_at) "
                    "VALUES (?, ?, '2026-09-12 00:00:00.000000')",
                    (rule_id, f"item-{rule_id}-{n}"),
                )
    finally:
        con.close()
    return path


def _columns(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [row[1] for row in con.execute('PRAGMA table_info("monitor")')]
    finally:
        con.close()


def test_it_drops_the_column_and_keeps_every_rule(old_db, tmp_path):
    report = m002.migrate(old_db, backup_dir=tmp_path / "backups")

    assert "hit_count" not in _columns(old_db)
    con = sqlite3.connect(old_db)
    try:
        assert [r[0] for r in con.execute("SELECT id FROM monitor ORDER BY id")] == [1, 2]
        # The ledger is untouched, which is the whole point: it was always the
        # real answer and now it is the only one.
        assert (
            con.execute("SELECT count(*) FROM monitorhit WHERE monitor_id = 2").fetchone()[0] == 2
        )
    finally:
        con.close()
    assert "dropped monitor.hit_count" in report


def test_it_is_idempotent(old_db, tmp_path):
    m002.migrate(old_db, backup_dir=tmp_path / "backups")
    again = m002.migrate(old_db, backup_dir=tmp_path / "backups")
    assert "already migrated" in again


def test_it_backs_up_before_touching_anything(old_db, tmp_path):
    backups = tmp_path / "backups"
    m002.migrate(old_db, backup_dir=backups)
    copies = list(backups.glob("*.db"))
    assert len(copies) == 1

    # The backup still has the column and the stale numbers, so a rollback is
    # a real rollback and not a copy of the migrated file.
    con = sqlite3.connect(copies[0])
    try:
        assert "hit_count" in [row[1] for row in con.execute('PRAGMA table_info("monitor")')]
        assert con.execute("SELECT hit_count FROM monitor WHERE id = 2").fetchone()[0] == 0
    finally:
        con.close()


def test_it_refuses_a_missing_database(tmp_path):
    with pytest.raises(m002.Refused, match="no database"):
        m002.migrate(tmp_path / "nope.db", backup_dir=tmp_path / "backups")


def test_it_refuses_while_another_connection_holds_the_write_lock(old_db, tmp_path):
    """A scheduler mid-write is the case that actually destroys data."""
    holder = sqlite3.connect(old_db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(m002.Refused, match="in use"):
            m002.migrate(old_db, backup_dir=tmp_path / "backups")
        # Refused BEFORE any change, so the column is still there.
        holder.execute("ROLLBACK")
    finally:
        holder.close()
    assert "hit_count" in _columns(old_db)


def test_it_refuses_a_database_with_no_monitor_table(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(m002.Refused, match="no `monitor` table"):
        m002.migrate(path, backup_dir=tmp_path / "backups")


def test_it_tolerates_orphans_it_did_not_create(old_db, tmp_path):
    """Pre-existing debt must not block a column drop.

    Deleting a monitor rule leaves its `collectrun` rows behind (PRAGMA
    foreign_keys is off on the app's pooled connections), so a real database
    can carry orphans for reasons that have nothing to do with this migration
    — 98 of them on the developer's own. 001 could demand a clean database
    because it rebuilt a table other rows point at; this one cannot create or
    repair an orphan, so it only refuses if it ADDS one.
    """
    con = sqlite3.connect(old_db, isolation_level=None)
    try:
        con.execute(
            "INSERT INTO monitorhit (monitor_id, item_id, first_hit_at) "
            "VALUES (99, 'ghost', '2026-09-12 00:00:00.000000')"
        )
        assert len(list(con.execute("PRAGMA foreign_key_check"))) == 1
    finally:
        con.close()

    report = m002.migrate(old_db, backup_dir=tmp_path / "backups")

    assert "dropped monitor.hit_count" in report
    assert "hit_count" not in _columns(old_db)
    # Still there, untouched: the migration is not a cleanup tool either.
    con = sqlite3.connect(old_db)
    try:
        assert len(list(con.execute("PRAGMA foreign_key_check"))) == 1
    finally:
        con.close()
