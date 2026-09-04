"""Backing up a WAL database.

The drill that produced these tests: `cp data/app.db` was the documented
backup, and in T8 every copy taken that way read ZERO rows in every table --
a 114KB main file beside a 3.3MB WAL that had never been checkpointed.
Restoring one would have looked like a successful restore of an empty
database.
"""

import sqlite3

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Monitor
from scripts.backup import backup, row_counts


@pytest.fixture
def wal_db(tmp_path):
    """A database with rows that live only in the WAL, like production."""
    path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for i in range(5):
            s.add(Monitor(name=f"规则{i}", keyword="iPhone", interval_seconds=300))
        s.commit()
    # Deliberately NOT disposed: connections stay open, so nothing is
    # checkpointed -- exactly the state a running app leaves the file in.
    return path, engine


def test_a_plain_file_copy_loses_everything(wal_db):
    """The behaviour that made the documented procedure unsafe. If this ever
    fails, SQLite changed and the rest of this file needs rereading.

    Note how complete the loss is: `row_counts` reports -1, its sentinel for
    "the query failed", because the copy has no `monitor` TABLE at all. Even
    the schema was still sitting in the WAL.
    """
    path, _engine = wal_db
    copied = path.parent / "copied.db"
    copied.write_bytes(path.read_bytes())  # the `cp data/app.db` of the docs

    assert row_counts(path)["monitor"] == 5
    assert row_counts(copied)["monitor"] <= 0, "a file copy suddenly works; recheck the docs"


def test_the_backup_api_captures_the_wal(wal_db):
    path, _engine = wal_db
    target = path.parent / "backup.db"

    counts = backup(path, target)

    assert counts["monitor"] == 5
    with sqlite3.connect(target) as con:
        names = [r[0] for r in con.execute("select name from monitor order by name")]
    assert names == [f"规则{i}" for i in range(5)]


def test_a_backup_works_while_the_source_is_being_written(wal_db):
    """No downtime: the whole point of using the backup API over a copy."""
    path, engine = wal_db
    with Session(engine) as s:
        s.add(Monitor(name="并发写入", keyword="x", interval_seconds=300))
        s.commit()
        target = path.parent / "hot.db"
        counts = backup(path, target)

    assert counts["monitor"] == 6


def test_a_checkpoint_makes_a_file_copy_safe_again(wal_db):
    """What `db.checkpoint()` buys on shutdown: after it, the main file is
    self-contained and copying a STOPPED instance is a real backup.
    """
    path, engine = wal_db
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()

    copied = path.parent / "after-checkpoint.db"
    copied.write_bytes(path.read_bytes())
    assert row_counts(copied)["monitor"] == 5
