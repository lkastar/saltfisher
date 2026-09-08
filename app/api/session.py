"""Upstream session: health, credential import, credential removal.

The import endpoint is what makes deployment possible at all. The collector can
refresh a rotating `_m_h5_tk` on its own, but the long-lived login state has to
come from a human who solved a slider in their own browser — there is no way to
do that inside a headless container, so the panel has to accept a paste.

Two ways in, on purpose. The devtools paste is the complete one and the
fallback. The bookmarklet is the short one: it runs on a goofish page and posts
`document.cookie`, which measurement (`research/cookie-domains.md`) showed is
enough — 43 of the 44 cookies are readable, and the one signing token that is
not (`_m_h5_tk`, `.taobao.com`-only) is the one the collector re-mints itself.

Because that script executes in a page Alibaba controls, it must not carry
`SFD_API_TOKEN`. It carries a one-time import ticket instead: worst case it is
stolen off the page and buys exactly one cookie import and nothing else.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.auth import require_token
from app.collector.browser import BrowserCollector
from app.collector.fingerprint import Fingerprint
from app.collector.session import UpstreamSession
from app.scheduler import resume_challenge_disabled
from app.schemas import CookieImport, ImportTicket, SessionState

log = logging.getLogger(__name__)
# No router-level auth dependency: `POST /cookies` is the one route in the app
# that accepts something other than the bearer token, so each route says what
# it needs. `test_import_ticket.py` asserts every route here still needs
# something, so adding one without a dependency fails a test rather than
# quietly publishing it.
router = APIRouter(prefix="/api/session", tags=["session"])

# Names that carry the login itself, as opposed to the short-lived token the
# collector rotates on its own. Used only to tell "you pasted the cookie header
# from the wrong tab" apart from a real session -- upstream is free to change
# this set, so it gates nothing on its own.
LOGIN_COOKIE_NAMES = ("cookie2", "unb", "_tb_token_", "sgcookie")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse what devtools' "copy request headers" produces.

    Splits each pair on the FIRST `=` only: cookie values are base64-ish and
    routinely contain `=` padding, and splitting on all of them truncates the
    value into something that looks imported but authenticates as nobody.
    """
    cookies: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            cookies[name] = value
    return cookies


def _state(request: Request) -> SessionState:
    session: UpstreamSession = request.app.state.session
    fingerprint: Fingerprint = request.app.state.fingerprint
    return SessionState(
        origin=session.origin,
        usable=session.usable,
        needs_verification=session.needs_verification,
        established_at=session.established_at,
        last_error=session.last_error,
        challenged_apis=sorted(session.challenged_apis),
        proven=session.proven,
        last_success_at=session.last_success_at,
        # Names only. A count and a name list are enough to answer "did the
        # paste work"; the values must never leave the process.
        cookie_names=sorted(session.cookies),
        # Same rule one field down: a summary line, never the snapshot. The
        # panel says "Google Chrome 141 · Windows · 1920×1080 · Asia/Shanghai";
        # `hardware_concurrency` and friends stay in the process.
        fingerprint=fingerprint.summary(),
        fingerprint_applied=fingerprint.applied,
    )


# --------------------------------------------------------------------------- #
# One-time import ticket
# --------------------------------------------------------------------------- #

IMPORT_PATH = "/api/session/cookies"
TICKET_HEADER = "x-sfd-import-ticket"

# Long enough for a human to switch tabs, log in, fight a slider and refresh;
# short enough that a bookmarklet left in the bookmarks bar is dead by the time
# anyone else could find it. Ten minutes is the round number that covers the
# slow case observed while writing the docs (login + slider + reload) with room
# to spare. Minting another is one click, so erring short costs nothing.
TICKET_TTL_SECONDS = 600
# Expired tickets are kept a while after they die so an expired one can be told
# apart from an invented or already-spent one -- "which of the two" is the whole
# difference between "click the button again" and "you are being phished".
TICKET_GRACE_SECONDS = 3600

