"""The seller-listing path: `mtop.idle.web.xyh.item.list` -> RawItem.

Every upstream fact pinned here was measured on 2026-09-08 by watching what a
real seller page sends (see the seller-listing probe note), not guessed. Three
of them cost a live request each and are the reason this file exists:

1. **`totalCount` lies.** The measured response said `totalCount: 0` while
   `cardList` held 4 real listings and the page itself displayed 「在售 4」. An
   implementation that pages on the obvious field collects nothing and reports
   no error at all.
2. **The card carries no seller id**, so the caller injects it — and it has to
   be the opaque canonical `Seller.id`, not the numeric userId this API takes
   as input. The numeric form there creates a second seller row for one
   person, which is the bug `8f10a77` removed.
3. **There is no browser route for a seller page.** The browser can still
   ESTABLISH a session (that is how a keyword rule survives an expired one for
   free), so an unusable session gets established first — but it can never
   collect the listings, and a session that cannot be established has to ask
   for a human rather than back off.
"""

import json
import logging
import pathlib
import time

import httpx
import pytest

from app.collector import base, filters
from app.collector.base import (
    ChallengeError,
    ParseError,
    RawItem,
    SearchResult,
    TransientCollectorError,
)
from app.collector.filters import RuleFilters
from app.collector.mtop import (
    SELLER_LISTING_API,
    MtopClient,
    flatten_seller_card,
)
from app.collector.pipeline import Pipeline
from app.collector.session import UpstreamSession

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

OPAQUE = "6uSUGlb2aPN1F6kZNEnN9Q=="
NUMERIC = "2218219939144"


@pytest.fixture(scope="module")
def payload() -> dict:
    body = json.loads((FIXTURES / "seller_listing_synthetic.json").read_text())
    assert body["data"]["totalCount"] == 0, "the fixture no longer exercises the trap"
    assert len(body["data"]["cardList"]) == 4
    return body


def cards(payload: dict) -> list[dict]:
    return [c["cardData"] for c in payload["data"]["cardList"]]


def normalize(card: dict) -> RawItem:
    return base.normalize_seller_listing(
        flatten_seller_card(card), seller_id=OPAQUE, seller_nick="小顾数码", source="mtop"
    )


# --------------------------------------------------------------------------- #
# The card shape
# --------------------------------------------------------------------------- #


def test_the_item_id_comes_from_detail_params(payload):
    """`cardData.id` was recorded as a key and never as a value we understand,
    so it is not an item-id candidate at all. The fixture gives it a different
    value on purpose — with the two equal, reading the wrong one would pass.
    """
    card = cards(payload)[0]
    assert card["id"] != card["detailParams"]["itemId"], "fixture stopped proving anything"
    assert "id" not in flatten_seller_card(card)
    assert normalize(card).item_id == "1080376910394"


def test_the_price_is_read_from_either_place_the_card_puts_it(payload):
    """Both keys were observed and neither was shown to lie (unlike search's
    `oriPrice`), so this pins the shapes rather than a preference.
    """
    first, _, third, _ = cards(payload)
    assert normalize(first).price_cents == 75000  # detailParams.soldPrice "750"
    # No detailParams.soldPrice on this one: priceInfo.price answers instead.
    assert "soldPrice" not in flatten_seller_card(third)
    assert normalize(third).price_cents == 8900


def test_a_thousands_separator_is_not_a_new_price_parser(payload):
    """`base.parse_price_cents` already handles every shape upstream uses;
    a second parse here would be the duplicate the guidelines forbid."""
    assert normalize(cards(payload)[1]).price_cents == 245000


def test_the_seller_is_injected_as_the_opaque_canonical_id(payload):
    """The payload has no seller id in either space. The caller knows it — it
    is the seller this rule watches — and the OPAQUE one is what every
    Item.seller_id points at.
    """
    item = normalize(cards(payload)[0])
    assert item.seller_id == OPAQUE
    assert item.seller_id != NUMERIC
    assert item.seller_nick == "小顾数码"


