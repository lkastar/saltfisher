"""mtop h5 collector — the cheap path.

Consumes the cookie jar established elsewhere (see session.py). Roughly 50 KB
and ~300 ms per search when the session is valid, versus hundreds of MB for
the browser path.
"""

import hashlib
import json
import logging
import time
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
from app.collector.fingerprint import Fingerprint
from app.collector.session import CHALLENGE_COOKIE, UpstreamSession

log = logging.getLogger(__name__)

APP_KEY = "12574478"
BASE_URL = "https://h5api.m.goofish.com/h5"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
ITEM_API = "mtop.taobao.idle.pc.detail"
# No separate seller endpoint exists in our path: the item detail response
# carries a richer `sellerDO` than a profile page would, and the three other
# candidate API names all returned FAIL_SYS_API_NOT_FOUNDED (verified
# 2026-09-04, see the task research note).

# ret prefix -> action. Derived from observed responses; see the probe record.
_CHALLENGE_PREFIXES = (
    "RGV587_ERROR",
    "FAIL_SYS_ILLEGAL_ACCESS",
    # Observed live on the detail endpoint after a burst of requests: risk
    # control demands human validation. It is a challenge, not a transport
    # failure — classifying it as a generic CollectorError would send every
    # subsequent cycle through a browser launch that is equally blocked.
    "FAIL_SYS_USER_VALIDATE",
    "SM::",
)
# Note the upstream typo: EXOIRED, not EXPIRED. Both are matched because the
# spelling is not ours to rely on.
_TOKEN_PREFIXES = ("FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED")
_TRANSIENT_PREFIXES = ("FAIL_SYS_TRAFFIC_LIMIT", "FAIL_SYS_SERVICE_FACADE_TIMEOUT", "ANDROID_SYS_")
# Only the first of these has been OBSERVED. Measured 2026-09-05 against two
# genuinely deleted listings:
#   FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!
# The rest were guessed before real data existed and are kept as cheap
# insurance. Note the observed name does NOT contain "ITEM_NOT_FOUND" as a
# substring -- it is ITEM_DEL_NOT_FOUND -- so the guesses never matched and a
# deleted item raised a plain CollectorError. That made the watchlist record a
# failure instead of marking the entry gone, so the `gone` notification could
# never fire for a deleted listing, and a pasted link to one answered 502
# "could not fetch" instead of 404 "no longer exists".
_GONE_MARKERS = (
    "FAIL_BIZ_ITEM_DEL_NOT_FOUND",
    "ITEM_NOT_FOUND",
    "ITEM_DELETED",
    "FAIL_BIZ_ITEM_NOT_EXIST",
)


def _sign(token: str, t: str, data: str) -> str:
    """mtop h5 signature: md5(token & timestamp & appKey & data)."""
    return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data}".encode()).hexdigest()


class TokenStaleError(CollectorError):
    """The signature token expired. Recoverable WITHOUT human help.

    Measured: the response that reports this also carries a fresh `_m_h5_tk`
    in Set-Cookie, so absorbing it and retrying once succeeds. Classifying it
    as a challenge instead auto-disabled every rule a few hours after start
    and asked the user to re-import cookies for nothing.
    """


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
        raise TokenStaleError(first)
    if any(first.startswith(p) for p in _TRANSIENT_PREFIXES):
        raise TransientCollectorError(first)
    raise CollectorError(first or "empty ret envelope")