# The bookmarklet posts from the goofish page the user is logged into. Taken
# from the domains actually present in a captured `data/state.json`
# (`.goofish.com`, `passport.goofish.com`), not from guesswork. Nothing else in
# the app is cross-origin, which is why this is a route-scoped allowlist rather
# than CORSMiddleware over the whole app.
ALLOWED_IMPORT_ORIGINS = frozenset(
    {
        "https://www.goofish.com",
        "https://goofish.com",
        "https://passport.goofish.com",
    }
)


def _tickets(request: Request) -> dict[str, float]:
    """The live ticket store: ticket -> monotonic deadline.

    On `app.state` and never in the database. These are credential-adjacent
    secrets with a ten-minute life; persisting them would let one outlive the
    process that minted it, and a restart is a perfectly good way to invalidate
    every outstanding ticket at once.
    """
    store: dict[str, float] | None = getattr(request.app.state, "import_tickets", None)
    if store is None:
        store = request.app.state.import_tickets = {}
    return store


def _consume_ticket(store: dict[str, float], candidate: str) -> str | None:
    """Spend a ticket. Returns None when it was valid, else a reason.

    Consumed on any outcome, valid or not: a ticket that survived a failed
    import would be a ticket an attacker can retry.
    """
    now = time.monotonic()
    for ticket, deadline in list(store.items()):
        # compare_digest rather than `==`/`in`: the dict lookup would be the
        # fast path an attacker times against.
        if secrets.compare_digest(ticket, candidate):
            del store[ticket]
            return None if deadline > now else "expired"
    return "unknown"


def _authorise_import(request: Request, ticket: str) -> None:
    """Bearer token, or one ticket spent. Raises 401 with which one failed."""
    if not ticket:
        # Same check the other routes get through Depends(); this route just
        # has to run it by hand because it has a second door.
        require_token(request.headers.get("authorization", ""))
        return
    reason = _consume_ticket(_tickets(request), ticket)
    if reason is None:
        return
    log.warning("import ticket refused", extra={"reason": reason})
    raise HTTPException(
        status_code=401,
        detail=(
            "导入票据已过期（有效期 10 分钟），请回设置页重新生成书签"
            if reason == "expired"
            else "导入票据无效或已经用过了（每张只能用一次），请回设置页重新生成书签"
        ),
    )


@router.post("/import-ticket", response_model=ImportTicket, dependencies=[Depends(require_token)])
def mint_import_ticket(request: Request) -> ImportTicket:
    """Mint a single-use authorisation for exactly one cookie import.

    It authorises nothing else: no other route reads the ticket header, so a
    stolen ticket cannot read the panel, the items, or the LLM keys.
    """
    store = _tickets(request)
    now = time.monotonic()
    # Bounded without a sweeper task: drop the ones too old to still need a
    # distinguishing "expired" message.
    for old, deadline in list(store.items()):
        if deadline + TICKET_GRACE_SECONDS < now:
            del store[old]

    ticket = secrets.token_urlsafe(32)
    store[ticket] = now + TICKET_TTL_SECONDS
    # Count, never the value -- it goes into a bookmarklet on a third-party
    # page and a log file is one more copy of it.
    log.info("import ticket minted", extra={"outstanding": len(store)})
    return ImportTicket(
        expires_at=datetime.now(UTC) + timedelta(seconds=TICKET_TTL_SECONDS), ticket=ticket
    )


