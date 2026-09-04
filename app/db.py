"""Engine and session handling.

Sync sessions on purpose (see .trellis/spec/backend/database-guidelines.md):
this is a single-user SQLite app, and async drivers buy nothing here. Async
callers wrap DB work in asyncio.to_thread.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # WAL + busy_timeout are not optional: the scheduler writes while the API
    # reads, and the default journal mode turns that into "database is locked".
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    import app.models  # noqa: F401  (register tables before create_all)

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


# The Annotated form is FastAPI's current idiom and keeps `Depends()` out of
# argument defaults, which is both a lint rule and a real footgun (a default is
# evaluated once at import).
SessionDep = Annotated[Session, Depends(get_session)]