class MtopClient:
    """One client for the process lifetime, created by the lifespan.

    A per-request client would drop the cookie jar that the whole approach
    depends on.
    """

    def __init__(
        self,
        session: UpstreamSession,
        fingerprint: Fingerprint | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._session = session
        # Shared with the browser collector so both paths claim one identity.
        # Optional so the many tests that only care about signing keep working
        # -- an absent fingerprint is the built-in defaults, which is what the
        # devtools-paste deployment runs on anyway.
        self._fingerprint = fingerprint or Fingerprint()
        # The identity headers are NOT baked in here: they are read per request
        # (below) so a credential import applies on the next call instead of
        # the next restart, and so a snapshot without client hints does not
        # inherit the previous one's.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "referer": "https://www.goofish.com/",
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        api: str,
        payload: dict[str, Any],
        now_ms: str,
        *,
        session_health: bool = True,
    ) -> dict[str, Any]:
        """Call one mtop API.

        `session_health=False` for auxiliary calls: a challenge on the seller
        profile endpoint is not evidence that the session is dead — the search
        call moments earlier succeeded. Marking the shared session from an
        auxiliary failure took the working search path down with it and
        auto-disabled the rule (seen on live data).
        """
        sess = self._session

        # Serialise ONCE and reuse. Re-dumping for the request body produces a
        # different string than the one that was signed, which fails as an
        # invalid signature and is indistinguishable from risk control.
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "api": api,
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "sessionOption": "AutoLoginOnly",
            "accountSite": "xianyu",
        }

        # Two attempts, because `_m_h5_tk` rotates: the response that reports
        # the token expired is the one carrying its replacement. Absorbing it
        # and retrying once recovers without any human action. Treating it as
        # a challenge instead auto-disabled every rule a few hours after
        # startup and asked for a manual cookie re-import for nothing.
        for attempt in (1, 2):
            token = sess.token
            if not token:
                # Rotation cannot help here, and reading TokenStaleError's
                # "recoverable without human help" as if it could is a mistake
                # that has been made once already: rotation replaces a token
                # that EXISTS, and with none at all there is nothing to sign,
                # so the response that would carry a replacement never happens.
                # A tokenless session is the browser path's job -- a real page
                # load lets goofish's own JS mint one (pipeline.py routes on
                # `session.usable` for exactly this).
                raise ChallengeError("no _m_h5_tk: session not established")
            stamp = now_ms if attempt == 1 else str(int(time.time() * 1000))
            signed = {**params, "t": stamp, "sign": _sign(token, stamp, data)}

            self._client.cookies.update(sess.cookies)
            try:
                response = await self._client.post(
                    f"{BASE_URL}/{api}/1.0/",
                    params=signed,
                    data={"data": data},
                    headers=self._fingerprint.http_headers(),
                )
            except httpx.TimeoutException as exc:
                raise TransientCollectorError(f"timeout calling {api}") from exc
            except httpx.HTTPError as exc:
                raise TransientCollectorError(f"transport error calling {api}: {exc}") from exc

            # Absorb rotated cookies first: the error response carries the
            # fresh token, so this is what makes the retry work.
            sess.refresh(dict(response.cookies))

            if CHALLENGE_COOKIE in response.cookies and session_health:
                sess.mark_challenged(api, "upstream issued x5secdata challenge cookie")

            try:
                body = response.json()
            except ValueError as exc:
                # A non-JSON body here is the risk-control interstitial HTML.
                raise ChallengeError(f"{api} returned non-JSON body") from exc

            try:
                classify_ret(body.get("ret"))
            except TokenStaleError as exc:
                if attempt == 1:
                    log.info("token rotated, retrying once", extra={"api": api})
                    continue
                # The refreshed token did not help, so the session really is
                # unusable and a human has to re-establish it.
                if not session_health:
                    raise CollectorError(f"{api} unavailable: {exc}") from exc
                sess.mark_challenged(api, str(exc))
                raise ChallengeError(str(exc)) from exc
            except ChallengeError as exc:
                if not session_health:
                    # Auxiliary path: report it as merely unavailable so callers
                    # can degrade one filter instead of the whole session.
                    raise CollectorError(f"{api} unavailable: {exc}") from exc
                sess.mark_challenged(api, str(exc))
                raise
            sess.clear_challenge(api)
            # One real success is what turns "credentials present" into
            # "credentials working" for the settings page.
            sess.mark_success()
            return body.get("data") or {}

        raise CollectorError(f"{api}: token retry exhausted")

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
                items.append(base.normalize_item(flatten_search_row(row), source="mtop"))
            except ParseError as exc:
                log.warning("skipping unparseable row", extra={"err": str(exc)})
        if rows_out and not items:
            raise ParseError(f"{SEARCH_API}: {len(rows_out)} rows, none parseable")
        return items

    async def fetch_item(self, item_id: str, now_ms: str) -> tuple[RawItem, RawSeller | None]:
        """Item detail plus its seller profile, in ONE call.

        The detail response carries `sellerDO`, so a separate seller endpoint
        would be a second request for less data — and its `sellerId` lands in
        a different id space than the opaque token search returns.
        """
        data = await self._call(ITEM_API, {"itemId": item_id}, now_ms)
        item_do = data.get("itemDO") or {}
        if not item_do:
            raise ItemGoneError(f"item {item_id}: detail response carries no itemDO")
        seller_do = data.get("sellerDO") or {}
        item = base.normalize_item(flatten_detail(item_do, seller_do, item_id), source="detail")
        seller = None
        if seller_do:
            seller = base.normalize_seller(
                flatten_seller(seller_do),
                seller_id=str(seller_do.get("sellerId") or ""),
                source="detail",
            )
        return item, seller