def test_absent_fields_stay_none_instead_of_being_invented(payload):
    """Region, the avatar and the posting time are not in this payload at all.

    Publish time deserves the note: `postInfo` was wired as its only candidate
    on the strength of the NAME, before anyone looked at the value. Measured
    2026-09-08 on two different sellers, it is `"包邮"` — the shipping label,
    邮 as in postage. This endpoint carries no posting time whatsoever, so the
    field is None and the published-within filter waives itself.
    """
    item = normalize(cards(payload)[0])
    assert item.region is None
    assert item.seller_avatar_url is None
    assert item.publish_time is None, "postInfo is postage, not posting"
    assert item.want_count is None and item.view_count is None
    assert item.condition_fact is None, "a list page states no condition"


def test_the_shipping_label_is_read_as_shipping(payload):
    """The other half of the same mistake: while `postInfo` was being read as a
    date, the free-shipping fact it actually carries was thrown away — and
    free_shipping is a filter the rules already support.
    """
    free, negotiable, _, bare = cards(payload)
    assert normalize(free).free_shipping_fact is True
    # Positive only. A seller who does not pay postage may simply omit the
    # label, so anything else is "not stated" -- False would be a claim.
    assert normalize(negotiable).free_shipping_fact is None
    assert normalize(bare).free_shipping_fact is None


def test_the_filter_can_now_act_on_free_shipping(payload):
    """Worth pinning end to end: before this, a rule asking for 包邮 got the
    yellow waiver on every seller-rule hit."""
    item = normalize(cards(payload)[0])
    outcome = filters.apply_local(item, RuleFilters(free_shipping=True))
    assert outcome.passed
    assert "包邮未知" not in filters.describe_unverified(outcome.unverified)


def test_the_yellow_label_logic_catches_the_missing_fields(payload):
    """Unchanged behaviour, reached through the new normaliser: a filter whose
    data is unavailable waives itself and says so.
    """
    item = normalize(cards(payload)[0])
    outcome = filters.apply_local(item, RuleFilters(region="上海", published_within_hours=24))
    assert outcome.passed
    assert set(filters.describe_unverified(outcome.unverified)) == {"地区未知", "发布时间未知"}


def test_the_whole_gallery_comes_back_not_just_the_cover(payload):
    """`imageInfos` is a JSON STRING here — measured on two sellers, a list of
    `{url, major, type, widthSize, heightSize, videoCover}`. Note the detail
    endpoint uses the same key for an already-parsed list: one name, two
    shapes, which is why this has its own parser.
    """
    item = normalize(cards(payload)[0])
    assert item.cover_url == "https://cdn/xm4-1.jpg"
    assert item.image_urls == (
        "https://cdn/xm4-1.jpg",
        "https://cdn/xm4-2.jpg",
        "https://cdn/xm4-3.jpg",
    ), "the cover leads and is not repeated"


def test_a_gallery_that_does_not_parse_still_leaves_the_listing(payload, caplog):
    """Losing the photos must never cost the listing. Card 2's imageInfos is
    malformed on purpose."""
    with caplog.at_level(logging.WARNING, logger="app.collector.base"):
        item = normalize(cards(payload)[2])
    assert item.item_id, "the listing survives"
    assert item.image_urls == ("https://cdn/anker-1.jpg",), "degrades to the cover alone"
    assert any("imageInfos" in r.message for r in caplog.records)


def test_the_cover_leads_a_gallery_it_is_not_part_of(payload):
    """Card 1's cover is not in its gallery. Both belong in `image_urls`, and
    the cover first -- everything downstream treats index 0 as the thumbnail.
    """
    item = normalize(cards(payload)[1])
    assert item.image_urls == ("https://cdn/ipad-1.jpg", "https://cdn/other-1.jpg")


def test_a_card_with_no_photo_at_all_is_still_a_listing(payload):
    item = normalize(cards(payload)[3])
    assert item.cover_url is None and item.image_urls == ()
    assert "cover_url" in item.missing_fields


def test_a_card_without_a_cover_still_parses(payload):
    """picInfo is absent on the last card. A photo is optional; a price is not."""
    item = normalize(cards(payload)[3])
    assert item.cover_url is None and item.image_urls == ()
    assert "cover_url" in item.missing_fields


