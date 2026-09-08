"""Boundary types and errors for the collector layer.

RawItem and RawSeller are the ONLY shapes allowed to leave app/collector/.
Upstream dicts, Playwright handles and raw JSON never escape their module —
otherwise every consumer grows its own field-name guesses.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import settings


class CollectorError(Exception):
    """Upstream data could not be acquired. Carries no user-facing text."""


class TransientCollectorError(CollectorError):
    """Network blip or genuine rate limit. Back off; do not switch collectors."""


class ChallengeError(CollectorError):
    """Risk control demanded human verification, or the session is invalid.

    Distinct from TransientCollectorError on purpose. Probing this project's
    target showed that a bare HTTP client is answered with
    `RGV587_ERROR::SM::...` plus an `x5secdata` cookie and never receives an
    `_m_h5_tk` token at all — see the probe record in this task's research/.
    Treating that as a rate limit makes the tool back off forever while telling
    the user "throttled", hiding the real cause: the session needs to be
    established or re-verified by a browser or by imported cookies.
    """


class ItemGoneError(CollectorError):
    """The listing no longer exists. A valid observation, not a failure."""


class ParseError(CollectorError):
    """Response arrived but did not contain the fields we need."""


@dataclass(frozen=True, slots=True)
class RawItem:
    """A normalised observation of one listing at one moment."""

    item_id: str
    title: str
    price_cents: int
    seller_id: str
    seller_nick: str
    source: str
    description: str | None = None
    cover_url: str | None = None
    image_urls: tuple[str, ...] = ()
    region: str | None = None
    seller_avatar_url: str | None = None
    publish_time: datetime | None = None
    want_count: int | None = None
    view_count: int | None = None
    status: str = "on_sale"
    # Merchant signal, available directly in search results via
    # `userIdentityShow` / `userFishShopLabel`. Carrying it here means the
    # exclude_shop filter usually needs no extra seller-page request at all.
    seller_is_shop: bool | None = None
    # Seller reputation, carried in every search row (review count + positive
    # rate). Free here; a seller-page request otherwise.
    seller_review_count: int | None = None
    seller_positive_rate: float | None = None
    # Fuzzy publish label ("刚刚发布" / "3天前"). Search results carry no
    # timestamp, only this tag, so a precise publish filter is not possible
    # from a list page — see filters.parse_publish_hint.
    publish_hint: str | None = None
    # Detail-only facts. None means "we only had a search row", which is what
    # keeps a heuristic guess from being presented as certainty.
    condition_fact: str | None = None
    free_shipping_fact: bool | None = None
    status_text: str | None = None
    # Field names that were expected but absent in the payload. Feeds the
    # "collector field map may be stale" warning rather than dying silently.
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResult:
    """What one search cycle actually managed to observe.

    A bare list cannot say "I asked for three pages and got two", and that
    difference is exactly what the supply chart must not read as a quiet
    market. `pages` is the observation aperture at the moment of collection;
    `partial_error` is non-None when a later page failed after an earlier one
    succeeded.
    """

    items: tuple[RawItem, ...]
    pages: int
    partial_error: str | None = None


@dataclass(frozen=True, slots=True)
class RawSeller:
    """A normalised seller profile. Every field is optional: None means
    "not available", which is a different thing from 0.
    """

    seller_id: str
    nick: str
    source: str
    avatar_url: str | None = None
    is_shop: bool | None = None
    credit_level: int | None = None
    verified: bool | None = None
    sold_count: int | None = None
    reply_rate: float | None = None
    review_count: int | None = None
    positive_rate: float | None = None
    account_age_days: int | None = None
    # On-sale listing count. 189 live listings is not a personal seller, and
    # this discriminates better than the identity label.
    listing_count: int | None = None
    missing_fields: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #
# Upstream key names are NOT verified: every probe from the development
# machine was blocked by risk control before a result list was ever returned
# (see research/mtop-access-probe.md). Candidates are listed per field so the
# first successful run on an unblocked network can be corrected in ONE place.
# Run `uv run python -m scripts.capture_fixture` there to dump a real payload.

# Verified against a live search response (2026-09-04) once a logged-in cookie
# session was available. The collector flattens the real nested payload into
# these keys first — see mtop.flatten_search_row.
#
# Two traps confirmed by that capture:
#   * `oriPrice` is the struck-through ORIGINAL price and is often higher than
#     the real one (¥2829 vs 2489). It must never be a price candidate.
#   * `exContent.price` is a rich-text SEGMENT LIST, not a number. The clean
#     value lives in `detailParams.soldPrice` / `clickParam.args.price`.
ITEM_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "item_id": ("itemId", "item_id", "id"),
    "title": ("title", "titleSpanContent", "name"),
    "price": ("soldPrice", "displayPrice", "argsPrice", "currentPrice", "price", "priceText"),
    "description": ("detailTitle", "desc", "description", "content"),
    "cover_url": ("picUrl", "imageUrl", "mainPic", "cover"),
    "image_urls": ("images", "picUrls", "imageUrls"),
    "region": ("area", "city", "region"),
    "seller_id": ("seller_id", "userId", "sellerId", "user_id"),
    "seller_nick": ("userNickName", "userNick", "nick", "sellerNick"),
    "seller_avatar_url": ("userAvatarUrl", "userAvatar", "avatar", "portrait"),
    "publish_time": ("publishTime", "gmtCreate", "createTime"),
    "want_count": ("want", "wantCnt", "wantCount", "collectCount"),
    "view_count": ("browseCnt", "viewCount", "pv"),
}

SELLER_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "nick": ("nick", "userNick", "displayName"),
    "avatar_url": ("avatar", "portrait", "userAvatar"),
    "is_shop": ("isShop", "shopFlag", "idleShop"),
    "credit_level": ("creditLevel", "level", "sesameLevel"),
    "verified": ("realNameVerified", "verified", "certified"),
    "sold_count": ("soldCount", "sellCount", "dealCount"),
    "reply_rate": ("replyRate", "responseRate"),
    "review_count": ("reviewCount", "rateCount", "evaluateCount"),
    "positive_rate": ("positiveRate", "goodRate", "praiseRate"),
    "listing_count": ("listingCount", "itemCount"),
    "account_age_days": ("accountAgeDays", "registerDays"),
}


def pick(payload: dict[str, Any], candidates: tuple[str, ...], *, scalar_only: bool = False) -> Any:
    """First candidate key present with a non-empty value, else None.

    `scalar_only` skips list/dict values instead of returning them, and it is
    what makes a shared candidate name safe across payload shapes: the live
    search response carries `price` as a list of rich-text segments
    ([{"text": "¥"}, {"text": "2619"}]) while a detail response may carry it as
    a clean string. Without the guard, the list is returned, the price parse
    fails, and the whole row is dropped instead of falling through to the next
    candidate.

    Nested payloads are flattened by the caller; this stays deliberately dumb
    so a wrong guess shows up as a missing field rather than a wrong value.
    """
    for key in candidates:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        if scalar_only and isinstance(value, list | dict):
            continue
        return value
    return None


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


def parse_price_cents(raw: str | int | float | None) -> int:
    """Yuan in any of the shapes upstream uses -> integer cents.

    Accepts "¥1,200", "1200.00", "1200元", 1200, 1200.5.

    The input is always YUAN. There is deliberately no "looks like it is
    already cents" heuristic: guessing the unit is how money code silently
    doubles or halves every price. Callers that hold cents must not route
    through here.
    """
    if raw is None or raw == "":
        raise ParseError("price is empty")
    if isinstance(raw, bool):  # bool is an int subclass; never a price
        raise ParseError(f"price is not numeric: {raw!r}")
    if isinstance(raw, int | float):
        yuan = float(raw)
    else:
        cleaned = (
            str(raw)
            .replace(",", "")
            .replace("¥", "")
            .replace("￥", "")
            .replace("元", "")
            .replace(" ", "")
            .strip()
        )
        if not cleaned:
            raise ParseError(f"price has no digits: {raw!r}")
        try:
            yuan = float(cleaned)
        except ValueError as exc:
            # "面议" / "详聊" and friends are not prices.
            raise ParseError(f"price is not numeric: {raw!r}") from exc
    if yuan < 0:
        raise ParseError(f"price is negative: {raw!r}")
    return round(yuan * 100)


def parse_timestamp(raw: str | int | float | None) -> datetime | None:
    """Upstream epoch millis / epoch seconds / ISO string -> aware UTC.

    Returns None rather than raising: a missing publish time degrades a filter
    (see the missing-field policy) but must not drop the listing.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, int | float) or (isinstance(raw, str) and raw.isdigit()):
        n = float(raw)
        # 1e12 sits between "seconds until year 33658" and "millis since 2001",
        # which is the only ambiguity that occurs in practice.
        if n > 1e11:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_item(payload: dict[str, Any], source: str) -> RawItem:
    """Upstream item dict -> RawItem. Raises ParseError only for the fields
    without which the row is meaningless: id, title, price, seller id.
    """
    missing: list[str] = []

    # Fields that must be a single value: a candidate holding a list or dict is
    # a differently-shaped payload, not this field.
    _SCALAR = {
        "item_id",
        "title",
        "price",
        "description",
        "seller_id",
        "want_count",
        "view_count",
        "publish_time",
        "region",
        "cover_url",
    }

    def get(field_name: str) -> Any:
        value = pick(payload, ITEM_FIELD_MAP[field_name], scalar_only=field_name in _SCALAR)
        if value is None:
            missing.append(field_name)
        return value

    item_id = get("item_id")
    title = get("title")
    price = get("price")
    seller_id = get("seller_id")
    if item_id is None or title is None or price is None or seller_id is None:
        raise ParseError(f"item payload lacks required fields: {sorted(missing)}")

    cover = get("cover_url")
    images = get("image_urls") or []
    if isinstance(images, str):
        images = [images]
    # A listing with only a cover photo still has a photo: without this, the
    # LLM item advice would report "no images analysed" while a picture was
    # sitting right there in cover_url.
    if not images and cover:
        images = [cover]
        # The key really was absent, but we filled it — do not report it as a
        # stale-field-map symptom, or capture_fixture raises a false alarm.
        missing = [m for m in missing if m != "image_urls"]
    images = list(dict.fromkeys(str(u) for u in images))[: settings.max_image_urls]

    want_raw = get("want_count")
    return RawItem(
        item_id=str(item_id),
        title=str(title),
        price_cents=parse_price_cents(price),
        seller_id=str(seller_id),
        seller_nick=str(get("seller_nick") or ""),
        source=source,
        description=(lambda d: str(d) if d is not None else None)(get("description")),
        cover_url=str(cover) if cover is not None else None,
        image_urls=tuple(images),
        region=(lambda r: str(r) if r is not None else None)(get("region")),
        seller_avatar_url=(lambda a: str(a) if a is not None else None)(get("seller_avatar_url")),
        publish_time=parse_timestamp(get("publish_time")),
        want_count=_as_count(want_raw),
        view_count=_as_count(get("view_count")),
        status=str(payload.get("status") or "on_sale"),
        condition_fact=payload.get("condition_fact"),
        free_shipping_fact=payload.get("free_shipping_fact"),
        seller_is_shop=payload.get("seller_is_shop"),
        seller_review_count=payload.get("seller_review_count"),
        seller_positive_rate=payload.get("seller_positive_rate"),
        publish_hint=payload.get("publish_hint"),
        missing_fields=tuple(sorted(set(missing))),
    )


