"""mtop h5 collector — the cheap path.

Consumes the cookie jar established elsewhere (see session.py). Roughly 50 KB
and ~300 ms per search when the session is valid, versus hundreds of MB for
the browser path.
"""

import hashlib
import json
import logging
from typing import Any

import httpx

from app.collector import base
from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    ParseError,
    RawItem,
    RawSeller,
    TransientCollectorError,
)
from app.collector.session import CHALLENGE_COOKIE, UpstreamSession

log = logging.getLogger(__name__)

APP_KEY = "12574478"
BASE_URL = "https://h5api.m.goofish.com/h5"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
ITEM_API = "mtop.taobao.idle.pc.detail"
SELLER_API = "mtop.idle.web.user.page.head"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ret prefix -> action. Derived from observed responses; see the probe record.
_CHALLENGE_PREFIXES = ("RGV587_ERROR", "FAIL_SYS_ILLEGAL_ACCESS", "SM::")
_TOKEN_PREFIXES = ("FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED")
_TRANSIENT_PREFIXES = ("FAIL_SYS_TRAFFIC_LIMIT", "FAIL_SYS_SERVICE_FACADE_TIMEOUT", "ANDROID_SYS_")
_GONE_MARKERS = ("ITEM_NOT_FOUND", "ITEM_DELETED", "FAIL_BIZ_ITEM_NOT_EXIST")


def _sign(token: str, t: str, data: str) -> str:
    """mtop h5 signature: md5(token & timestamp & appKey & data)."""
    return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data}".encode()).hexdigest()


def classify_ret(ret: list[str] | None) -> None:
    """Raise the error class that matches the upstream `ret` envelope.

    Returns None when the response is a success. The mapping matters: a
    challenge must NOT be treated as a rate limit, or the scheduler backs off
    forever while the UI reports "throttled" and nobody ever verifies.
    """
    first = (ret or [""])[0]
    if first.startswith("SUCCESS"):
        return
    if any(m in first for m in _GONE_MARKERS):
        raise ItemGoneError(first)
    if any(first.startswith(p) or p in first for p in _CHALLENGE_PREFIXES):
        raise ChallengeError(first)
    if any(first.startswith(p) for p in _TOKEN_PREFIXES):
        # Token staleness is only recoverable if something else can refresh the
        # session; mtop cannot mint one itself.
        raise ChallengeError(first)
    if any(first.startswith(p) for p in _TRANSIENT_PREFIXES):
        raise TransientCollectorError(first)
    raise CollectorError(first or "empty ret envelope")