def test_a_card_without_a_price_is_dropped_not_priced_at_zero():
    with pytest.raises(ParseError):
        normalize({"detailParams": {"itemId": "1"}, "title": "无价"})


def test_an_unobserved_item_status_is_reported_and_never_mapped(payload, caplog):
    """`0` is the only value ever seen here, and we ask for the 在售 group.
    Another value is a finding to go and read — inventing a mapping for it is
    the mistake `detail_status` already documents about `sold`.
    """
    card = json.loads(json.dumps(cards(payload)[0]))
    card["itemStatus"] = -2
    with caplog.at_level(logging.WARNING, logger="app.collector.base"):
        item = normalize(card)
    assert item.status == "on_sale", "group membership is what was observed"
    assert any("unobserved itemStatus" in r.message for r in caplog.records)


def test_the_observed_status_is_silent(payload, caplog):
    with caplog.at_level(logging.WARNING, logger="app.collector.base"):
        normalize(cards(payload)[0])
    assert not [r for r in caplog.records if "itemStatus" in r.message]


# --------------------------------------------------------------------------- #
# The request, and the response envelope
# --------------------------------------------------------------------------- #


def transport(bodies: list[dict]):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=bodies[min(len(calls) - 1, len(bodies) - 1)])

    return httpx.MockTransport(handler), calls


def client_with(bodies: list[dict]) -> tuple[MtopClient, list[httpx.Request]]:
    session = UpstreamSession()
    session.adopt({"_m_h5_tk": "tok_1", "cookie2": "x"}, origin="imported")
    tr, calls = transport(bodies)
    client = MtopClient(session)
    client._client = httpx.AsyncClient(transport=tr)
    return client, calls


async def listings(client: MtopClient, page: int = 1) -> tuple[list[RawItem], bool]:
    return await client.seller_listings(
        NUMERIC,
        page=page,
        now_ms=str(int(time.time() * 1000)),
        seller_id=OPAQUE,
        seller_nick="小顾数码",
    )


@pytest.mark.asyncio
async def test_a_total_count_of_zero_still_collects_four_listings(payload):
    """The measured trap, end to end. `totalCount: 0` with four real cards in
    the same response: reading the field name collects nothing, silently.
    """
    client, _ = client_with([payload])
    items, next_page = await listings(client)

    assert [i.item_id for i in items] == [
        "1080376910394",
        "1080376910395",
        "1080376910396",
        "1080376910397",
    ]
    assert next_page is False


@pytest.mark.asyncio
async def test_every_collected_card_is_stamped_with_the_opaque_seller(payload):
    """The seam the collector owns: `seller_listings` is CALLED with the
    numeric id and must stamp the items with the opaque one. Passing the
    numeric id through here is what creates a second seller row for one
    person, and neither the normaliser's own test nor the scheduler's can see
    it — this is the only place both ids are in scope at once.
    """
    client, _ = client_with([payload])
    items, _ = await listings(client)
    assert {i.seller_id for i in items} == {OPAQUE}


@pytest.mark.asyncio
async def test_the_request_omits_group_id_and_uses_the_page_size_the_page_uses(payload):
    """`groupId` was measured to be droppable, which is what keeps a cycle at
    ONE request instead of first fetching the group list. `pageSize` is the
    page's own 20, not search's 30: matching the real request is the safer bet
    in front of risk control.
    """
    client, calls = client_with([payload])
    await listings(client)

    (call,) = calls
    assert call.url.params["api"] == SELLER_LISTING_API
    sent = json.loads(httpx.QueryParams(call.content.decode())["data"])
    assert sent == {
        "needGroupInfo": False,
        "pageNumber": 1,
        "userId": NUMERIC,
        "pageSize": 20,
        "groupName": "在售",
        "defaultGroup": True,
    }


@pytest.mark.asyncio
async def test_an_absent_card_list_is_a_parse_error_not_an_empty_shelf():
    """Returning [] would be written to analytics as "this seller has nothing
    on sale" — the fake zero the never-return-[] rule exists to prevent.
    """
    client, _ = client_with([{"ret": ["SUCCESS::调用成功"], "data": {"totalCount": 7}}])
    with pytest.raises(ParseError):
        await listings(client)