def _as_count(raw: Any) -> int | None:
    """Counts arrive as "3人想要" or "" as often as as an int."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    digits = "".join(c for c in str(raw) if c.isdigit())
    return int(digits) if digits else None


def normalize_seller(payload: dict[str, Any], seller_id: str, source: str) -> RawSeller:
    """Upstream seller dict -> RawSeller. Never raises: an unavailable profile
    is a legitimate state (guest mode cannot see most of it).
    """
    missing: list[str] = []

    def get(field_name: str) -> Any:
        value = pick(payload, SELLER_FIELD_MAP[field_name])
        if value is None:
            missing.append(field_name)
        return value

    def as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def as_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return bool(value)
        return str(value).lower() in ("true", "1", "yes", "y")

    def as_float(value: Any) -> float | None:
        try:
            return float(str(value).rstrip("%")) if value is not None else None
        except (TypeError, ValueError):
            return None

    return RawSeller(
        seller_id=seller_id,
        nick=str(get("nick") or ""),
        source=source,
        avatar_url=(lambda a: str(a) if a is not None else None)(get("avatar_url")),
        is_shop=as_bool(get("is_shop")),
        credit_level=as_int(get("credit_level")),
        verified=as_bool(get("verified")),
        sold_count=as_int(get("sold_count")),
        reply_rate=as_float(get("reply_rate")),
        review_count=as_int(get("review_count")),
        positive_rate=as_float(get("positive_rate")),
        listing_count=as_int(get("listing_count")),
        account_age_days=as_int(get("account_age_days")),
        missing_fields=tuple(sorted(set(missing))),
    )
