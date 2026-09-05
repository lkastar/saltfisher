"""Request and response models.

These are non-table SQLModel classes, and they exist because SQLModel SKIPS
pydantic validation on `table=True` models: `Field(ge=60)` on Monitor does not
reject a 30-second interval. The storage layer has a CHECK constraint for that,
but a constraint violation surfaces as a 500, not a readable 422. Validation
belongs here.
"""

from datetime import datetime
from typing import Any, Literal

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


# --------------------------------------------------------------------------- #
# Market analytics (P3)
# --------------------------------------------------------------------------- #


class DurationQuantiles(SQLModel):
    """Minutes. Empty below two samples, for the same reason PriceQuantiles is."""

    p10: int | None = None
    p25: int | None = None
    p50: int | None = None
    p75: int | None = None
    p90: int | None = None


class DurationBucket(SQLModel):
    """One histogram column, in minutes, on readable step boundaries.

    The step adapts to the spread (`analytics.duration_align`): 5, 10, 15, 30
    minutes, then hours. Hour-aligned edges were the original plan and they
    collapsed the real data into two bars, because the median listing stays in
    range for 1 to 9 minutes.
    """

    lo_minutes: int
    hi_minutes: int
    count: int


class ListingDuration(Sample):
    """How long listings stayed inside our observation range.

    Not a time to sale, and the field names must never start claiming it is: a
    listing stops coming back because it was bought, because it was delisted,
    or because its rank fell past the pages we read — and the last of those is
    our own doing.

    Which is why the aperture is part of the response and not a footnote.
    `aperture_pages_min` / `max` are the narrowest and widest page counts any
    cycle for this keyword actually fetched inside the window, with rows
    written before paging existed counted as the single page they were.
    **When the two differ, the aperture changed mid-window and the
    distribution is not internally comparable** — a wider aperture makes a
    listing "leave" later, so the shape is partly a measurement of us. Both
    are null when no cycle in the window recorded an aperture, which is not
    the same as one page.

    Durations are integer minutes for the same reason prices are integer
    cents. Histogram edges land on a readable step chosen from the spread,
    not on a fixed hour: at this poll interval most durations are minutes, and
    hour buckets showed 264 samples as a single bar.
    """

    quantiles: DurationQuantiles
    histogram: list[DurationBucket]
    aperture_pages_min: int | None = None
    aperture_pages_max: int | None = None
    aperture_rows: int
    # Samples still timed by the old GLOBAL clock, i.e. ledger rows written
    # before MonitorHit.last_hit_at existed. Non-zero means the distribution
    # mixes a per-keyword measurement with a global one: for those rows a
    # listing another rule still sees keeps a fresh timestamp, so they under-
    # report. Cannot be backfilled, so it shrinks on its own and the page
    # says so while it is non-zero.
    legacy_clock_rows: int = 0


# --------------------------------------------------------------------------- #
# LLM configuration (P4)
# --------------------------------------------------------------------------- #

# Literal rather than str, for the reason `database-guidelines.md` gives: a
# bad value becomes a 422 instead of reaching `client.adapter_for` /
# `prompts.default_prompt` and surfacing as a 500, and the values land in
# OpenAPI as an enum so the generated frontend types are a union instead of a
# second source of truth for what a wire format or a scenario is.
WireFormat = Literal["openai", "anthropic"]
Scenario = Literal["market", "item"]


class LlmEndpointCreate(SQLModel):
    """`api_key` is accepted here and appears on NO response model.

    Not even masked, not even as a length. The channel pages redact a stored
    secret on the way out and then have to defend against the redacted value
    being PATCHed back over the real one (`api/channels.py`); a key that is
    never echoed has no such round trip to get wrong.

    Empty is allowed: a local Ollama or a self-hosted vLLM needs no key, and
    demanding a placeholder there would teach users to type one.
    """

    label: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    wire_format: WireFormat = "openai"


class LlmEndpointUpdate(SQLModel):
    """Absent `api_key` leaves the stored one alone; `""` clears it.

    The distinction is why this is `str | None` and not `str`: the config page
    submits the form without a key whenever the user did not retype it.
    """

    label: str | None = Field(default=None, min_length=1, max_length=60)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    wire_format: WireFormat | None = None


class LlmEndpointPublic(SQLModel):
    """`api_key_configured` is the entire answer the page gets about the key."""

    id: int
    label: str
    base_url: str
    wire_format: str
    api_key_configured: bool
    created_at: datetime


class LlmModelList(SQLModel):
    """An empty list plus a reason is a NORMAL answer, not a failure.

    Plenty of relay gateways never implement the models route, so the useful
    response to a failed lookup is one the page can render beside a text input
    for typing the model name — a 4xx would read as "this endpoint is broken"
    and stop a user whose endpoint works fine.
    """

    models: list[str]
    error: str | None = None


class LlmTestResult(SQLModel):
    """`text` is the model's own answer, which is what makes the button mean
    something: a reachable endpoint that returns nothing visible is a distinct
    outcome and it comes back as `ok: false` with its own message."""

    ok: bool
    error: str | None = None
    text: str = ""


