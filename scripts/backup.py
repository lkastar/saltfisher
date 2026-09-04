"""Back up the database correctly, including while the app is running.

`cp data/app.db` is NOT a backup. The app runs SQLite in WAL mode, so recent
writes live in `app.db-wal` until a checkpoint folds them in — and SQLite only
auto-checkpoints once the WAL passes about 4MB. Measured in T8: a 114KB main
file beside a 3.3MB WAL, and every copy of `app.db` read **zero rows in every
table**. Restoring one of those would have looked like a successful restore of
an empty database.

This uses SQLite's own online backup API, which is consistent while the app
writes and needs no downtime.

    uv run python scripts/backup.py                    # data/backups/app-<ts>.db
    uv run python scripts/backup.py --out /mnt/x.db
    uv run python scripts/backup.py --verify-only PATH # count rows in a backup
"""

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TABLES = (
    "monitor",
    "item",
    "seller",
    "pricesnapshot",
    "monitorhit",
    "watchlist",
    "notifychannel",
    "notifylog",
)


def row_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in TABLES:
            try:
                counts[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1
    finally:
        con.close()
    return counts


def backup(source: Path, target: Path) -> dict[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        # The backup API reads through the WAL, which a file copy cannot.
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return row_counts(target)


def main() -> int:
    from app.config import settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--verify-only", type=Path, default=None)
    args = parser.parse_args()

    if args.verify_only is not None:
        counts = row_counts(args.verify_only)
        print(f"{args.verify_only}: {counts}")
        return 0 if any(v > 0 for v in counts.values()) else 1

    source = settings.db_path
    if not source.exists():
        print(f"no database at {source}", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = args.out or source.parent / "backups" / f"app-{stamp}.db"

    before = row_counts(source)
    after = backup(source, target)
    print(f"source {source}  {before}")
    print(f"backup {target}  {after}")

    if before != after:
        print("MISMATCH: the backup does not agree with the source", file=sys.stderr)
        return 1
    if not any(v > 0 for v in after.values()):
        # An empty backup of an empty database is fine; an empty backup of a
        # live one is the exact failure this script exists to prevent.
        print("note: the database is empty", file=sys.stderr)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
