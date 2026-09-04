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
    last_error: str | None = None
    # Risk control is applied PER ENDPOINT, not per session: measured live, the
    # detail API was demanding validation while search kept working normally.
    # One global flag therefore auto-disabled healthy keyword rules because a
    # watchlist lookup got challenged.
    challenged_apis: dict[str, str] = field(default_factory=dict)
    # When an upstream call last SUCCEEDED. `usable` only means "there is a
    # token"; `adopt()` clears the challenge map unconditionally, so right
    # after an import the session looks perfectly healthy while the very next
    # call may fail. Found in T8: the panel showed a green "session usable"
    # for a freshly imported cookie whose detail endpoint was still
    # challenged. Nothing had failed *yet*, which is not the same as working.
    last_success_at: datetime | None = None

    @property
    def proven(self) -> bool:
        """Whether an upstream call has actually succeeded since the import.

        The UI must distinguish "credentials accepted and working" from
        "credentials present, never exercised".
        """
        return (
            self.last_success_at is not None
            and self.established_at is not None
            and self.last_success_at >= self.established_at
        )

    def mark_success(self, now: datetime | None = None) -> None:
        self.last_success_at = now or datetime.now(UTC)

    @property
    def token(self) -> str | None:
        """The `_m_h5_tk` prefix used in the mtop signature."""
        raw = self.cookies.get(TOKEN_COOKIE)
        return raw.split("_")[0] if raw else None

    @property
    def usable(self) -> bool:
        """Has a token and nothing globally blocks its use."""
        return bool(self.token)

    def is_challenged(self, api: str) -> bool:
        return api in self.challenged_apis

    @property
    def needs_verification(self) -> bool:
        """Whether a human has to act.

        Derived from the endpoints actually blocked, so the UI can say *what*
        stopped working instead of declaring everything broken.
        """
        return bool(self.challenged_apis)

    def adopt(self, cookies: dict[str, str], origin: str) -> None:
        self.cookies.update(cookies)
        self.origin = origin
        self.established_at = datetime.now(UTC)
        self.challenged_apis.clear()
        # `established_at` moving forward is what withdraws the previous
        # credential's proof: `proven` requires last_success_at >=
        # established_at, so an older success stops counting on its own. An
        # explicit reset here was written first and then removed -- it was a
        # second mechanism for the same job, and a test could not tell whether
        # either one worked.
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

    def mark_challenged(self, api: str, detail: str) -> None:
        """Record that ONE endpoint wants human verification.

        Scoped per API on purpose: search and detail are challenged
        independently, and collapsing them takes a working path down with a
        blocked one.
        """
        self.challenged_apis[api] = detail
        self.last_error = f"{api}: {detail}"
        log.warning(
            "endpoint challenged, verification required",
            extra={"api": api, "detail": detail},
        )

    def clear_challenge(self, api: str) -> None:
        """An endpoint answered normally again."""
        if self.challenged_apis.pop(api, None) is not None:
            log.info("endpoint recovered", extra={"api": api})
