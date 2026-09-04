"""All table models.

Invariants that the rest of the codebase depends on:
  - money is int cents, named *_cents. Never float.
  - datetimes are UTC-aware, produced by utcnow() below. Never datetime.utcnow().
  - upstream ids (item, seller) are str: goofish ids exceed 32-bit range and
    appear zero-padded in some payloads.
  - seller profile fields are all Optional; None means "not fetched", which is
    a different thing from 0.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, types
from sqlmodel import Field, SQLModel

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(types.TypeDecorator[datetime]):
    """Keeps the "all datetimes are UTC-aware" invariant actually true.

    SQLite has no timezone-aware column type: it stores whatever it is given
    and hands back naive datetimes. Comparing one of those with utcnow()
    raises TypeError, which would surface as a crash in the price-drop and
    interval arithmetic rather than anywhere near the cause. Coercing in both
    directions here makes it impossible to get wrong at the call sites.
    """

    impl = types.DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value  # already naive UTC by convention
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


# The 60s floor is an anti-ban rule, so it is enforced in storage: SQLModel
# skips Pydantic validation on table=True models, which means Field(ge=...)
# alone would silently accept a 5-second interval written by internal code.
# Request-level validation lands with the non-table request schemas in T3.
_INTERVAL_CHECK = CheckConstraint(
    f"interval_seconds >= {settings.min_interval_seconds}", name="interval_floor"
)


# --------------------------------------------------------------------------- #
# Monitoring rules
# --------------------------------------------------------------------------- #


class Monitor(SQLModel, table=True):
    __table_args__ = (_INTERVAL_CHECK,)

    id: int | None = Field(default=None, primary_key=True)
    name: str
    keyword: str

    # Filters. Anything the upstream API cannot express is applied locally;
    # see .trellis/spec/backend/collector-guidelines.md for the split.
    exclude_words: str = ""
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    published_within_hours: int | None = None
    region: str | None = None
    condition: str | None = None
    free_shipping: bool | None = None
    min_seller_credit: int | None = None
    exclude_shop: bool = False

    interval_seconds: int = Field(default=300, ge=settings.min_interval_seconds)
    enabled: bool = True

    # False until the first cycle has recorded its baseline. Flipped inside the
    # same transaction that writes the baseline hits, so a crash mid-baseline
    # does not turn into a full-volume push next cycle.
    baseline_done: bool = False

    last_run_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    last_error: str | None = None
    last_collector: str | None = None
    consecutive_failures: int = 0
    hit_count: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


# --------------------------------------------------------------------------- #
# Listings
# --------------------------------------------------------------------------- #


class Seller(SQLModel, table=True):
    id: str = Field(primary_key=True)
    nick: str
    avatar_url: str | None = None

    # Profile fields. fetched_at is None on rows created as a by-product of
    # collecting an item, before the seller page has ever been fetched.
    is_shop: bool | None = None
    credit_level: int | None = None
    credit_score: int | None = None
    verified: bool | None = None
    sold_count: int | None = None
    reply_rate: float | None = None
    # Available free in every search row via userFishShopLabel; see
    # collector/mtop.py _seller_reputation.
    review_count: int | None = None
    positive_rate: float | None = None
    account_age_days: int | None = None
    fetched_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    fetch_error: str | None = None


class Item(SQLModel, table=True):
    id: str = Field(primary_key=True)
    title: str
    description: str | None = None

    # Display fields. Images are stored as URLs only — the goofish CDN is the
    # image host; downloading thousands of photos would blow up data/.
    cover_url: str | None = None
    image_urls: str | None = None  # JSON array, capped at settings.max_image_urls
    region: str | None = None

    # seller_nick is denormalised on purpose: search results carry it, while
    # the Seller profile needs an extra request. Lists must render before the
    # profile has been fetched.
    seller_id: str = Field(index=True, foreign_key="seller.id")
    seller_nick: str
    seller_avatar_url: str | None = None

    publish_time: datetime | None = Field(default=None, sa_type=UtcDateTime)
    first_seen_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    status: str = "on_sale"  # on_sale | sold | removed


class PriceSnapshot(SQLModel, table=True):
    """Append-only price history.

    Written only when price or status changes; Item.last_seen_at is refreshed
    every cycle instead. That keeps the full price history while cutting row
    volume by 2-3 orders of magnitude, and still makes listing duration exact.
    """

    # Composite index covers the item_id prefix, so a separate single-column
    # index on it would only add write cost to this append-hot table.
    __table_args__ = (Index("ix_snapshot_item_time", "item_id", "captured_at"),)

    id: int | None = Field(default=None, primary_key=True)
    item_id: str = Field(foreign_key="item.id")
    price_cents: int
    want_count: int | None = None
    view_count: int | None = None
    status: str
    captured_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    source: str  # mtop | browser | detail


class MonitorHit(SQLModel, table=True):
    """Dedup and notification ledger — the single source of truth for
    "has this rule already told the user about this item?".

    in_range implements the "price entered the range" semantics: deduping on
    "have we seen this item" alone would permanently miss an item that was
    over budget and then dropped into range, which is the most common way a
    deal actually appears.
    """

    __table_args__ = (Index("ix_hit_monitor_time", "monitor_id", "first_hit_at"),)

    monitor_id: int = Field(foreign_key="monitor.id", primary_key=True)
    item_id: str = Field(foreign_key="item.id", primary_key=True)
    first_hit_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    notified_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    notified_price_cents: int | None = None
    in_range: bool = True
    unverified_filters: str | None = None  # JSON array of filter names


class Watchlist(SQLModel, table=True):
    """Manually watched items. Fully decoupled from Monitor: deleting a rule or
    changing its keyword must not stop tracking a watched item.
    """

    __table_args__ = (_INTERVAL_CHECK,)

    item_id: str = Field(primary_key=True, foreign_key="item.id")
    added_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    added_price_cents: int
    note: str | None = None  # doubles as {user_intent} for the LLM item advice
    price_watch_enabled: bool = True
    interval_seconds: int = Field(default=300, ge=settings.min_interval_seconds)
    last_run_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    last_error: str | None = None
    notified_price_cents: int | None = None
    consecutive_failures: int = 0


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #


class NotifyChannel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str  # email | telegram
    label: str
    config: str  # JSON; holds secrets, never returned by the API
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class MonitorChannel(SQLModel, table=True):
    monitor_id: int = Field(foreign_key="monitor.id", primary_key=True)
    channel_id: int = Field(foreign_key="notifychannel.id", primary_key=True)


class NotifyLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="notifychannel.id", index=True)
    kind: str  # new_in_range | price_drop | gone | test
    monitor_id: int | None = None
    item_count: int = 0
    ok: bool
    error: str | None = None
    sent_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)


# --------------------------------------------------------------------------- #
# LLM (M4) — table now so the config surface exists from the start
# --------------------------------------------------------------------------- #


class LlmEndpoint(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    base_url: str
    api_key: str  # write-only: the API reports whether it is set, never its value
    wire_format: str  # openai | anthropic
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class LlmScenarioConfig(SQLModel, table=True):
    scenario: str = Field(primary_key=True)  # market | item
    endpoint_id: int | None = Field(default=None, foreign_key="llmendpoint.id")
    model: str | None = None
    prompt_template: str | None = None  # None = use the built-in default
    send_images: bool = False
    enabled: bool = False
