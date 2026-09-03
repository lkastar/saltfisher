"""Test-wide setup.

Settings are validated at import time (app.config fails fast when
SFD_API_TOKEN is missing), so the env var must exist before any `app.*`
import. conftest.py is imported by pytest before test modules, which is the
one place that can happen without an os.environ line above every import.
"""

import os

os.environ.setdefault("SFD_API_TOKEN", "testtoken123")
os.environ.setdefault("SFD_DATA_DIR", "data/test")

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory DB. Real SQLite, not a mock — faster than the mock and it
    actually exercises the schema, constraints included.
    """
    import app.models  # noqa: F401  (register tables)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
