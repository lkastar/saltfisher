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
    # Omitted means "leave the channels alone"; a list REPLACES the set, and
    # an empty list is a real instruction to detach everything. Without this
    # field a rule's channels were frozen at creation, and since the panel
    # created every rule with none, no rule could ever notify anyone.
    channel_ids: list[int] | None = None


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
    # So the edit form can show what is currently attached rather than
    # guessing, and so "this rule notifies nobody" is visible in the list.
    channel_ids: list[int] = []


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


# --------------------------------------------------------------------------- #
# Items and price history
# --------------------------------------------------------------------------- #


class ItemPublic(SQLModel):
    """One collected listing, shared by the list and the detail endpoint.

    Seller fields are all optional and mean "not fetched", NOT zero. In this
    product 0 reviews is a danger signal and unknown is merely missing data;
    collapsing them would turn "we could not check" into "we checked and it is
    bad".

    Hit fields are populated only when the request named a monitor_id, because
    `unverified_filters` is a property of one rule's match, not of the item.
    """

    id: str
    title: str
    description: str | None
    cover_url: str | None
    image_urls: list[str]
    region: str | None
    status: str
    price_cents: int
    publish_time: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime

    seller_id: str
    seller_nick: str
    seller_avatar_url: str | None
    seller_is_shop: bool | None
    seller_credit_level: int | None
    seller_credit_score: int | None
    seller_review_count: int | None
    seller_positive_rate: float | None
    seller_sold_count: int | None
    seller_verified: bool | None

    first_hit_at: datetime | None = None
    notified_at: datetime | None = None
    in_range: bool | None = None
    unverified_filters: list[str] | None = None


class PricePoint(SQLModel):
    """One observation. The series is ascending by time so a step chart can
    draw it directly: the price held until the next point, which is why the
    chart is a step line and never a smoothed curve.
    """

    price_cents: int
    status: str
    captured_at: datetime
    source: str
    want_count: int | None
    view_count: int | None


# --------------------------------------------------------------------------- #
# Upstream session
# --------------------------------------------------------------------------- #


class CookieImport(SQLModel):
    """A paste from the browser's devtools.

    A raw Cookie header rather than a structured list: that is the one form a
    user can actually produce without tooling, and asking for JSON would mean
    they hand-edit it and get it wrong.
    """

    cookie_header: str = Field(min_length=1)
    origin: str = "https://www.goofish.com"


class SessionState(SQLModel):
    """Session health. Carries cookie NAMES and never values."""

    origin: str | None
    usable: bool
    needs_verification: bool
    established_at: datetime | None
    last_error: str | None
    challenged_apis: list[str]
    cookie_names: list[str]
    # `usable` only means a token is present. `proven` means a call has
    # actually succeeded since the credentials were imported -- without the
    # distinction the page reports a green session for cookies whose very
    # next request fails.
    proven: bool
    last_success_at: datetime | None


# --------------------------------------------------------------------------- #
# Market analytics (M2)
# --------------------------------------------------------------------------- #


class Sample(SQLModel):
    """How much data is behind a chart, on every analytics response.

    Not optional metadata: this tool starts with an empty database and grows
    its history one poll at a time, so a chart has to be able to say "33
    listings over 4 days" instead of drawing a confident line over nothing.
    `data_days` is what the panel's "collecting for N days" reads.
    """

    sample_size: int
    data_days: int
    window_days: int


class PriceBucket(SQLModel):
    """One histogram column. Edges are whole yuan in cents."""

    lo_cents: int
    hi_cents: int
    count: int


class PriceQuantiles(SQLModel):
    """Empty below two samples: statistics.quantiles needs two data points,
    and a one-listing keyword is an ordinary day-one state here."""

    p10: int | None = None
    p25: int | None = None
    p50: int | None = None
    p75: int | None = None
    p90: int | None = None


class PriceDistribution(Sample):
    """`fresh_size` is separate from `sample_size` on purpose: the first says
    how much the distribution covers, the second how much of it is still on
    sale right now. With only the first, four-day-old asking prices read as
    the current market."""

    quantiles: PriceQuantiles
    histogram: list[PriceBucket]
    fresh_size: int


class PriceDrop(SQLModel):
    """`drop_bps` is integer basis points -- 2500 is 25.00%.

    `is_fresh` false means the listing has left our observation window or was
    seen gone; the UI must say so rather than link to a dead page.
    """

    item_id: str
    title: str
    cover_url: str | None
    then_cents: int
    now_cents: int
    drop_bps: int
    last_seen_at: datetime | None
    is_fresh: bool


class PriceDrops(Sample):
    rows: list[PriceDrop]


class SupplyDay(SQLModel):
    """`collected` false means we have no record of a successful cycle that
    day, which is a different fact from `new_count == 0`. Rendering both as an
    empty bar is how a chart reports a dead market during downtime."""

    date: str
    new_count: int
    collected: bool
    runs_ok: int
    runs_failed: int


class SupplyTrend(Sample):
    days: list[SupplyDay]
