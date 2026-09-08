"""One description of the machine the credentials came from.

The cookies are exported from the user's own browser, but until this module
existed the requests carrying them announced a different browser, on a
different operating system, in a different timezone -- and did so from *two*
independent copies of the same hardcoded user-agent, one in `mtop.py` and one
in `browser.py`, free to drift apart with nothing to catch it.

Three rules shape what is here:

1. **No snapshot means today's behaviour, unchanged.** The devtools paste
   cannot produce one and it is the documented fallback, so it must not get
   worse for lacking a fingerprint. Every default below is the literal value
   that used to be hardcoded in the two collectors.
2. **A mobile snapshot is stored and NOT applied.** Every URL and XHR name
   this project drives is the PC site (`www.goofish.com/search`,
   `mtop.taobao.idlemtopsearch.pc.search`, `mtop.taobao.idle.pc.detail`), so a
   phone's user-agent would trade one inconsistency for a worse one -- and the
   `pc.search` XHR may not fire at all.
3. **The snapshot is not a credential, but it is still not for showing.**
   Only `summary()` leaves the process; see `api/session.py`.

It is persisted because the alternative is an identity that swings back to the
built-in defaults on every restart, which is a bigger signal than never having
had one. `data/state.json` is deliberately not the place: that file's format
belongs to Playwright's own serializer (see `browser.py`).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# The single source of truth for "who is asking". `mtop.py` and `browser.py`
# each had their own byte-identical copy of this string; two literals meaning
# one thing is a drift waiting to happen, and the httpx path and the Playwright
# path disagreeing about the browser is exactly the contradiction this module
# exists to remove.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

FINGERPRINT_FILENAME = "fingerprint.json"

# Substrings that mean "this snapshot came off a phone". `userAgentData.mobile`
# is the authoritative answer where it exists; these cover the browsers that
# do not ship it.
MOBILE_MARKERS = ("mobile", "android", "iphone")

# Chromium pads its brand list with a randomised nonsense entry so that servers
# cannot hardcode the shape of it ("GREASE"). It is real and must be forwarded
# verbatim in `sec-ch-ua`; it just must not be shown to a human as the browser
# they are running.
_GREASE = re.compile(r"not.*brand", re.IGNORECASE)


def _sec_ch_ua(brands: list[dict[str, Any]]) -> str:
    """Rebuild the `sec-ch-ua` header from `navigator.userAgentData.brands`.

    Order is preserved exactly as the browser reported it: Chromium randomises
    both the position of the GREASE entry and the order of the real ones, so
    sorting it would produce a list no browser would ever send.
    """
    parts = [
        f'"{b["brand"]}";v="{b["version"]}"'
        for b in brands
        if isinstance(b, dict) and b.get("brand") and b.get("version")
    ]
    return ", ".join(parts)


def _accept_language(languages: list[str]) -> str:
    """`navigator.languages` -> the header Chrome derives from it.

    First entry unweighted, then q descending by 0.1 with a 0.1 floor, which is
    what Chrome sends for the same list.
    """
    head, *rest = languages
    return ",".join(
        [head] + [f"{lang};q={max(0.1, round(1 - i / 10, 1))}" for i, lang in enumerate(rest, 1)]
    )


@dataclass
class Fingerprint:
    """The environment snapshot, plus everything derived from it.

    Mutable and shared, like `UpstreamSession`: `MtopClient` reads it per
    request, so re-importing takes effect on the next call rather than the next
    restart. The Playwright context is built once at startup and therefore
    picks up a new snapshot only after a restart -- rebuilding a live context
    mid-collection would cost the running cycle, and the persisted file is what
    makes the restart enough.
    """

    snapshot: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #

    @property
    def mobile(self) -> bool:
        ua_data = self.snapshot.get("ua_data") or {}
        if ua_data.get("mobile") is True:
            return True
        ua = str(self.snapshot.get("user_agent") or "").lower()
        return any(marker in ua for marker in MOBILE_MARKERS)

    @property
    def applied(self) -> bool:
        """Whether the snapshot is actually driving the requests."""
        return bool(self.snapshot) and not self.mobile

    def _live(self) -> dict[str, Any]:
        """The snapshot when it is allowed to shape a request, else nothing.

        One gate for both paths: reading `self.snapshot` directly anywhere
        below is how a mobile user-agent would leak into one of them.
        """
        return self.snapshot if self.applied else {}

    # ------------------------------------------------------------------ #

    @property
    def user_agent(self) -> str:
        return str(self._live().get("user_agent") or DEFAULT_UA)

    def http_headers(self) -> dict[str, str]:
        """Identity headers for the mtop (httpx) path -- the primary one.

        Without a usable snapshot this is exactly the one header the client
        used to carry, so the devtools paste flow sends what it always did.
        """
        snap = self._live()
        headers = {"user-agent": self.user_agent}

        ua_data = snap.get("ua_data") or {}
        brands = _sec_ch_ua(ua_data.get("brands") or [])
        if brands:
            headers["sec-ch-ua"] = brands
            # Only alongside the brands: a client hint header on its own from a
            # browser that has no `userAgentData` is itself a contradiction.
            headers["sec-ch-ua-mobile"] = "?1" if ua_data.get("mobile") else "?0"
        if ua_data.get("platform"):
            headers["sec-ch-ua-platform"] = f'"{ua_data["platform"]}"'

        languages = snap.get("languages") or ([snap["language"]] if snap.get("language") else [])
        if languages:
            headers["accept-language"] = _accept_language(list(languages))
        return headers

    def context_options(self) -> dict[str, Any]:
        """Playwright `new_context` kwargs -- the fallback path.

        Without a usable snapshot these are the four values `browser.py` used
        to pass literally, `device_scale_factor` included by its absence.
        """
        snap = self._live()
        options: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": snap.get("locale") or DEFAULT_LOCALE,
            "timezone_id": snap.get("time_zone") or DEFAULT_TIMEZONE,
            "viewport": DEFAULT_VIEWPORT,
        }
        width, height = snap.get("screen_width"), snap.get("screen_height")
        if width and height:
            # ponytail: the screen, used as the viewport. Playwright has no
            # window chrome to subtract and reports `window.screen` as the
            # viewport when nothing else is set, so this is one number rather
            # than two, one of which we would be inventing.
            options["viewport"] = {"width": int(width), "height": int(height)}
        if snap.get("device_pixel_ratio"):
            options["device_scale_factor"] = float(snap["device_pixel_ratio"])
        return options

    # ------------------------------------------------------------------ #

    def summary(self) -> str | None:
        """One line a human can check, and the ONLY thing that may be shown.

        Same rule as the cookie list: names and shapes, never the material.
        `hardwareConcurrency` and `deviceMemory` are in the snapshot and are
        not in here, on purpose -- a panel that renders them is a panel that
        hands a page's worth of fingerprint back to whoever can read it.
        """
        snap = self.snapshot
        if not snap:
            return None
        parts = [
            p
            for p in (
                self._browser_label(),
                snap.get("ua_data", {}).get("platform") or snap.get("platform"),
                f"{snap['screen_width']}×{snap['screen_height']}"
                if snap.get("screen_width") and snap.get("screen_height")
                else None,
                snap.get("time_zone"),
            )
            if p
        ]
        return " · ".join(parts) if parts else None

    def _browser_label(self) -> str | None:
        brands = (self.snapshot.get("ua_data") or {}).get("brands") or []
        real = [b for b in brands if b.get("brand") and not _GREASE.search(b["brand"])]
        # "Chromium" is true of every Chromium browser and therefore says the
        # least; keep it only when it is all there is.
        named = [b for b in real if b["brand"] != "Chromium"] or real
        if named:
            return f"{named[0]['brand']} {named[0].get('version', '')}".strip()
        # Firefox and Safari have no `userAgentData` at all, and they are
        # precisely the snapshots where the label is worth the regex.
        ua = str(self.snapshot.get("user_agent") or "")
        match = re.search(r"(Firefox|Edg|Chrome|Version)/(\d+)", ua)
        if not match:
            return None
        # Safari writes its own version as `Version/17.5` and puts a Safari
        # build number elsewhere; "Version 17" is not a browser anyone
        # recognises on a settings page.
        token = {"Version": "Safari", "Edg": "Edge"}.get(match.group(1), match.group(1))
        return f"{token} {match.group(2)}"

    # ------------------------------------------------------------------ #

    def adopt(self, snapshot: dict[str, Any]) -> None:
        """Take a snapshot from an import and write it down.

        Written through immediately rather than at shutdown: the process that
        crashes is the one whose fingerprint would otherwise be lost, and an
        identity that reverts to the built-in defaults on the next start is
        worse than one that never moved.
        """
        self.snapshot = snapshot
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # Not fatal: the import itself succeeded and the in-memory
            # fingerprint is live. Only the surviving-a-restart part is lost.
            log.warning("could not persist environment snapshot", exc_info=True)
        log.info(
            "environment snapshot adopted",
            extra={"summary": self.summary(), "applied": self.applied},
        )


def _path() -> Path:
    return settings.data_dir / FINGERPRINT_FILENAME


def load_fingerprint() -> Fingerprint:
    """Read the persisted snapshot, or start from the built-in defaults.

    A missing or unreadable file is not an error -- it is the devtools-paste
    deployment, which is the documented fallback.
    """
    path = _path()
    if not path.exists():
        return Fingerprint()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not read %s, using defaults", path, exc_info=True)
        return Fingerprint()
    return Fingerprint(data) if isinstance(data, dict) else Fingerprint()