@pytest.mark.asyncio
async def test_a_seller_with_nothing_on_sale_is_not_an_error():
    client, _ = client_with(
        [{"ret": ["SUCCESS::调用成功"], "data": {"cardList": [], "nextPage": False}}]
    )
    items, next_page = await listings(client)
    assert items == [] and next_page is False


@pytest.mark.asyncio
async def test_cards_we_do_not_understand_do_not_pass_as_listings():
    """A page of unknown card types is a changed shape, so it raises rather
    than reporting an empty on-sale list."""
    client, _ = client_with(
        [
            {
                "ret": ["SUCCESS::调用成功"],
                "data": {"cardList": [{"cardType": 9999, "cardData": {}}], "nextPage": False},
            }
        ]
    )
    with pytest.raises(ParseError):
        await listings(client)


# --------------------------------------------------------------------------- #
# Pipeline: paging, partial success, and the fallback that does not exist
# --------------------------------------------------------------------------- #


def make_item(item_id: str) -> RawItem:
    return RawItem(
        item_id=item_id,
        title="索尼耳机",
        price_cents=75000,
        seller_id=OPAQUE,
        seller_nick="小顾数码",
        source="mtop",
    )


class StubMtop:
    """Pages of (items, next_page), or an exception per page."""

    def __init__(self, pages: list) -> None:
        self.pages = pages
        self.calls: list[int] = []

    async def seller_listings(self, numeric_id, page, now_ms, *, seller_id, seller_nick):
        self.calls.append(page)
        assert numeric_id == NUMERIC
        assert seller_id == OPAQUE
        result = self.pages[page - 1]
        if isinstance(result, Exception):
            raise result
        return result


class StubBrowser:
    """The browser, in its two distinct roles.

    Collecting a seller's listings is one thing it cannot do. ESTABLISHING a
    session is one it can, and `_adopt_session` handing a token back is the
    whole reason a keyword rule survives an expired session without help --
    which is what `establishes` models here.
    """

    def __init__(self, session: UpstreamSession, *, establishes: bool = False) -> None:
        self.search_calls = 0
        self.search_args: list[tuple[str, int]] = []
        self._session = session
        self._establishes = establishes

    async def search(self, keyword, rows):
        self.search_calls += 1
        self.search_args.append((keyword, rows))
        if self._establishes:
            self._session.adopt({"_m_h5_tk": "tok_from_browser"}, origin="browser")
        return []


def pipeline_with(
    pages: list, *, usable: bool = True, establishes: bool = False
) -> tuple[Pipeline, StubMtop, StubBrowser]:
    session = UpstreamSession()
    if usable:
        session.adopt({"_m_h5_tk": "tok_1"}, origin="imported")
    mtop = StubMtop(pages)
    browser = StubBrowser(session, establishes=establishes)
    return Pipeline(mtop, browser, session), mtop, browser  # type: ignore[arg-type]


async def collect(pipeline: Pipeline, pages: int = 2) -> SearchResult:
    return await pipeline.collect_seller_listings(
        NUMERIC, seller_id=OPAQUE, seller_nick="小顾数码", pages=pages
    )


@pytest.mark.asyncio
async def test_an_unusable_session_is_established_first_not_just_reported():
    """The hole this closes: a deployment whose rules are ALL seller rules.

    A keyword rule survives an expired session for free -- `usable` is false,
    `collect_search` takes the browser route, a real page load happens and
    `_adopt_session` hands the token back. A seller rule has no such route, so
    merely reporting the absence meant backing off, failing five times,
    auto-disabling with a generic message, and never telling the user to
    re-import. `ensure_session` was written for exactly this and had zero
    callers until now.
    """
    pipeline, mtop, browser = pipeline_with(
        [([make_item("1")], False)], usable=False, establishes=True
    )

    result = await collect(pipeline)

    assert browser.search_calls == 1, "established once"
    assert browser.search_args == [("test", 1)], "established, NOT used to collect"
    assert [i.item_id for i in result.items] == ["1"], "then the real path ran"
    assert mtop.calls, "and the listing came from mtop, not from the browser"


