"""Upstream session: health, credential import, credential removal.

The import endpoint is what makes deployment possible at all. The collector can
refresh a rotating `_m_h5_tk` on its own, but the long-lived login state has to
come from a human who solved a slider in their own browser — there is no way to
do that inside a headless container, so the panel has to accept a paste.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app.collector.browser import BrowserCollector
from app.collector.session import UpstreamSession
from app.scheduler import resume_challenge_disabled
from app.schemas import CookieImport, SessionState

log = logging.getLogger(__name__)
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


def _state(session: UpstreamSession) -> SessionState:
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
    )


@router.get("", response_model=SessionState)
def read_session(request: Request) -> SessionState:
    """`needs_verification` is the difference between "wait" and "a human must
    act" — the UI has to be able to say which, or a challenged session looks
    identical to a quiet market.
    """
    return _state(request.app.state.session)


@router.post("/cookies", response_model=SessionState)
async def import_cookies(payload: CookieImport, request: Request) -> SessionState:
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
    # adopt() already clears the challenge flags and re-arms the alert. What it
    # cannot reach is the DATABASE side of the damage: the rules the challenge
    # switched off, which the user otherwise has to re-enable by hand one by
    # one. Only the slider itself needs a human.
    session.adopt(cookies, payload.origin)
    resumed = await asyncio.to_thread(resume_challenge_disabled)
    # Not "recovered" — nothing here has talked to the upstream. The state is
    # cleared and the next sweep will produce the actual evidence.
    log.info("cookies imported", extra={"resumed_monitors": len(resumed)})
    return _state(session)


@router.delete("/cookies", response_model=SessionState)
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
    return _state(session)
