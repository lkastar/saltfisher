"""Degradation orchestration, with stub collectors — no network.

The two properties worth proving here are the ones that silently ruin the tool
when broken:

1. **Which errors degrade and which propagate.** A rate limit must back off, a
   challenge must ask for verification, and a bug in our own normaliser must
   crash loudly instead of routing every future cycle through a 200 MB browser.
2. **Seller profiles are fetched only for survivors.** Getting that backwards
   turns one request per cycle into one per search result and walks straight
   into risk control.
"""

import pytest

from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    ParseError,
    RawItem,
    RawSeller,
    TransientCollectorError,
)
from app.collector.filters import RuleFilters
from app.collector.pipeline import Pipeline
from app.collector.session import UpstreamSession


def make_item(item_id: str = "1", **kw) -> RawItem:
    return RawItem(
        **{
            **dict(
                item_id=item_id,
                title="iPhone 15",
                price_cents=300000,
                seller_id=f"s{item_id}",
                seller_nick="老王",
                source="stub",
            ),
            **kw,
        }
    )


class StubMtop:
    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.search_calls = 0
        self.seller_calls = 0
        self.item_calls = 0

    async def search(self, keyword, page, rows, now_ms):
        self.search_calls += 1
        if self.raises:
            raise self.raises
        return [make_item("mtop-1", source="mtop")]

    async def fetch_item(self, item_id, now_ms):
        # One call returns both: the detail response carries `sellerDO`.
        self.item_calls += 1
        self.seller_calls += 1
        if self.raises:
            raise self.raises
        return (
            make_item(item_id, source="mtop"),
            RawSeller(seller_id="s1", nick="老王", source="detail", credit_level=5),
        )


class StubBrowser:
    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.search_calls = 0
        self.seller_calls = 0
        self.item_calls = 0

    async def search(self, keyword, rows):
        self.search_calls += 1
        if self.raises:
            raise self.raises
        return [make_item("browser-1", source="browser")]

    async def fetch_item(self, item_id):
        # The scraped page exposes no seller profile block.
        self.item_calls += 1
        if self.raises:
            raise self.raises
        return make_item(item_id, source="browser")


@pytest.fixture
def usable_session() -> UpstreamSession:
    return UpstreamSession(cookies={"_m_h5_tk": "tok_123"}, origin="browser")


@pytest.fixture
def no_session() -> UpstreamSession:
    return UpstreamSession()


pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Which path runs
# --------------------------------------------------------------------------- #


async def test_without_a_session_the_browser_is_used_directly(no_session):
    """mtop cannot mint a token, so trying it first would only waste a call."""
    mtop, browser = StubMtop(), StubBrowser()
    result = await Pipeline(mtop, browser, no_session).collect_search("iPhone")
    assert mtop.search_calls == 0
    assert browser.search_calls == 1
    assert result.items[0].source == "browser"
    # The browser covers page 1 only, and the aperture says so.
    assert result.pages == 1


async def test_with_a_session_the_cheap_path_is_preferred(usable_session):
    mtop, browser = StubMtop(), StubBrowser()
    result = await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert (mtop.search_calls, browser.search_calls) == (1, 0)
    assert result.items[0].source == "mtop"


async def test_collector_error_degrades_to_the_browser(usable_session):
    mtop = StubMtop(raises=CollectorError("shape changed"))
    browser = StubBrowser()
    result = await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert (mtop.search_calls, browser.search_calls) == (1, 1)
    assert result.items[0].source == "browser"


async def test_parse_error_degrades_too(usable_session):
    """ParseError is a CollectorError: the payload shape moved, so the other
    collector is worth a try."""
    mtop = StubMtop(raises=ParseError("no result list"))
    browser = StubBrowser()
    result = await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert browser.search_calls == 1 and result.items[0].source == "browser"


# --------------------------------------------------------------------------- #
# Which errors must NOT degrade
# --------------------------------------------------------------------------- #


