"""001 — a monitor rule may target a seller instead of a keyword.

    uv run python scripts/migrations/001_monitor_seller_rules.py --i-stopped-the-app
    uv run python scripts/migrations/001_monitor_seller_rules.py \
        --db /tmp/copy.db --i-stopped-the-app

Two changes, and NEITHER is inside `db.add_missing_columns()`'s ceiling:

  1. `monitor.keyword` becomes nullable. SQLite cannot `ALTER TABLE` a NOT NULL
     constraint away.
  2. `monitor` gains the `rule_target_xor` CHECK, so exactly one of
     `keyword` / `seller_id` is set. SQLite has no `ADD CONSTRAINT`.

So this is the standard table rebuild, in the order SQLite's own docs require
and this project's `scripts/migrations/__init__.py` restates. `seller_id`
itself is additive and nullable, so **startup has very likely already added
it** — which is exactly why "have we migrated?" cannot be `seller_id in
columns`. It is all three facts or nothing.

Idempotent: a second run prints that it has already been done and exits 0.

What this does NOT do, deliberately: nothing is backfilled. Every pre-existing
rule is a keyword rule, so `seller_id IS NULL` is already the right answer for
all of them, and `Seller.numeric_id` is additive so startup adds it.
"""

import argparse
import contextlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import settings  # noqa: E402
from scripts.backup import backup  # noqa: E402

# The target schema, frozen. Deliberately hand-written rather than compiled
# from `app.models`: a shipped migration must keep meaning what it meant the
# day it ran, and generating it from the live models would silently re-aim
# this script at whatever the schema becomes in P6.
#
# The ONE substitution is the interval floor, because it is not a constant:
# `SFD_MIN_INTERVAL_SECONDS` feeds `interval_floor` on the model, so writing
# the literal 60 here would silently LOWER a floor a deployment deliberately
# raised -- an anti-ban setting, quietly relaxed by a migration about
# something else. Taking it from settings makes the rebuilt table agree with
# what `create_all()` would produce on this same machine, which is the
# invariant that actually matters. (A raised floor with rows below it makes
# the copy fail and roll back, loudly, which is the correct outcome.)
CREATE_NEW = """
CREATE TABLE monitor_new (
    id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    keyword VARCHAR,
    seller_id VARCHAR,
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
    CONSTRAINT interval_floor CHECK (interval_seconds >= {floor}),
    CONSTRAINT rule_target_xor CHECK ((keyword IS NOT NULL) <> (seller_id IS NOT NULL)),
    FOREIGN KEY(seller_id) REFERENCES seller (id)
)
"""

# Index the model declares on the new column. Any index the OLD table carried
# is captured from sqlite_master at runtime and replayed, because DROP TABLE
# takes a table's indexes with it.
CREATE_INDEX = "CREATE INDEX ix_monitor_seller_id ON monitor (seller_id)"

NEW_COLUMNS = (
    "id",
    "name",
    "keyword",
    "seller_id",
    "exclude_words",
    "price_min_cents",
    "price_max_cents",
    "published_within_hours",
    "region",
    "condition",
    "free_shipping",
    "min_seller_credit",
    "exclude_shop",
    "interval_seconds",
    "enabled",
    "baseline_done",
    "last_run_at",
    "last_error",
    "last_collector",
    "consecutive_failures",
    "hit_count",
    "created_at",
)


class Refused(Exception):
    """The migration declined to run. Nothing was changed."""


def _table_info(con: sqlite3.Connection, table: str) -> list[tuple]:
    return list(con.execute(f'PRAGMA table_info("{table}")'))


def is_migrated(con: sqlite3.Connection) -> bool:
    """All three facts, read off the real schema.

    Any two of them is a HALF-migrated table and must still be rebuilt. That
    is not a hypothetical state: `seller_id` is nullable, so the ordinary
    startup path adds it on its own, and a check that only looked for the
    column would skip every database the app has ever booted against.
    """
    info = {row[1]: row for row in _table_info(con, "monitor")}
    if "seller_id" not in info or "keyword" not in info:
        return False
    keyword_is_nullable = info["keyword"][3] == 0
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='monitor'"
    ).fetchone()
    has_check = bool(row) and "rule_target_xor" in (row[0] or "")
    return keyword_is_nullable and has_check


