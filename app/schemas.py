"""Request and response models.

These are non-table SQLModel classes, and they exist because SQLModel SKIPS
pydantic validation on `table=True` models: `Field(ge=60)` on Monitor does not
reject a 30-second interval. The storage layer has a CHECK constraint for that,
but a constraint violation surfaces as a 500, not a readable 422. Validation
belongs here.
"""

from datetime import datetime
from typing import Literal

from pydantic import model_validator
from sqlmodel import Field, SQLModel

from app.config import settings


class MonitorBase(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=100)
    exclude_words: str = ""
    price_min_cents: int | None = Field(default=None, ge=0)
    price_max_cents: int | None = Field(default=None, ge=0)
    published_within_hours: int | None = Field(default=None, ge=1)
    region: str | None = None
    condition: str | None = None
    free_shipping: bool | None = None
    min_seller_credit: int | None = Field(default=None, ge=0)
    exclude_shop: bool = False
    # The 60s floor is an anti-ban rule, not a UI hint. See
    # .trellis/spec/backend/collector-guidelines.md.
    interval_seconds: int = Field(default=300, ge=settings.min_interval_seconds)

    @model_validator(mode="after")
    def _price_range_must_be_ordered(self) -> "MonitorBase":
        lo, hi = self.price_min_cents, self.price_max_cents
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("price_min_cents must not exceed price_max_cents")
        return self


class MonitorCreate(MonitorBase):
    channel_ids: list[int] = []


class MonitorUpdate(SQLModel):
    """Every field optional: PATCH semantics. Validation of the ones that are
    present still applies.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    keyword: str | None = Field(default=None, min_length=1, max_length=100)
    exclude_words: str | None = None
    price_min_cents: int | None = Field(default=None, ge=0)
    price_max_cents: int | None = Field(default=None, ge=0)
    published_within_hours: int | None = Field(default=None, ge=1)
    region: str | None = None
    condition: str | None = None
    free_shipping: bool | None = None
    min_seller_credit: int | None = Field(default=None, ge=0)
    exclude_shop: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=settings.min_interval_seconds)
    enabled: bool | None = None


class MonitorPublic(MonitorBase):
    """What the management page renders.

    The health fields are not decoration: a monitoring tool whose failures are
    invisible is worse than one that stopped. See design.md section 12.
    """

    id: int
    enabled: bool
    baseline_done: bool
    last_run_at: datetime | None
    last_error: str | None
    last_collector: str | None
    consecutive_failures: int
    hit_count: int


class CycleResult(SQLModel):
    """Returned by run-now so the user sees what a rule actually does."""

    collected: int
    passed: int
    notifiable: int
    collector: str | None = None
    error: str | None = None
    baseline: bool = False


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #


class ChannelCreate(SQLModel):
    # Literal rather than a validated string: it surfaces in the OpenAPI
    # schema as an enum, so the generated frontend types give a union and the
    # form can render a select without a second source of truth.
    kind: Literal["email", "telegram"]
    label: str = Field(min_length=1, max_length=60)
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class ChannelUpdate(SQLModel):
    label: str | None = Field(default=None, min_length=1, max_length=60)
    config: dict | None = None
    enabled: bool | None = None


class ChannelPublic(SQLModel):
    """Secrets are replaced with a presence marker before this leaves the app.

    The page needs to know WHETHER a token is set, never what it is.
    """

    id: int
    kind: str
    label: str
    config: dict
    enabled: bool
    created_at: datetime


class NotifyLogPublic(SQLModel):
    id: int
    channel_id: int
    kind: str
    monitor_id: int | None
    item_count: int
    ok: bool
    error: str | None
    sent_at: datetime


class TestSendResult(SQLModel):
    ok: bool
    error: str | None = None


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


class WatchlistCreate(SQLModel):
    """Two ways in: an item already in the database, or a pasted link.

    The link path matters — without it the tool is closed inside its own search
    results, and the specific thing the user actually wants to buy (spotted in
    the app, or sent by a friend) cannot be watched at all.
    """

    item_id: str | None = None
    url: str | None = None
    note: str | None = Field(default=None, max_length=500)
    interval_seconds: int = Field(default=300, ge=settings.min_interval_seconds)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "WatchlistCreate":
        if bool(self.item_id) == bool(self.url):
            raise ValueError("provide exactly one of item_id or url")
        return self


class WatchlistUpdate(SQLModel):
    note: str | None = Field(default=None, max_length=500)
    price_watch_enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=settings.min_interval_seconds)


class WatchlistPublic(SQLModel):
    item_id: str
    title: str
    price_cents: int
    added_price_cents: int
    change_cents: int
    change_ratio: float
    status: str
    cover_url: str | None
    seller_nick: str
    seller_is_shop: bool | None
    seller_credit_level: int | None
    seller_positive_rate: float | None
    first_seen_at: datetime
    last_seen_at: datetime
    listed_days: float
    note: str | None
    price_watch_enabled: bool
    interval_seconds: int
    last_run_at: datetime | None
    last_error: str | None
    added_at: datetime
