"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api import monitors
from app.auth import require_token
from app.collector.browser import BrowserCollector
from app.collector.mtop import MtopClient
from app.collector.pipeline import Pipeline
from app.collector.session import UpstreamSession
from app.config import settings
from app.db import init_db
from app.scheduler import search_loop, watch_loop

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

    # One session, one HTTP client, one browser for the whole process, all
    # owned here rather than as module-level globals. The browser is what
    # establishes the upstream session; the cheap mtop path reuses its cookies.
    session = UpstreamSession()
    mtop = MtopClient(session)
    browser = BrowserCollector(session)
    await browser.start()
    app.state.session = session
    app.state.pipeline = Pipeline(mtop, browser, session)
    log.info("collector ready")

    stop = asyncio.Event()
    loops = [
        asyncio.create_task(search_loop(app.state.pipeline, stop), name="search_loop"),
        asyncio.create_task(watch_loop(app.state.pipeline, stop), name="watch_loop"),
    ]
    try:
        yield
    finally:
        # Signal, then await. Cancelling would risk interrupting a write.
        stop.set()
        await asyncio.gather(*loops, return_exceptions=True)
        await browser.stop()
        await mtop.aclose()


app = FastAPI(title="saltfish-digger", lifespan=lifespan)
app.include_router(monitors.router, dependencies=[Depends(require_token)])


@app.get("/api/health")
def health() -> dict[str, str]:
    """Unauthenticated on purpose — it must work as a container healthcheck."""
    return {"status": "ok"}


@app.get("/api/whoami", dependencies=[Depends(require_token)])
def whoami() -> dict[str, bool]:
    """Cheapest possible way for the frontend to validate a stored token."""
    return {"authenticated": True}


@app.get("/api/session", dependencies=[Depends(require_token)])
def session_state() -> dict[str, object]:
    """Upstream session health.

    `needs_verification` is the difference between "wait" and "a human must
    act" — the UI has to be able to say which, or a challenged session looks
    identical to a quiet market.
    """
    session: UpstreamSession = app.state.session
    return {
        "origin": session.origin,
        "usable": session.usable,
        "needs_verification": session.needs_verification,
        "established_at": session.established_at.isoformat() if session.established_at else None,
        "last_error": session.last_error,
    }
