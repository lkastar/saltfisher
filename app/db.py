"""Engine and session handling.

Sync sessions on purpose (see .trellis/spec/backend/database-guidelines.md):
this is a single-user SQLite app, and async drivers buy nothing here. Async
callers wrap DB work in asyncio.to_thread.
"""

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

log = logging.getLogger(__name__)

settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def add_missing_columns(target: Engine = engine) -> list[str]:
    """Add nullable columns the models declare but the database lacks.

    `create_all()` creates missing TABLES and nothing else — it never alters a
    table that already exists. So adding a field to a model left every
    already-deployed database without that column, and the first endpoint to
    select it returned a 500. That is not hypothetical: `Seller.review_count`
    and `Seller.positive_rate` shipped in T2/T5 and broke `/api/watchlist` on
    any database created before them, invisibly, because tests build their
    schema fresh from the current models and can never reproduce the drift.

    Returns the DDL it ran, so startup can log it and a test can assert it.

    ponytail: additive nullable columns only. A rename, a drop, a type change,
    or a NOT NULL column still needs a numbered script under
    scripts/migrations/ — those cannot be inferred from the models, and
    guessing at them is how data gets destroyed.
    """
    import app.models  # noqa: F401  (register tables before inspecting them)

    applied: list[str] = []
    with target.begin() as conn:
        for name, table in SQLModel.metadata.tables.items():
            rows = conn.exec_driver_sql(f'PRAGMA table_info("{name}")').fetchall()
            if not rows:
                continue  # create_all() just made it, or is about to
            present = {row[1] for row in rows}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    log.error(
                        "column missing and cannot be added automatically; "
                        "write a script under scripts/migrations/",
                        extra={"table": name, "column": column.name},
                    )
                    continue
                sql_type = column.type.compile(dialect=target.dialect)
                ddl = f'ALTER TABLE "{name}" ADD COLUMN "{column.name}" {sql_type}'
                conn.exec_driver_sql(ddl)
                applied.append(ddl)
                log.warning("added missing column", extra={"table": name, "column": column.name})
    return applied


def init_db() -> None:
    # WAL + busy_timeout are not optional: the scheduler writes while the API
    # reads, and the default journal mode turns that into "database is locked".
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    import app.models  # noqa: F401  (register tables before create_all)

    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)


def checkpoint() -> None:
    """Fold the WAL into the main database file and release the connections.

    Without this, every row lives only in `app.db-wal` until SQLite's
    auto-checkpoint fires at ~4MB, and `cp data/app.db` copies a file that can
    be entirely empty. Measured in T8: a 114KB main file beside a 3.3MB WAL,
    and all three copies of app.db read zero rows in every table.

    Called on shutdown so a stopped container leaves a self-contained file.
    Backups taken while running must still go through `scripts/backup.py`.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    log.info("wal checkpointed")


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


# The Annotated form is FastAPI's current idiom and keeps `Depends()` out of
# argument defaults, which is both a lint rule and a real footgun (a default is
# evaluated once at import).
SessionDep = Annotated[Session, Depends(get_session)]