# --------------------------------------------------------------------------- #
# Shape helpers
# --------------------------------------------------------------------------- #
# Key names are unverified — every probe was blocked before a result list came
# back. Candidates live here so one edit fixes them; capture a real payload
# with `uv run python -m scripts.capture_fixture`.

_RESULT_KEYS = ("resultList", "items", "itemList")
_SHOP_IDENTITY_HINTS = ("严选", "服务商", "专营", "旗舰", "官方", "商家", "鱼小铺")


def _result_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _RESULT_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def _looks_like_empty_result(data: dict[str, Any]) -> bool:
    """True when the payload plausibly says "zero matches" rather than
    "we no longer understand this payload".

    `resultInfo` is present on every real response, so its absence together
    with an absent result list means the shape moved.
    """
    if any(isinstance(data.get(k), list) for k in _RESULT_KEYS):
        return True
    return "resultInfo" in data


def flatten_search_row(row: dict[str, Any]) -> dict[str, Any]:
    """Real nested search row -> the flat keys ITEM_FIELD_MAP expects.

    The live shape (verified 2026-09-04) is:

        row.data.item.main.exContent      display fields, area, nick, picUrl
        row.data.item.main.exContent.detailParams
                                          clean soldPrice + the LONG title
        row.data.item.main.clickParam.args
                                          seller_id (opaque, stable), price
        row.data.item.main.exContent.fishTags.r2.tagList[].data.content
                                          fuzzy publish label, e.g. 刚刚发布

    Explicit paths rather than a generic flatten: guessing was only justified
    while the payload was unknown, and a blind merge here would let
    `exContent.price` (a rich-text segment list) shadow the clean
    `detailParams.soldPrice`.
    """
    main = ((row.get("data") or {}).get("item") or {}).get("main") or {}
    ex = main.get("exContent") or {}
    detail = ex.get("detailParams") or {}
    args = (main.get("clickParam") or {}).get("args") or {}

    flat: dict[str, Any] = {
        "itemId": ex.get("itemId") or detail.get("itemId") or args.get("item_id"),
        "title": ex.get("title") or detail.get("title"),
        # The long text lives on detailParams.title and is what the condition
        # and shipping heuristics need to read.
        "detailTitle": detail.get("title"),
        "soldPrice": detail.get("soldPrice"),
        "argsPrice": args.get("price") or args.get("displayPrice"),
        "picUrl": ex.get("picUrl"),
        "area": ex.get("area"),
        "seller_id": args.get("seller_id") or args.get("user_id"),
        "userNickName": ex.get("userNickName") or detail.get("userNick"),
        "userAvatarUrl": ex.get("userAvatarUrl"),
        "want": ex.get("want"),
        "seller_is_shop": _guess_is_shop(ex),
        "publish_hint": _publish_hint(ex),
    }
    reviews, rate = _seller_reputation(ex)
    flat["seller_review_count"] = reviews
    flat["seller_positive_rate"] = rate
    return {k: v for k, v in flat.items() if v not in (None, "")}