def _refuse_if_busy(path: Path) -> None:
    """An exclusive lock, taken and released, before anything is touched.

    Not a complete liveness test and not pretended to be: the app's pooled
    connections can sit idle holding no lock at all, which is why
    `--i-stopped-the-app` is required on top of this. It does reliably catch
    the case that actually destroys data — a scheduler mid-write.
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


def _rows(con: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple]:
    names = ", ".join(f'"{c}"' for c in columns)
    return list(con.execute(f'SELECT {names} FROM "{table}" ORDER BY id'))


def migrate(path: Path, *, backup_dir: Path | None = None) -> str:
    """Rebuild `monitor`. Returns a one-line report of what happened.

    Raises `Refused` before touching anything if the database is missing, in
    use, or the backup fails. Any failure after that point rolls the whole
    transaction back, so the table is either the old one or the new one and
    never a mixture.
    """
    if not path.exists():
        raise Refused(f"no database at {path}")

    # Before the schema is even read: outside WAL mode an exclusive lock
    # blocks readers too, so asking anything first would surface as a bare
    # "database is locked" instead of an instruction to stop the app.
    _refuse_if_busy(path)

    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _table_info(probe, "monitor"):
            raise Refused(f"{path} has no monitor table; let the app create the schema first")
        if is_migrated(probe):
            return f"{path}: already migrated, nothing to do"
    finally:
        probe.close()

    # Through scripts/backup.py, which reads via SQLite's online backup API.
    # `cp` in WAL mode copies a file that can read zero rows in every table
    # (measured, T8), and a migration whose rollback plan is a lie is worse
    # than no backup at all. A failure here ABORTS -- best-effort is not a
    # thing this step is allowed to be.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (backup_dir or path.parent / "backups") / f"pre-001-{stamp}.db"
    try:
        counts = backup(path, target)
    except (OSError, sqlite3.Error) as exc:
        raise Refused(f"backup to {target} failed ({exc}); refusing to migrate") from exc
    print(f"backed up to {target}  {counts}")

    con = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        old_columns = [row[1] for row in _table_info(con, "monitor")]
        carried = [c for c in NEW_COLUMNS if c in old_columns]
        dropped = [c for c in old_columns if c not in NEW_COLUMNS]
        # Every index the old table has, so the DROP does not take one away
        # for good. There are none on a stock monitor table; a hand-added one
        # on a real deployment is not worth losing to that assumption.
        old_indexes = [
            row[0]
            for row in con.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='monitor' "
                "AND sql IS NOT NULL"
            )
        ]
        before = _rows(con, "monitor", carried)

        # Order is not negotiable and PRAGMA comes FIRST: `foreign_keys` is a
        # no-op inside a transaction, and with it left on, DROP TABLE monitor
        # would either fail or silently rewrite monitorhit / monitorchannel /
        # collectrun rows that point at it.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN EXCLUSIVE")
        con.execute(CREATE_NEW.format(floor=int(settings.min_interval_seconds)))
        names = ", ".join(f'"{c}"' for c in carried)
        con.execute(f"INSERT INTO monitor_new ({names}) SELECT {names} FROM monitor")
        con.execute("DROP TABLE monitor")
        con.execute("ALTER TABLE monitor_new RENAME TO monitor")
        for ddl in old_indexes:
            con.execute(ddl)
        con.execute(CREATE_INDEX)

        after = _rows(con, "monitor", carried)
        if after != before:
            raise Refused(
                f"copy does not match the original ({len(before)} rows in, {len(after)} out); "
                "rolled back"
            )
        # Database-WIDE on purpose, not just monitor's dependents. If the
        # file was already inconsistent, a migration that commits over it
        # gets the blame for the damage, and this is the last moment a
        # ROLLBACK is still free.
        broken = list(con.execute("PRAGMA foreign_key_check"))
        if broken:
            tables = sorted({row[0] for row in broken})
            raise Refused(
                f"foreign_key_check reported {len(broken)} orphaned row(s) in {tables}; "
                "rolled back, nothing was changed. Fix those rows first."
            )
        con.execute("COMMIT")
    except BaseException:
        # Suppressed because the reads above happen before BEGIN: a failure
        # there has no transaction to roll back, and letting "no transaction
        # is active" replace the real exception hides the actual cause.
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
        con.close()

    note = f", left behind unknown column(s) {dropped}" if dropped else ""
    return f"{path}: rebuilt monitor, {len(before)} rows carried on {len(carried)} columns{note}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="defaults to settings.db_path")
    parser.add_argument(
        "--i-stopped-the-app",
        action="store_true",
        help="required: rebuilding monitor under a running scheduler corrupts it",
    )
    args = parser.parse_args()
    path = args.db or settings.db_path

    if not args.i_stopped_the_app:
        print(
            f"refusing to rebuild the monitor table in {path} without --i-stopped-the-app.\n"
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