@pytest.mark.asyncio
async def test_a_session_that_cannot_be_established_asks_for_a_human():
    """`ensure_session` returning False is its documented "risk control wants a
    human" answer. Raising the challenge class is what routes this to the
    notification and the `needs verification:` disable instead of to backoff --
    backoff would retry forever against something no retry fixes.
    """
    pipeline, mtop, browser = pipeline_with(
        [([make_item("1")], False)], usable=False, establishes=False
    )

    with pytest.raises(ChallengeError, match="needs verification"):
        await collect(pipeline)

    assert browser.search_calls == 1, "tried once"
    assert mtop.calls == [], "never signed a request without a token"


@pytest.mark.asyncio
async def test_next_page_false_stops_after_one_request():
    pipeline, mtop, _ = pipeline_with([([make_item("1"), make_item("2")], False)])
    result = await collect(pipeline, pages=3)
    assert mtop.calls == [1]
    assert len(result.items) == 2
    assert result.pages == 1


@pytest.mark.asyncio
async def test_a_second_page_is_read_when_next_page_says_so(monkeypatch):
    monkeypatch.setattr("app.collector.pipeline.INTER_PAGE_PAUSE", (0.0, 0.0))
    pipeline, mtop, _ = pipeline_with(
        [([make_item("1")], True), ([make_item("2")], False), ([make_item("3")], True)]
    )
    result = await collect(pipeline, pages=3)
    assert mtop.calls == [1, 2]
    assert [i.item_id for i in result.items] == ["1", "2"]
    assert result.pages == 2


@pytest.mark.asyncio
async def test_the_page_budget_is_the_ceiling(monkeypatch):
    """`settings.search_pages` is reused rather than a new knob: every page is
    one more upstream request, and that budget belongs in one place."""
    monkeypatch.setattr("app.collector.pipeline.INTER_PAGE_PAUSE", (0.0, 0.0))
    pipeline, mtop, _ = pipeline_with([([make_item("1")], True), ([make_item("2")], True)])
    result = await collect(pipeline, pages=2)
    assert mtop.calls == [1, 2]
    assert result.pages == 2


@pytest.mark.asyncio
async def test_a_repeated_page_stops_the_cycle(monkeypatch):
    """If `pageNumber` were ignored, every page would be page one: silent,
    expensive, and it would overstate the aperture `pages` reports."""
    monkeypatch.setattr("app.collector.pipeline.INTER_PAGE_PAUSE", (0.0, 0.0))
    pipeline, mtop, _ = pipeline_with([([make_item("1")], True), ([make_item("1")], True)])
    result = await collect(pipeline, pages=3)
    assert mtop.calls == [1, 2]
    assert len(result.items) == 1
    assert result.pages == 1, "a page that added nothing must not widen the aperture"


@pytest.mark.asyncio
async def test_a_failing_later_page_keeps_what_the_first_one_got(monkeypatch):
    monkeypatch.setattr("app.collector.pipeline.INTER_PAGE_PAUSE", (0.0, 0.0))
    pipeline, _, _ = pipeline_with([([make_item("1")], True), ParseError("shape moved")])
    result = await collect(pipeline, pages=2)
    assert [i.item_id for i in result.items] == ["1"]
    assert result.pages == 1
    assert result.partial_error is not None and "shape moved" in result.partial_error


@pytest.mark.asyncio
async def test_a_failing_first_page_is_a_failed_cycle():
    """Nothing was observed, and there is no second collector to try. Raising
    is what puts the reason in `last_error` and the cycle in the run log as
    ok=False."""
    pipeline, _, browser = pipeline_with([ParseError("shape moved")])
    with pytest.raises(ParseError):
        await collect(pipeline, pages=2)
    assert browser.search_calls == 0


@pytest.mark.parametrize(
    "error", [TransientCollectorError("FAIL_SYS_TRAFFIC_LIMIT"), ChallengeError("RGV587_ERROR")]
)
@pytest.mark.asyncio
async def test_throttling_and_challenges_still_belong_to_the_scheduler(error):
    """Backing off and asking for verification are the scheduler's decisions,
    exactly as on the search path."""
    pipeline, _, _ = pipeline_with([error])
    with pytest.raises(type(error)):
        await collect(pipeline, pages=2)
