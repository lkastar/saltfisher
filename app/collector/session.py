"""Shared upstream session state.

Direction of travel, corrected by measurement (research/mtop-access-probe.md):
a bare HTTP client is NEVER issued an `_m_h5_tk` token — the search endpoint
answers with a risk-control challenge instead. So the browser (or cookies the
user exports from their own verified browser) establishes the session, and the
cheap mtop path consumes it. mtop never bootstraps itself.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# Cookies that actually matter for mtop calls. Others are carried along
# untouched; these are the ones whose absence means "no usable session".
TOKEN_COOKIE = "_m_h5_tk"
CHALLENGE_COOKIE = "x5secdata"


@dataclass
class UpstreamSession:
    """Cookie jar plus a health state the UI can render.

    `needs_verification` is the difference between "wait and retry" and
    "a human has to do something". Collapsing the two is what makes a
    monitoring tool sit silently doing nothing.
    """

    cookies: dict[str, str] = field(default_factory=dict)
    origin: str = "none"  # none | browser | imported
    established_at: datetime | None = None
    needs_verification: bool = False
    last_error: str | None = None

    @property
    def token(self) -> str | None:
        """The `_m_h5_tk` prefix used in the mtop signature."""
        raw = self.cookies.get(TOKEN_COOKIE)
        return raw.split("_")[0] if raw else None

    @property
    def usable(self) -> bool:
        return bool(self.token) and not self.needs_verification

    def adopt(self, cookies: dict[str, str], origin: str) -> None:
        self.cookies.update(cookies)
        self.origin = origin
        self.established_at = datetime.now(UTC)
        self.needs_verification = not self.token
        self.last_error = None if self.token else "no _m_h5_tk in adopted cookies"
        log.info(
            "session adopted",
            extra={
                "origin": origin,
                "has_token": bool(self.token),
                "cookie_count": len(self.cookies),
            },
        )

    def refresh(self, cookies: dict[str, str]) -> None:
        """Absorb rotated cookies from a response.

        `_m_h5_tk` is short-lived and the server hands back a fresh one on
        every response, including on the "token expired" error. Without
        absorbing it the stored token goes stale within hours and every call
        fails — which previously auto-disabled every rule and demanded a
        manual cookie re-import for a problem the server had already solved.

        Deliberately does not touch `origin` or health: this is a rotation, not
        a new session.
        """
        if not cookies:
            return
        before = self.token
        self.cookies.update(cookies)
        if self.token and self.token != before:
            log.debug("session token rotated")

    def mark_challenged(self, detail: str) -> None:
        """Risk control wants a human. Stop retrying and say so."""
        self.needs_verification = True
        self.last_error = detail
        log.warning("session challenged, verification required", extra={"detail": detail})


# Deliberately NO module-level singleton. An imported-by-value global cannot be
# replaced (only mutated), so anything that re-establishes the session would be
# invisible to modules that imported it — and it violates the project's own
# forbidden-patterns rule in .trellis/spec/backend/quality-guidelines.md.
# The lifespan owns one instance and injects it.