async def test_rate_limit_backs_off_instead_of_degrading(usable_session):
    """Being throttled is not being broken; launching a browser makes it worse."""
    mtop = StubMtop(raises=TransientCollectorError("FAIL_SYS_TRAFFIC_LIMIT"))
    browser = StubBrowser()
    with pytest.raises(TransientCollectorError):
        await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert browser.search_calls == 0


async def test_challenge_propagates_for_the_scheduler_to_surface(usable_session):
    mtop = StubMtop(raises=ChallengeError("RGV587_ERROR"))
    browser = StubBrowser()
    with pytest.raises(ChallengeError):
        await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert browser.search_calls == 0


async def test_a_bug_in_our_own_code_is_not_swallowed(usable_session):
    """The rule that keeps a normaliser TypeError from turning into a
    permanent browser fallback on every single cycle.
    """
    mtop = StubMtop(raises=TypeError("normaliser bug"))
    browser = StubBrowser()
    with pytest.raises(TypeError):
        await Pipeline(mtop, browser, usable_session).collect_search("iPhone")
    assert browser.search_calls == 0


async def test_item_gone_is_not_a_failure_to_retry(usable_session):
    mtop = StubMtop(raises=ItemGoneError("deleted"))
    browser = StubBrowser()
    with pytest.raises(ItemGoneError):
        await Pipeline(mtop, browser, usable_session).collect_item("42")
    assert browser.item_calls == 0


async def test_detail_returns_the_item_and_its_seller_together(usable_session):
    """One request, both answers — there is no standalone seller endpoint."""
    mtop, browser = StubMtop(), StubBrowser()
    item, seller = await Pipeline(mtop, browser, usable_session).collect_item("42")
    assert item.source == "mtop"
    assert seller is not None and seller.credit_level == 5
    assert mtop.item_calls == 1


async def test_the_browser_route_has_no_seller_profile(usable_session):
    """Scraping the page cannot produce what the API returns; saying so is
    better than fabricating an empty profile that looks fetched."""
    mtop = StubMtop(raises=CollectorError("api down"))
    browser = StubBrowser()
    item, seller = await Pipeline(mtop, browser, usable_session).collect_item("42")
    assert item.source == "browser"
    assert seller is None


# --------------------------------------------------------------------------- #
# Seller profile: unavailable is a state, not an error
# --------------------------------------------------------------------------- #


async def test_seller_profile_returns_none_when_both_paths_fail(usable_session):
    mtop = StubMtop(raises=CollectorError("nope"))
    browser = StubBrowser(raises=CollectorError("nope"))
    assert await Pipeline(mtop, browser, usable_session).collect_seller_via_item("i1") is None


async def test_a_challenged_profile_lookup_does_not_kill_the_session(usable_session):
    """Regression, found on live data twice over.

    A challenged profile lookup used to mark the SHARED session as needing
    verification, auto-disabling rules whose search path was working fine.
    Risk control is applied per endpoint, so an auxiliary lookup may degrade
    one filter and nothing more.
    """
    mtop = StubMtop(raises=ChallengeError("FAIL_SYS_USER_VALIDATE"))
    browser = StubBrowser(raises=CollectorError("no browser session"))
    assert await Pipeline(mtop, browser, usable_session).collect_seller_via_item("i1") is None
    assert usable_session.usable


async def test_seller_profile_is_absent_when_only_the_browser_answers(usable_session):
    mtop = StubMtop(raises=CollectorError("nope"))
    browser = StubBrowser()
    assert await Pipeline(mtop, browser, usable_session).collect_seller_via_item("i1") is None


# --------------------------------------------------------------------------- #
# Request-count discipline
# --------------------------------------------------------------------------- #


async def test_seller_profile_is_fetched_only_for_survivors(usable_session):
    """Three results, one survives the local filters -> exactly one profile
    request. Reversing the order would make it three.
    """
    mtop, browser = StubMtop(), StubBrowser()
    items = [
        make_item("1", title="iPhone 15 128G", price_cents=300000),
        make_item("2", title="iPhone 15 手机壳", price_cents=3900),
        make_item("3", title="iPhone 15 Pro", price_cents=900000),
    ]
    rule = RuleFilters(
        price_min_cents=200000,
        price_max_cents=350000,
        exclude_words="壳 膜",
        min_seller_credit=3,
    )
    candidates = await Pipeline(mtop, browser, usable_session).screen(items, rule)
    assert len(candidates) == 3, "every item gets a verdict, not just survivors"
    assert [c.item.item_id for c in candidates if c.passed] == ["1"]
    assert mtop.seller_calls == 1


