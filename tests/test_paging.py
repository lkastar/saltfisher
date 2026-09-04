"""Search paging: the aperture, and what happens when part of it fails.

Two of these tests exist because of a measurement rather than a hunch
(`research/paging-probe.md`, 2026-09-05): pages 1/2/3 of `iPhone 15` returned
28/30/30 listings with **zero overlap**, so

1. a count-based stop ("returned fewer than rows") reads page 1 as the last
   page and throws away two thirds of the reachable inventory, and
2. single-page collection was never observing most of the market.

The third property under test is a failure expression, not a feature: a page
that fails after an earlier one succeeded must return the earlier pages, not
zero. Zeroing the cycle is what makes the supply trend draw "page 3 timed out"
as "no stock that day".
"""

import asyncio

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from app import scheduler
from app.collector.base import (
    ChallengeError,
    CollectorError,
    RawItem,
    TransientCollectorError,
)
from app.collector.filters import FilterOutcome
from app.collector.pipeline import INTER_PAGE_PAUSE, Candidate, Pipeline
from app.collector.session import UpstreamSession
from app.config import Settings
from app.models import CollectRun, Item, Monitor


def make_item(item_id: str, price_cents: int = 300000, source: str = "mtop") -> RawItem:
    return RawItem(
        item_id=item_id,
        title="iPhone 15",
        price_cents=price_cents,
        seller_id=f"s{item_id}",
        seller_nick="老王",
        source=source,
    )


def page_of(start: int, count: int) -> list[RawItem]:
    return [make_item(str(start + i)) for i in range(count)]


class PagedMtop:
    """Returns a scripted page per call; an Exception in the script is raised."""

    def __init__(self, *pages: list[RawItem] | Exception) -> None:
        self.pages = list(pages)
        self.requested: list[int] = []

    async def search(self, keyword, page, rows, now_ms):
        self.requested.append(page)
        scripted = self.pages[page - 1] if page - 1 < len(self.pages) else []
        if isinstance(scripted, Exception):
            raise scripted
        return list(scripted)


class StubBrowser:
    def __init__(self) -> None:
        self.search_calls = 0

    async def search(self, keyword, rows):
        self.search_calls += 1
        return [make_item("browser-1", source="browser")]


@pytest.fixture
def usable_session() -> UpstreamSession:
    return UpstreamSession(cookies={"_m_h5_tk": "tok_123"}, origin="browser")


