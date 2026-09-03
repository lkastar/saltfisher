"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import require_token
from app.config import settings
from app.db import init_db

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    log.info("db ready", extra={"path": str(settings.db_path)})
    # Collector clients and the two polling loops are started here from T2/T3.
    yield


app = FastAPI(title="saltfish-digger", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Unauthenticated on purpose — it must work as a container healthcheck."""
    return {"status": "ok"}


@app.get("/api/whoami", dependencies=[Depends(require_token)])
def whoami() -> dict[str, bool]:
    """Cheapest possible way for the frontend to validate a stored token."""
    return {"authenticated": True}