class LlmScenarioPublic(SQLModel):
    """`prompt_template` null means the built-in default is in use."""

    scenario: str
    endpoint_id: int | None
    model: str | None
    prompt_template: str | None
    send_images: bool
    # Null means the built-in default. Exposed because the starved answer's
    # own advice is "raise max_tokens", and an error that names a knob the
    # product does not offer is not actionable.
    max_tokens: int | None = None
    # Load-bearing placeholders this template dropped. Non-empty means the
    # rendered prompt would carry no data while still telling the model to
    # cite specific numbers, so the analyze route refuses rather than billing
    # for a fabrication. Reported here so the warning reaches the user on the
    # config page, before they click and wait 150 seconds for a refusal.
    missing_placeholders: list[str] = []
    enabled: bool


class LlmScenarioUpdate(SQLModel):
    """The whole scenario config, PUT as one object.

    `prompt_template` is free text and is NOT checked against the placeholder
    contract. `prompts.render` substitutes with `str.replace`, so an unknown
    or typo'd `{plcaeholder}` survives into the prompt as visible text by
    design — the model sees a stray brace pair and the answer degrades, which
    is a far better failure than a 422 on a field a user is mid-edit in. The
    contract is published by `GET /scenarios/{scenario}/default-prompt` so the
    page can list it and offer the default back.
    """

    endpoint_id: int | None = None
    model: str | None = Field(default=None, max_length=200)
    prompt_template: str | None = Field(default=None, max_length=20_000)
    send_images: bool = False
    # Bounded rather than free: measured, 4096 starves the market prompt and
    # 16384 answers it, so the floor keeps a user from configuring the failure
    # the message just told them to fix.
    max_tokens: int | None = Field(default=None, ge=1024, le=65_536)
    enabled: bool = False


class LlmDefaultPrompt(SQLModel):
    """The built-in template plus the placeholders it is allowed to use."""

    scenario: str
    prompt_template: str
    placeholders: list[str]


# --------------------------------------------------------------------------- #
# LLM market analysis (P4/T3)
# --------------------------------------------------------------------------- #


class LlmMarketAnalyze(SQLModel):
    """What the market panel asks for: a keyword and the window it is showing.

    `days` bounds all four metrics at once, unlike the analytics routes where
    it means something different in each. The prompt says "最近 N 天" over a
    single block of numbers, and mixing windows inside it would be a lie to
    the model about figures it has no way to check. Default 30 matches the
    analytics page's window selector, which is where the button lives.
    """

    keyword: str = Field(min_length=1, max_length=200)
    days: int = Field(default=30, ge=1, le=365)


class LlmMarketAnalysis(SQLModel):
    """One market reading, in the four states the UI has to render.

    `kind` carries the three HTTP-200 failure modes of design.md §4 apart, and
    they are NOT interchangeable: `starved` is fixed by raising max_tokens and
    `unparsable` by fixing the template, so collapsing them tells one of the
    two users to go and edit the thing that was never wrong. `text` is the
    model's own output, kept so the `unparsable` state can show it verbatim.

    `no_data` is this layer's own fifth state: the keyword had no priced
    listing in the window, so no call was made at all. A billed call whose
    honest answer is "there is nothing here" is a call worth not making.

    `reading` is a validated `app.llm.market.MarketReading` as a dict.
    Typed as `dict` on purpose — the JSON field names are already stated twice
    (in `prompts.MARKET_PROMPT` and in that model) and a third copy here would
    be a third place to drift, while the frontend types are hand-written in
    `web/src/api/types.ts` either way. `test_llm_market.py` pins the two that
    do exist against each other.

    `disclaimer` has no default: it is required by FR-P4-3 and a default is a
    thing a later route can quietly stop passing.
    """

    kind: Literal["ok", "starved", "empty", "unparsable", "no_data"]
    keyword: str
    window_days: int
    data_days: int
    sample_size: int
    disclaimer: str
    reading: dict | None = None
    text: str = ""
    message: str | None = None
    # True means no endpoint call was billed for this response (FR-P4-3).
    cached: bool = False
    # How many endpoint calls this answer cost. Not derivable from `cached`:
    # a first draw that fails validation is retried once, so one click can
    # bill twice, and `cached: false` alone cannot tell that apart from a
    # single call. Measured live: 100s + 52s for one market click.
    calls: int = 0


# --------------------------------------------------------------------------- #
# LLM item advice (P4-T4)
# --------------------------------------------------------------------------- #


class LlmItemAnalysis(SQLModel):
    """One single-item advice call, in the four states the panel renders.

    `kind` carries `LlmOutcome`'s four values unchanged rather than collapsing
    them into ok/error: `starved` is fixed by raising max_tokens, `empty` by
    checking the model id, and `unparsable` by fixing the template — and
    `unparsable` is the one that must still show `text`, because a user
    breaking their own prompt is a normal event with the model's own words as
    the only useful thing to look at.

    `keyword` is which market the listing was compared against, echoed because
    the choice is not obvious: an item can sit in two keywords' ledgers at
    different price levels and this one is "the rule that saw it first"
    (design §5). Null means it is in no ledger at all, and the advice was
    written without any market statistics.

    `notes` are the inputs that did NOT make it in — a photo the CDN answered
    with a 1x1 pixel, an oversized one, a text-only degradation. An answer
    that silently dropped the pictures is indistinguishable from one that read
    them, which is why this is a field and not a log line.
    """

    kind: Literal["ok", "starved", "empty", "unparsable"]
    text: str
    data: dict[str, Any] | None = None
    # Endpoint calls billed. Two when the first draw failed validation and was
    # retried; this scenario is never cached, so it is always at least one.
    calls: int = 1
    message: str | None = None
    keyword: str | None = None
    notes: list[str]
    images_sent: int
    disclaimer: str