async def test_no_seller_condition_means_no_profile_request(usable_session):
    mtop, browser = StubMtop(), StubBrowser()
    candidates = await Pipeline(mtop, browser, usable_session).screen(
        [make_item("1")], RuleFilters()
    )
    assert len(candidates) == 1
    assert mtop.seller_calls == 0 and browser.seller_calls == 0


async def test_unfetchable_profile_waives_the_check_and_labels_it(usable_session):
    """The conservative-pass policy, end to end through the pipeline."""
    mtop = StubMtop(raises=CollectorError("nope"))
    browser = StubBrowser(raises=CollectorError("nope"))
    candidates = await Pipeline(mtop, browser, usable_session).screen(
        [make_item("1")], RuleFilters(min_seller_credit=3)
    )
    assert len(candidates) == 1 and candidates[0].passed
    assert "min_seller_credit" in candidates[0].outcome.unverified


async def test_rejected_items_keep_their_verdict_for_the_ledger(usable_session):
    """An item that has risen out of budget must be reported as rejected, so the
    hit ledger can flip in_range back to False. Dropping it silently means the
    next price fall never registers as a fresh entry into the range.
    """
    mtop, browser = StubMtop(), StubBrowser()
    candidates = await Pipeline(mtop, browser, usable_session).screen(
        [make_item("1", price_cents=900000)], RuleFilters(price_max_cents=350000)
    )
    assert len(candidates) == 1
    assert not candidates[0].passed
    assert candidates[0].outcome.rejected_by == "price"


async def test_screen_can_skip_profile_fetching_entirely(usable_session):
    mtop, browser = StubMtop(), StubBrowser()
    candidates = await Pipeline(mtop, browser, usable_session).screen(
        [make_item("1")], RuleFilters(min_seller_credit=3), fetch_seller=False
    )
    assert mtop.seller_calls == 0
    assert "min_seller_credit" in candidates[0].outcome.unverified


# --------------------------------------------------------------------------- #
# Seller profiles: paid for once, kept, and not re-bought
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_profile_fetched_while_screening_reaches_the_candidate(usable_session):
    """The fetch used to be consumed by the filter and dropped.

    Measured on the real database before this: 249 seller rows, `fetched_at`
    on 6 of them, `credit_score` on none -- every cycle bought a profile per
    candidate and threw it away, so FR-3b's dataset stayed empty while the
    request count stayed high. Carrying it on the Candidate is what lets
    `persist_cycle` store what was already paid for.
    """
    mtop = StubMtop()
    pipeline = Pipeline(mtop, StubBrowser(), usable_session)
    rule = RuleFilters(min_seller_credit=1)

    candidates = await pipeline.screen([make_item("1", seller_id="s1")], rule)

    assert len(candidates) == 1
    assert mtop.seller_calls == 1, "no profile was fetched, so nothing is proved"
    assert candidates[0].seller is not None, "the profile was fetched and discarded"
    assert candidates[0].seller.credit_level == 5


@pytest.mark.asyncio
async def test_a_seller_with_a_fresh_profile_is_not_fetched_again(usable_session):
    """`settings.seller_profile_ttl_days` had no second reference in the whole
    project, so the documented 7-day cache did not exist and every cycle
    re-fetched. One extra upstream request per candidate per cycle is the
    request amplification the pacing rules are written to prevent.

    The freshness set is passed IN because `collector/` may not read the
    database; `store.fresh_seller_ids` computes it in one query.
    """
    mtop = StubMtop()
    pipeline = Pipeline(mtop, StubBrowser(), usable_session)
    rule = RuleFilters(min_seller_credit=1)

    await pipeline.screen([make_item("1", seller_id="s1")], rule, fresh_sellers=frozenset({"s1"}))

    assert mtop.seller_calls == 0, "re-bought a profile it already had"