@pytest.fixture
def no_pauses(monkeypatch) -> list[float]:
    """Records the inter-page waits instead of serving them."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


# --------------------------------------------------------------------------- #
# Termination
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_short_first_page_is_not_the_last_page(usable_session, no_pauses):
    """The measured trap. Page 1 gave 28 of the 30 rows asked for, while pages
    2 and 3 were full — so "returned < rows" is not evidence of the end.

    Reverse-verified: replacing the empty-page check with `len(items) < rows`
    makes this fail with one page and 28 items.
    """
    mtop = PagedMtop(page_of(1, 28), page_of(101, 30))
    result = await Pipeline(mtop, StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=2
    )
    assert mtop.requested == [1, 2]
    assert result.pages == 2
    assert len(result.items) == 58
    assert result.partial_error is None


@pytest.mark.asyncio
async def test_an_empty_page_stops_the_walk(usable_session, no_pauses):
    """The one termination signal we trust. Asking for page 3 after an empty
    page 2 spends a request to learn nothing, and every extra request is the
    thing paging has to stay frugal with."""
    mtop = PagedMtop(page_of(1, 30), [], page_of(201, 30))
    result = await Pipeline(mtop, StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=3
    )
    assert mtop.requested == [1, 2]
    assert len(result.items) == 30
    # One page widened the aperture, so that is what gets reported -- the
    # frontend turns this into "we watched pages x rows listings", and page 2
    # showed us nothing to watch.
    assert result.pages == 1


@pytest.mark.asyncio
async def test_a_page_of_only_duplicates_stops_the_walk(usable_session, no_pauses):
    """The failure this guards is silent, permanent and expensive.

    If upstream ignores `pageNumber` -- every page is page one -- dedup makes
    pages 2..N contribute nothing, and without this check the walk burns a
    request per page forever against this phase's stated top risk. Worse,
    `pages` would report 5, so the panel would tell the user it watched
    5 x 30 = 150 listings when it watched 30. The aperture field exists to be
    honest about exactly that number.

    Measured upstream, three consecutive real pages shared ZERO items, so a
    whole page of duplicates is not ordinary re-ranking: it is an exhausted
    result set or a broken page parameter, and both mean stop.
    """
    first = page_of(1, 30)
    mtop = PagedMtop(first, list(first), list(first), list(first), list(first))
    result = await Pipeline(mtop, StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=5
    )

    assert mtop.requested == [1, 2], "kept asking for pages that repeat page one"
    assert len(result.items) == 30
    assert result.pages == 1, "would have claimed a 5x wider aperture than it had"
    assert result.partial_error is None


@pytest.mark.asyncio
async def test_the_configured_count_is_the_other_stop(usable_session, no_pauses):
    mtop = PagedMtop(page_of(1, 30), page_of(101, 30), page_of(201, 30))
    result = await Pipeline(mtop, StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=2
    )
    assert mtop.requested == [1, 2]
    assert len(result.items) == 60


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_item_seen_on_two_pages_is_counted_once(usable_session, no_pauses):
    """Zero overlap was measured, but it is an observation, not a guarantee:
    the pause between two pages is long enough for upstream to re-rank."""
    first = [make_item("a", price_cents=100), make_item("b")]
    second = [make_item("a", price_cents=999), make_item("c")]
    result = await Pipeline(PagedMtop(first, second), StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=2
    )
    assert sorted(i.item_id for i in result.items) == ["a", "b", "c"]
    kept = next(i for i in result.items if i.item_id == "a")
    assert kept.price_cents == 100, "first sighting wins; that is the rank we actually saw"


# --------------------------------------------------------------------------- #
# Partial failure: half an aperture beats a false zero
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_later_page_failing_keeps_the_earlier_pages(usable_session, no_pauses):
    """Reverse-verified: removing the partial-success branch (re-raising
    instead) makes this fail with a CollectorError."""
    mtop = PagedMtop(page_of(1, 28), CollectorError("page 2 timed out"))
    browser = StubBrowser()
    result = await Pipeline(mtop, browser, usable_session).collect_search(
        "iPhone 15", rows=30, pages=2
    )
    assert len(result.items) == 28
    assert result.pages == 1, "the aperture was one page, and the row must say so"
    assert result.partial_error is not None
    assert "page 2" in result.partial_error
    assert browser.search_calls == 0, "mtop page 1 worked; a browser launch buys nothing"


@pytest.mark.asyncio
async def test_the_first_page_failing_still_degrades_to_the_browser(usable_session, no_pauses):
    """M2's behaviour, unchanged: with nothing in hand yet, the other
    collector is worth a try."""
    mtop = PagedMtop(CollectorError("shape changed"), page_of(101, 30))
    browser = StubBrowser()
    result = await Pipeline(mtop, browser, usable_session).collect_search(
        "iPhone 15", rows=30, pages=2
    )
    assert browser.search_calls == 1
    assert result.items[0].source == "browser"
    assert result.pages == 1
    assert mtop.requested == [1], "mtop is broken; page 2 would only be a second failure"


@pytest.mark.parametrize(
    "error",
    [TransientCollectorError("FAIL_SYS_TRAFFIC_LIMIT"), ChallengeError("RGV587_ERROR")],
)
@pytest.mark.asyncio
async def test_throttling_and_challenges_stop_the_cycle_entirely(usable_session, no_pauses, error):
    """These two mean stop, not "fetch one page fewer". Returning a partial
    result here would keep the next cycle hammering a session that risk
    control has already flagged.
    """
    mtop = PagedMtop(page_of(1, 30), error)
    browser = StubBrowser()
    with pytest.raises(type(error)):
        await Pipeline(mtop, browser, usable_session).collect_search("iPhone 15", rows=30, pages=2)
    assert browser.search_calls == 0


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pages_are_spaced_by_a_jittered_pause(usable_session, no_pauses):
    """A fixed cadence is the easiest bot signature there is, so the waits are
    drawn per page rather than shared."""
    pages = [page_of(1 + 100 * i, 30) for i in range(5)]
    await Pipeline(PagedMtop(*pages), StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=5
    )
    assert len(no_pauses) == 4, "one wait between pages, none before the first"
    low, high = INTER_PAGE_PAUSE
    assert all(low <= s <= high for s in no_pauses)
    assert len(set(no_pauses)) > 1, "a constant interval is not jitter"


@pytest.mark.asyncio
async def test_a_single_page_cycle_waits_for_nothing(usable_session, no_pauses):
    await Pipeline(PagedMtop(page_of(1, 30)), StubBrowser(), usable_session).collect_search(
        "iPhone 15", rows=30, pages=1
    )
    assert no_pauses == []


# --------------------------------------------------------------------------- #
# The page count is a safety setting
# --------------------------------------------------------------------------- #


def test_the_default_is_the_smallest_useful_widening():
    """2, not the 3 the probe covered: the first increment is a probe of how
    risk control reacts, and every page is one more upstream request."""
    assert Settings(api_token="testtoken123").search_pages == 2


@pytest.mark.parametrize("pages", [0, -1, 6, 30])
def test_an_out_of_range_page_count_is_refused(pages):
    with pytest.raises(ValidationError):
        Settings(api_token="testtoken123", search_pages=pages)


# --------------------------------------------------------------------------- #
# What reaches the run log
# --------------------------------------------------------------------------- #


class FakePipeline:
    """Returns a scripted SearchResult; screens everything through."""

    def __init__(self, result) -> None:
        self.result = result
        self.pages_asked: int | None = None

    async def collect_search(self, keyword, rows=30, pages=1):
        self.pages_asked = pages
        return self.result

    async def screen(self, items, rule, *, fetch_seller=True):
        return [Candidate(item=i, outcome=FilterOutcome(passed=True)) for i in items]


@pytest.fixture
def wired(monkeypatch):
    from tests.conftest import memory_engine

    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr("app.store.settings", scheduler.settings)
    with Session(engine) as s:
        monitor = Monitor(name="rule", keyword="iPhone 15", baseline_done=True)
        s.add(monitor)
        s.commit()
        assert monitor.id is not None
        return engine, monitor.id


def only_run(engine) -> CollectRun:
    with Session(engine) as s:
        (row,) = s.exec(select(CollectRun)).all()
        return row


@pytest.mark.asyncio
async def test_the_cycle_asks_for_the_configured_page_count(wired, monkeypatch):
    from app.collector.base import SearchResult

    engine, monitor_id = wired
    monkeypatch.setattr(scheduler.settings, "search_pages", 3)
    pipeline = FakePipeline(SearchResult(items=tuple(page_of(1, 58)), pages=3))
    monitor = scheduler._sync_load_monitor(monitor_id)
    assert monitor is not None

    outcome = await scheduler.run_monitor_cycle(pipeline, monitor)

    assert pipeline.pages_asked == 3
    assert outcome.pages == 3
    assert outcome.collected == 58


@pytest.mark.asyncio
async def test_the_run_row_records_the_aperture_it_reached(wired, monkeypatch):
    """`item_count` is deduped across every page we got, so it only means
    anything next to `pages`. A row saying 58 at one page and one saying 58 at
    three pages describe very different markets."""
    from app.collector.base import SearchResult

    engine, monitor_id = wired
    monkeypatch.setattr(scheduler.settings, "search_pages", 3)
    # Configured 3, reached 2 — an empty third page.
    pipeline = FakePipeline(SearchResult(items=tuple(page_of(1, 47)), pages=2))
    await scheduler._run_and_record(pipeline, monitor_id)

    row = only_run(engine)
    assert row.pages == 2
    assert row.item_count == 47
    assert row.ok is True


@pytest.mark.asyncio
async def test_a_partial_cycle_is_a_failed_run_but_not_a_failing_rule(wired, monkeypatch):
    """Design section 6: the run row carries the failure so the supply chart
    hatches that cycle, while the rule's own state stays clean. A rule that
    reliably gets page 1 and loses page 2 must not be auto-disabled after five
    cycles — that trades a narrow aperture for no collection at all.
    """
    from app.collector.base import SearchResult

    engine, monitor_id = wired
    monkeypatch.setattr(scheduler.settings, "search_pages", 2)
    pipeline = FakePipeline(
        SearchResult(items=tuple(page_of(1, 28)), pages=1, partial_error="page 2 timed out")
    )
    await scheduler._run_and_record(pipeline, monitor_id)

    row = only_run(engine)
    assert row.ok is False
    assert row.error == "page 2 timed out"
    assert row.item_count == 28, "the pages we did get are still coverage"
    assert row.pages == 1

    with Session(engine) as s:
        monitor = s.get(Monitor, monitor_id)
        assert monitor is not None
        assert monitor.last_error is None
        assert monitor.consecutive_failures == 0
        # And the listings themselves were persisted, which is the whole point.
        assert len(s.exec(select(Item)).all()) == 28