class MtopClient:
    """One client for the process lifetime, created by the lifespan.

    A per-request client would drop the cookie jar that the whole approach
    depends on.
    """

    def __init__(self, session: UpstreamSession, timeout: float = 15.0) -> None:
        self._session = session
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "user-agent": UA,
                "referer": "https://www.goofish.com/",
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, api: str, payload: dict[str, Any], now_ms: str) -> dict[str, Any]:
        sess = self._session
        token = sess.token
        if not token:
            raise ChallengeError("no _m_h5_tk: session not established")

        # Serialise ONCE and reuse. Re-dumping for the request body produces a
        # different string than the one that was signed, which fails as an
        # invalid signature and is indistinguishable from risk control.
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": now_ms,
            "sign": _sign(token, now_ms, data),
            "api": api,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "sessionOption": "AutoLoginOnly",
            "accountSite": "xianyu",
        }
        self._client.cookies.update(sess.cookies)
        try:
            response = await self._client.post(
                f"{BASE_URL}/{api}/1.0/", params=params, data={"data": data}
            )
        except httpx.TimeoutException as exc:
            raise TransientCollectorError(f"timeout calling {api}") from exc
        except httpx.HTTPError as exc:
            raise TransientCollectorError(f"transport error calling {api}: {exc}") from exc

        if (
            base.CHALLENGE_COOKIE if hasattr(base, "CHALLENGE_COOKIE") else False
        ):  # pragma: no cover
            pass
        if CHALLENGE_COOKIE in response.cookies:
            sess.mark_challenged("upstream issued x5secdata challenge cookie")

        try:
            body = response.json()
        except ValueError as exc:
            # A non-JSON body here is the risk-control interstitial HTML.
            raise ChallengeError(f"{api} returned non-JSON body") from exc

        try:
            classify_ret(body.get("ret"))
        except ChallengeError as exc:
            sess.mark_challenged(str(exc))
            raise
        return body.get("data") or {}

    async def search(self, keyword: str, page: int, rows: int, now_ms: str) -> list[RawItem]:
        data = await self._call(
            SEARCH_API,
            {
                "pageNumber": page,
                "keyword": keyword,
                "fromFilter": False,
                "rowsPerPage": rows,
                "sortValue": "",
                "sortField": "create",
                "searchReqFromPage": "pcSearch",
            },
            now_ms,
        )
        rows_out = _result_rows(data)
        if not rows_out:
            # Distinguish "genuinely nothing matched" from "shape changed".
            # Returning [] on a broken parse would poison the supply-trend
            # metric with fake zeroes.
            if not _looks_like_empty_result(data):
                raise ParseError(f"{SEARCH_API}: no recognisable result list in {sorted(data)}")
            return []
        items: list[RawItem] = []
        for row in rows_out:
            try:
                items.append(base.normalize_item(_flatten(row), source="mtop"))
            except ParseError as exc:
                log.warning("skipping unparseable row", extra={"err": str(exc)})
        if rows_out and not items:
            raise ParseError(f"{SEARCH_API}: {len(rows_out)} rows, none parseable")
        return items

    async def fetch_item(self, item_id: str, now_ms: str) -> RawItem:
        data = await self._call(ITEM_API, {"itemId": item_id}, now_ms)
        if not data:
            raise ItemGoneError(f"item {item_id} returned no data")
        return base.normalize_item(_flatten(data), source="mtop")

    async def fetch_seller(self, seller_id: str, now_ms: str) -> RawSeller:
        data = await self._call(SELLER_API, {"userId": seller_id}, now_ms)
        return base.normalize_seller(_flatten(data), seller_id=seller_id, source="mtop")


# --------------------------------------------------------------------------- #
# Shape helpers
# --------------------------------------------------------------------------- #
# Key names are unverified — every probe was blocked before a result list came
# back. Candidates live here so one edit fixes them; capture a real payload
# with `uv run python -m scripts.capture_fixture`.

_RESULT_KEYS = ("resultList", "items", "itemList", "data")
_EMPTY_MARKER_KEYS = ("resultInfo", "totalCount", "total", "count")


def _result_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _RESULT_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def _looks_like_empty_result(data: dict[str, Any]) -> bool:
    """True when the payload plausibly says "zero matches" rather than
    "we no longer understand this payload"."""
    for key in _EMPTY_MARKER_KEYS:
        value = data.get(key)
        if isinstance(value, int) and value == 0:
            return True
        if isinstance(value, str) and value.strip() in ("0", ""):
            return True
    return any(isinstance(data.get(k), list) for k in _RESULT_KEYS)


def _flatten(row: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
    """Collapse nested wrapper dicts into one level.

    mtop rows habitually wrap the payload (`{"data": {...}}`,
    `{"itemCardData": {...}}`). Flattening keeps the field map one level deep
    instead of encoding a nesting path per field. Outer keys win, so a real
    top-level value is never shadowed by a nested one.
    """
    flat: dict[str, Any] = {}
    nested: list[dict[str, Any]] = []
    for key, value in row.items():
        if isinstance(value, dict) and _depth < 3:
            nested.append(value)
            flat.setdefault(key, value)
        else:
            flat.setdefault(key, value)
    for child in nested:
        for key, value in _flatten(child, _depth + 1).items():
            flat.setdefault(key, value)
    return flat
