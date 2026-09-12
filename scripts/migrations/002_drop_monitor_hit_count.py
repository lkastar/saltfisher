"""002 — drop `monitor.hit_count`; the ledger is the count.

    uv run python scripts/migrations/002_drop_monitor_hit_count.py --i-stopped-the-app
    uv run python scripts/migrations/002_drop_monitor_hit_count.py \
        --db /tmp/copy.db --i-stopped-the-app

`hit_count` was a stored copy of `count(MonitorHit WHERE monitor_id = ?)`, and
a second truth about a question the ledger already answers can only drift. It
did, in both directions at once:

  * `scheduler._sync_persist_cycle` incremented it by `len(hits)`, where
    `hits` is `persist_cycle`'s NOTIFIABLE subset -- not the ledger rows it
    wrote. A rule's first cycle is the baseline cycle, which notifies nothing
    by design, so a brand-new rule showed **0** beside a link that listed the
    60 items it had just collected. Measured on the developer's own database:
    rule 2 had 60 ledger rows and `hit_count = 0`; rule 1 had 88 and 64.
  * Every later cycle undercounted by whatever was deduped or still inside the
    renotify cooldown, so the gap only ever widened.

`MonitorPublic.hit_count` survives unchanged -- the API field is the same, it
is just derived in `api/monitors._hit_count` now. The frontend needs no change.

A DROP COLUMN, not the table rebuild 001 had to do: `hit_count` carries no
constraint and appears in no index, which is exactly the case SQLite's native
`ALTER TABLE ... DROP COLUMN` (3.35+) handles. Rebuilding a table to delete a
plain integer column would be more code and more risk for the same result.

Idempotent: a second run prints that it has already been done and exits 0.
"""

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import settings  # noqa: E402
from scripts.backup import backup  # noqa: E402

# DROP COLUMN landed in SQLite 3.35.0 (2021-03). Below that this script has no
# safe path and says so rather than half-doing the job.
MIN_SQLITE = (3, 35, 0)


class Refused(Exception):
    """The migration declined to run. Nothing was changed."""


def is_migrated(con: sqlite3.Connection) -> bool:
    """Read off the real schema, like 001. There is no version table."""
    columns = [row[1] for row in con.execute('PRAGMA table_info("monitor")')]
    if not columns:
        raise Refused("there is no `monitor` table in this database")
    return "hit_count" not in columns


def _refuse_if_busy(path: Path) -> None:
    """Same exclusive-lock probe as 001, and the same caveat.

    It cannot see an idle pooled connection, which is why
    `--i-stopped-the-app` is required on top of it. It does catch the case
    that actually loses data: a scheduler mid-write.
    """
    con = sqlite3.connect(path, isolation_level=None, timeout=1.0)
    try:
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise Refused(
            f"{path} is in use by another connection ({exc}); stop the app first"
        ) from exc
    finally:
        con.close()


def migrate(path: Path, *, backup_dir: Path | None = None) -> str:
    """Drop the column. Returns a one-line report of what happened.

    Raises `Refused` before touching anything if the database is missing, in
    use, too old, or the backup fails.
    """
    if not path.exists():
        raise Refused(f"no database at {path}")
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise Refused(
            f"SQLite {sqlite3.sqlite_version} cannot DROP COLUMN; "
            f"{'.'.join(map(str, MIN_SQLITE))} or newer is required"
        )

    # Liveness BEFORE the schema read, not after: a scheduler mid-write holds
    # the lock, and reading PRAGMA through it raises a bare "database is
    # locked" that tells the user nothing about what to do. Cheapest and most
    # actionable check first.
    _refuse_if_busy(path)

    con = sqlite3.connect(path, isolation_level=None)
    try:
        if is_migrated(con):
            return f"already migrated: {path} has no monitor.hit_count"
    finally:
        con.close()

    # Through scripts/backup.py, never `cp`: in WAL mode a file copy can read
    # back zero rows (measured, T8). A failed backup ABORTS -- a migration
    # whose rollback plan is a lie is worse than no backup at all.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (backup_dir or path.parent / "backups") / f"pre-002-{stamp}.db"
    try:
        counts = backup(path, destination)
    except (OSError, sqlite3.Error) as exc:
        raise Refused(f"backup to {destination} failed ({exc}); nothing was changed") from exc
    print(f"backed up to {destination}  {counts}")

    con = sqlite3.connect(path, isolation_level=None)
    try:
        before = [row[0] for row in con.execute("SELECT id FROM monitor ORDER BY id")]
        # Counted BEFORE, and compared after, rather than requiring zero.
        # 001 could demand a clean database because it rebuilt a table other
        # rows point AT; this only drops a plain column and can neither create
        # nor repair an orphan. Measured on the developer's database: 98
        # `collectrun` rows still name a rule that was deleted (deleting a
        # rule does not clear its run log, and PRAGMA foreign_keys is off on
        # the pooled connections). Refusing on that would block a migration
        # over unrelated pre-existing debt; what must not happen is this
        # script ADDING to it.
        orphans_before = len(list(con.execute("PRAGMA foreign_key_check")))
        con.execute("BEGIN")
        con.execute('ALTER TABLE monitor DROP COLUMN "hit_count"')

        # Prove it inside the transaction, the way 001 does: every rule still
        # here, the column actually gone, and no orphan created on the way.
        after = [row[0] for row in con.execute("SELECT id FROM monitor ORDER BY id")]
        if after != before:
            raise Refused(
                f"rule ids changed ({len(before)} -> {len(after)}); rolled back, "
                "nothing was changed"
            )
        if not is_migrated(con):
            raise Refused("hit_count is still present after the DROP; rolled back")
        broken = list(con.execute("PRAGMA foreign_key_check"))
        if len(broken) > orphans_before:
            tables = sorted({row[0] for row in broken})
            raise Refused(
                f"foreign_key_check went from {orphans_before} to {len(broken)} orphaned "
                f"row(s) in {tables}; rolled back, nothing was changed."
            )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    return (
        f"dropped monitor.hit_count from {path} ({len(before)} rule(s) kept); backup {destination}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="defaults to settings.db_path")
    parser.add_argument(
        "--i-stopped-the-app",
        action="store_true",
        help="required: altering monitor under a running scheduler corrupts it",
    )
    args = parser.parse_args()
    path = args.db or settings.db_path

    if not args.i_stopped_the_app:
        print(
            f"refusing to alter the monitor table in {path} without --i-stopped-the-app.\n"
            "Stop the container (docker compose stop) and run it again.",
            file=sys.stderr,
        )
        return 1

    try:
        print(migrate(path))
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
