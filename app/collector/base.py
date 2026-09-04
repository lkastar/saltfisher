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
    # Field names that were expected but absent in the payload. Feeds the
    # "collector field map may be stale" warning rather than dying silently.
    missing_fields: tuple[str, ...] = ()


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
    credit_score: int | None = None
    verified: bool | None = None
    sold_count: int | None = None
    reply_rate: float | None = None
    account_age_days: int | None = None
    missing_fields: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #
# Upstream key names are NOT verified: every probe from the development
# machine was blocked by risk control before a result list was ever returned
# (see research/mtop-access-probe.md). Candidates are listed per field so the
# first successful run on an unblocked network can be corrected in ONE place.
# Run `uv run python -m scripts.capture_fixture` there to dump a real payload.

ITEM_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "item_id": ("itemId", "id", "item_id"),
    "title": ("title", "name", "itemTitle"),
    "price": ("price", "soldPrice", "currentPrice", "priceText"),
    "description": ("desc", "description", "content"),
    "cover_url": ("picUrl", "imageUrl", "mainPic", "cover"),
    "image_urls": ("images", "picUrls", "imageUrls"),
    "region": ("area", "city", "region", "userNickArea"),
    "seller_id": ("userId", "sellerId", "user_id"),
    "seller_nick": ("userNick", "nick", "sellerNick"),
    "seller_avatar_url": ("userAvatar", "avatar", "portrait"),
    "publish_time": ("publishTime", "gmtCreate", "createTime"),
    "want_count": ("wantCnt", "wantCount", "collectCount"),
    "view_count": ("browseCnt", "viewCount", "pv"),
}

SELLER_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "nick": ("nick", "userNick", "displayName"),
    "avatar_url": ("avatar", "portrait", "userAvatar"),
    "is_shop": ("isShop", "shopFlag", "idleShop"),
    "credit_level": ("creditLevel", "level", "sesameLevel"),
    "credit_score": ("creditScore", "score", "sesameScore"),
    "verified": ("realNameVerified", "verified", "certified"),
    "sold_count": ("soldCount", "sellCount", "dealCount"),
    "reply_rate": ("replyRate", "responseRate"),
    "account_age_days": ("accountAgeDays", "registerDays"),
}


def pick(payload: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """First candidate key present with a non-empty value, else None.

    Nested payloads are flattened by the caller; this stays deliberately dumb
    so a wrong guess shows up as a missing field rather than a wrong value.
    """
    for key in candidates:
        value = payload.get(key)
        if value not in (None, "", [], {}):
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

    def get(field_name: str) -> Any:
        value = pick(payload, ITEM_FIELD_MAP[field_name])
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
    images = list(dict.fromkeys(str(u) for u in images))[: settings.max_image_urls]

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
        want_count=(lambda w: int(w) if w is not None else None)(get("want_count")),
        view_count=(lambda v: int(v) if v is not None else None)(get("view_count")),
        missing_fields=tuple(sorted(set(missing))),
    )


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
        credit_score=as_int(get("credit_score")),
        verified=as_bool(get("verified")),
        sold_count=as_int(get("sold_count")),
        reply_rate=as_float(get("reply_rate")),
        account_age_days=as_int(get("account_age_days")),
        missing_fields=tuple(sorted(set(missing))),
    )