def _guess_is_shop(ex: dict[str, Any]) -> bool | None:
    """Merchant signal from the search result — positive only.

    `userIdentityShow` gives a trustworthy POSITIVE signal when present
    (闲鱼严选卖家 / 手机严选授权服务商). Its absence proves nothing: the live
    capture had a row nicknamed 杭州靓机汇二手机批发 — plainly a wholesaler — with
    an empty identity field.

    So an unrecognised row returns None (unknown), never False. Returning False
    here would let `exclude_shop` silently drop personal-looking merchants and,
    worse, would report certainty we do not have. Unknown defers to the seller
    profile and, failing that, waives the filter with a label.

    (`userFishShopLabel` is NOT a shop flag despite the name — it carries
    review count and positive-rate text, and is present on every row.)
    """
    identity = str(ex.get("userIdentityShow") or "")
    if identity and any(h in identity for h in _SHOP_IDENTITY_HINTS):
        return True
    if ex.get("userIsUseFishShopCard") is True:
        return True
    return None


def _seller_reputation(ex: dict[str, Any]) -> tuple[int | None, float | None]:
    """Review count and positive rate, free of charge in every search row.

    `userFishShopLabel.tagList` renders as e.g. ["4737条评价", "好评率47%"].
    Both are seller-quality judgements the LLM item advice needs, and getting
    them here avoids a seller-page request per item.
    """
    reviews: int | None = None
    rate: float | None = None
    for tag in (ex.get("userFishShopLabel") or {}).get("tagList", []) or []:
        text = str((tag.get("data") or {}).get("content") or "")
        digits = "".join(c for c in text if c.isdigit() or c == ".")
        if not digits:
            continue
        if "评价" in text:
            reviews = int(float(digits))
        elif "好评" in text or "%" in text:
            rate = float(digits)
    return reviews, rate


def _publish_hint(ex: dict[str, Any]) -> str | None:
    for row in (ex.get("fishTags") or {}).get("r2", {}).get("tagList", []) or []:
        content = (row.get("data") or {}).get("content")
        if content:
            return str(content)
    return None


# --------------------------------------------------------------------------- #
# Detail shape (verified against a live response, 2026-09-04)
# --------------------------------------------------------------------------- #
# Unlike a search row, the detail payload states as FACTS what search could
# only guess: 成色 is a structured cpvLabel and 包邮 a commonTag. An item
# fetched this way must therefore not be tagged "heuristic".

_CONDITION_PROPERTY = "成色"
_FREE_SHIPPING_TAG = "包邮"


def _cpv(item_do: dict[str, Any], property_name: str) -> str | None:
    for label in item_do.get("cpvLabels") or []:
        if label.get("propertyName") == property_name:
            value = label.get("valueName")
            return str(value) if value else None
    return None


