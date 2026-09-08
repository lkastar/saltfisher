"""Numbered, one-shot schema migrations.

`db.add_missing_columns()` handles every ADDITIVE nullable column on startup,
and that covers almost everything this project does to its schema. A script
lands here only when the change is outside that ceiling, which its own
docstring names: a rename, a drop, a type change, a NOT NULL column, or a
constraint. On SQLite those all mean "build a new table, copy, swap", and
guessing at them from the models is how data gets destroyed.

The convention, set by 001 and not optional for the next one:

  * **Filename** `NNN_short_slug.py`, numbers never reused. The digits mean the
    file is not importable by name; tests load it with `importlib`, which is
    fine and keeps the CLI the only real entry point.
  * **Idempotent**, judged by reading the ACTUAL schema (`PRAGMA table_info`,
    `sqlite_master.sql`). There is no version table in this project and
    introducing one for a handful of scripts would be a second source of truth
    about a question the database itself can answer.
  * **Back up first**, through `scripts/backup.py` — never `cp`, which in WAL
    mode copies a file that can read zero rows. A failed backup ABORTS.
  * **Refuse to run against a live database.** Rebuilding a table under a
    writing scheduler is how the WAL and the swapped table disagree.
  * **Verify before COMMIT**, inside the transaction: row count, every column
    of every row, and `PRAGMA foreign_key_check`. A migration that cannot
    prove it kept the data is a migration that has to roll itself back.
  * **Never edit a shipped script.** Write the next number.
"""
