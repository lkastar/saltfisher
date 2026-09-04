"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import analytics as analytics_api
from app.api import channels, items, monitors, watchlist
from app.api import llm as llm_api
from app.api import session as session_api
from app.auth import require_token
from app.collector.browser import BrowserCollector
from app.collector.mtop import MtopClient
from app.collector.pipeline import Pipeline
from app.collector.session import UpstreamSession
from app.config import settings
from app.db import checkpoint, init_db
from app.notify import build_registry
from app.scheduler import search_loop, watch_loop

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Transport libraries log full request and response HEADERS at DEBUG, which
# for this app means dumping live session cookies into the log file. Found in
# T8: at SFD_LOG_LEVEL=DEBUG, httpcore printed
# `Set-Cookie: _m_h5_tk=<live token>` verbatim. Silencing httpx alone is not
# enough -- it is httpcore underneath that logs the headers.
for _noisy in ("httpx", "httpcore", "hpack", "h11"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
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
    # A separate client from the collector's: different host, different
    # headers, and a goofish-shaped user-agent has no business on api.telegram.
    notify_client = httpx.AsyncClient(timeout=20.0)
    app.state.notify_client = notify_client
    app.state.notify_registry = build_registry(notify_client)
    mtop = MtopClient(session)
    browser = BrowserCollector(session)
    await browser.start()
    app.state.session = session
    app.state.browser = browser
    app.state.pipeline = Pipeline(mtop, browser, session)
    log.info("collector ready")

    stop = asyncio.Event()
    registry = app.state.notify_registry
    loops = [
        asyncio.create_task(search_loop(app.state.pipeline, stop, registry), name="search_loop"),
        asyncio.create_task(watch_loop(app.state.pipeline, stop, registry), name="watch_loop"),
    ]
    try:
        yield
    finally:
        # Signal, then await. Cancelling would risk interrupting a write.
        stop.set()
        await asyncio.gather(*loops, return_exceptions=True)
        await browser.stop()
        await mtop.aclose()
        await notify_client.aclose()
        # Last, after every writer is done: leaves app.db self-contained so a
        # file copy of a stopped instance is actually a backup.
        checkpoint()


app = FastAPI(title="saltfish-digger", lifespan=lifespan)
app.include_router(monitors.router, dependencies=[Depends(require_token)])
app.include_router(channels.router, dependencies=[Depends(require_token)])
app.include_router(watchlist.router, dependencies=[Depends(require_token)])
app.include_router(items.router, dependencies=[Depends(require_token)])
app.include_router(session_api.router, dependencies=[Depends(require_token)])
app.include_router(analytics_api.router, dependencies=[Depends(require_token)])
app.include_router(llm_api.router, dependencies=[Depends(require_token)])


@app.get("/api/health")
def health() -> dict[str, str]:
    """Unauthenticated on purpose — it must work as a container healthcheck."""
    return {"status": "ok"}


@app.get("/api/whoami", dependencies=[Depends(require_token)])
def whoami() -> dict[str, bool]:
    """Cheapest possible way for the frontend to validate a stored token."""
    return {"authenticated": True}


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

# Registered LAST, after every API route: FastAPI matches in registration
# order, so a catch-all declared earlier would swallow the whole API.
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def mount_frontend(application: FastAPI, dist: Path = WEB_DIST) -> bool:
    """Serve `npm run build` output from the API process — no nginx.

    Returns whether anything was mounted, which is what the startup log and
    the tests care about.
    """
    index = dist / "index.html"
    if not index.is_file():
        # Backend-only development and CI have no build output. Refusing to
        # start here would turn "frontend not built" into "app is broken".
        log.info("frontend not built; serving api only", extra={"dist": str(dist)})
        return False

    assets = dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{spa_path:path}", include_in_schema=False)
    def serve_spa(spa_path: str) -> FileResponse:
        """Always index.html, never a path built from the request.

        That is what a client-side router needs (reloading /items/123 must
        still return the app), and it makes path traversal impossible by
        construction rather than by sanitising a string.

        ponytail: root-level static files other than index.html are not
        served. Nothing needs them yet -- the favicon is a data URI. Add a
        second mount if that changes.
        """
        if spa_path == "api" or spa_path.startswith("api/"):
            # An unmatched /api path must stay JSON. Handing back HTML makes
            # the frontend's res.json() throw and disguises a wrong URL as a
            # broken backend.
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(index)

    log.info("frontend mounted", extra={"dist": str(dist)})
    return True


mount_frontend(app)