def detail_status(item_do: dict[str, Any]) -> str:
    """Upstream status -> on_sale | sold | removed.

    Observed values, measured 2026-09-05 across ten real listings:

    | `itemStatus` | `itemStatusStr` | mapped to |
    |---|---|---|
    | `0` | 在线 | `on_sale` |
    | `-2` | 已下架 | `removed` |

    A **deleted** listing never reaches here: the envelope answers
    `FAIL_BIZ_ITEM_DEL_NOT_FOUND` and `classify_ret` raises `ItemGoneError`.

    **`sold` is still unobserved.** No listing happened to sell during the
    observation window, so the 售/成交/成功 markers below remain a guess. The
    default is `removed`, and that direction is the safe one: an item wrongly
    reported as gone is noticed immediately, while one wrongly reported as on
    sale is silently never followed up. The PRD also declines to claim we can
    tell a sale from a delisting — the analytics metric is "time until it
    disappeared", not a sell-through rate.
    """
    raw_status = str(item_do.get("itemStatus") or "")
    text = str(item_do.get("itemStatusStr") or "")
    if raw_status == "0" or text == "在线":
        return "on_sale"
    # `-2`/已下架 is measured; these markers are still GUESSES because no sale
    # was observed. Best-effort labelling; both outcomes collapse to "no
    # longer on sale" everywhere it matters.
    if any(marker in text for marker in ("售", "成交", "成功")):
        return "sold"
    return "removed"


def flatten_detail(
    item_do: dict[str, Any], seller_do: dict[str, Any], item_id: str
) -> dict[str, Any]:
    """Detail payload -> the flat keys ITEM_FIELD_MAP expects."""
    images = [str(info.get("url")) for info in item_do.get("imageInfos") or [] if info.get("url")]
    tags = tuple(str(tag.get("text")) for tag in item_do.get("commonTags") or [] if tag.get("text"))
    flat: dict[str, Any] = {
        "itemId": item_do.get("itemId") or item_id,
        "title": item_do.get("title"),
        # The long description lives on `desc` here, not on a nested title.
        "detailTitle": item_do.get("desc"),
        "soldPrice": item_do.get("soldPrice"),
        "picUrl": images[0] if images else None,
        "images": images,
        "area": seller_do.get("publishCity") or seller_do.get("city"),
        "seller_id": seller_do.get("sellerId"),
        "userNickName": seller_do.get("nick"),
        "userAvatarUrl": seller_do.get("portraitUrl"),
        "want": item_do.get("wantCnt"),
        "browseCnt": item_do.get("browseCnt"),
        "publishTime": item_do.get("gmtCreate"),
        "condition_fact": _cpv(item_do, _CONDITION_PROPERTY),
        "free_shipping_fact": (_FREE_SHIPPING_TAG in tags) or item_do.get("transportFee") == "0.00",
        "status": detail_status(item_do),
    }
    return {k: v for k, v in flat.items() if v not in (None, "")}


def flatten_seller(seller_do: dict[str, Any]) -> dict[str, Any]:
    """`sellerDO` -> the flat keys SELLER_FIELD_MAP expects."""
    credit = (seller_do.get("idleFishCreditTag") or {}).get("trackParams") or {}
    remarks = seller_do.get("remarkDO") or {}
    review_count = sum(
        int(remarks.get(key) or 0)
        for key in ("sellerGoodRemarkCnt", "sellerBadRemarkCnt", "sellerDefaultRemarkCnt")
    )
    flat: dict[str, Any] = {
        "nick": seller_do.get("nick") or seller_do.get("uniqueName"),
        "avatar": seller_do.get("portraitUrl"),
        "creditLevel": credit.get("sellerLevel"),
        "soldCount": seller_do.get("hasSoldNumInteger"),
        "registerDays": seller_do.get("userRegDay"),
        "replyRate": seller_do.get("replyRatio24h"),
        "goodRate": seller_do.get("newGoodRatioRate"),
        "verified": seller_do.get("zhimaAuth"),
        "reviewCount": review_count or None,
        # A personal seller does not keep 189 listings on sale. This is a
        # stronger merchant signal than the identity label, which the live
        # search capture showed to be absent on obvious wholesalers.
        "listingCount": seller_do.get("itemCount"),
        # The one place the NUMERIC seller id is available. Search returns an
        # opaque base64 token instead, and the seller-listing API rejects that
        # form, so this is where the mapping between the two id spaces comes
        # from (P5/T1).
        "numericId": seller_do.get("sellerId"),
    }
    return {k: v for k, v in flat.items() if v not in (None, "")}