async def import_cors(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """CORS for the bookmarklet's one route, and nothing else.

    Deliberately not `CORSMiddleware`: that is configured per-app, and no other
    route in this panel has any business answering a cross-origin caller.

    Middleware rather than a header set inside the handler because the answers
    that matter most are the failures -- an expired ticket must reach the
    overlay as its own message, and a 401 without the header is unreadable to
    the calling script and shows up as a bare "network error".

    A disallowed origin gets no CORS headers at all, which is how CORS refuses:
    the request may still execute (curl was never bound by CORS anyway), but no
    browser will hand the response to the page that asked for it.
    """
    origin = request.headers.get("origin", "")
    if request.url.path != IMPORT_PATH or origin not in ALLOWED_IMPORT_ORIGINS:
        return await call_next(request)
    # The preflight has no route of its own; answering it here keeps the
    # allowlist in one place.
    response = (
        Response(status_code=200) if request.method == "OPTIONS" else await call_next(request)
    )
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = f"content-type, {TICKET_HEADER}"
    response.headers["Access-Control-Max-Age"] = "600"
    # The panel is typically on a private address and goofish is public, which
    # is a Private Network Access request: Chrome preflights it with
    # `Access-Control-Request-Private-Network` and drops the call unless the
    # answer grants it. Sent unconditionally because it is only ever read when
    # the browser asked, and the allowlist above has already decided who is
    # allowed to be here.
    #
    # It does NOT cover mixed content: an https goofish page cannot fetch a
    # plain-http panel at all, except on localhost/127.0.0.1, which browsers
    # treat as trustworthy. A LAN panel on http therefore needs the devtools
    # flow (docs/operations.md).
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@router.get("", response_model=SessionState, dependencies=[Depends(require_token)])
def read_session(request: Request) -> SessionState:
    """`needs_verification` is the difference between "wait" and "a human must
    act" — the UI has to be able to say which, or a challenged session looks
    identical to a quiet market.
    """
    return _state(request)


@router.post("/cookies", response_model=SessionState)
async def import_cookies(
    payload: CookieImport,
    request: Request,
    x_sfd_import_ticket: str = Header(default="", alias=TICKET_HEADER),
) -> SessionState:
    """Accepts the bearer token, or a single-use import ticket instead.

    The ticket is spent BEFORE the payload is looked at: it authorises one
    attempt, not one success.
    """
    _authorise_import(request, x_sfd_import_ticket)
    cookies = parse_cookie_header(payload.cookie_header)
    if not cookies:
        raise HTTPException(
            status_code=400,
            detail="没有解析出任何 cookie，请粘贴完整的 Cookie 请求头",
        )
    if not any(name in cookies for name in LOGIN_COOKIE_NAMES):
        raise HTTPException(
            status_code=400,
            detail=(
                "解析到 cookie 但没有登录态字段"
                f"（期望其中之一：{'、'.join(LOGIN_COOKIE_NAMES)}），"
                "请确认是在已登录的闲鱼页面复制的"
            ),
        )

    session: UpstreamSession = request.app.state.session
    browser: BrowserCollector = request.app.state.browser
    await browser.import_cookies(cookies)
    if payload.env is not None:
        # `exclude_none` is what keeps "the browser has no `deviceMemory`"
        # distinct from "the browser reports no memory": an absent field stays
        # absent all the way to disk rather than becoming a null the collector
        # has to second-guess.
        snapshot = payload.env.model_dump(exclude_none=True)
        if snapshot:
            request.app.state.fingerprint.adopt(snapshot)
    # adopt() already clears the challenge flags and re-arms the alert. What it
    # cannot reach is the DATABASE side of the damage: the rules the challenge
    # switched off, which the user otherwise has to re-enable by hand one by
    # one. Only the slider itself needs a human.
    session.adopt(cookies, payload.origin)
    resumed = await asyncio.to_thread(resume_challenge_disabled)
    # Not "recovered" — nothing here has talked to the upstream. The state is
    # cleared and the next sweep will produce the actual evidence.
    log.info("cookies imported", extra={"resumed_monitors": len(resumed)})
    return _state(request)


@router.delete("/cookies", response_model=SessionState, dependencies=[Depends(require_token)])
async def clear_cookies(request: Request) -> SessionState:
    session: UpstreamSession = request.app.state.session
    browser: BrowserCollector = request.app.state.browser
    await browser.clear_cookies()
    session.cookies.clear()
    session.challenged_apis.clear()
    session.challenge_announced = False
    session.established_at = None
    session.last_error = "credentials cleared"
    log.info("session cleared by request")
    return _state(request)
