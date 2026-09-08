"""A seller rule, end to end through the scheduler.

`Monitor.seller_id` holds the OPAQUE canonical id (T2a). The listing API takes
the NUMERIC one, and the only place that mapping can be read is a listing's
`sellerDO.sellerId` — so a cycle has two halves: resolve the id once, then read
the on-sale list. Everything after that (screening, the hit ledger, baseline
silence, the run log) is the keyword path's machinery, reused unchanged, and
these tests are here to prove it really is reused rather than re-implemented.

The orchestration lives in `scheduler` and not in `collector/` because
`collector/` may not touch the database; `_sync_fresh_sellers` is the shape it
copies.
"""

import pytest
from sqlmodel import Session, select

from app import scheduler
from app.collector.base import CollectorError, RawItem, RawSeller, SearchResult
from app.collector.filters import FilterOutcome
from app.collector.pipeline import Candidate
from app.models import CollectRun, Item, Monitor, MonitorHit, Seller

pytestmark = pytest.mark.asyncio

OPAQUE = "6uSUGlb2aPN1F6kZNEnN9Q=="
NUMERIC = "2218219939144"


def listing(item_id: str, *, seller_id: str = OPAQUE) -> RawItem:
    """What `collect_seller_listings` hands back: the seller id is injected by
    the collector, so this is the shape under test rather than a stand-in."""
    return RawItem(
        item_id=item_id,
        title="索尼 WH-1000XM4",
        price_cents=75000,
        seller_id=seller_id,
        seller_nick="小顾数码",
        source="mtop",
    )


class FakePipeline:
    """No network. Counts what each cycle actually paid for."""

    def __init__(self, pages: list[list[RawItem]] | None = None, numeric_id: str = NUMERIC) -> None:
        self.pages = pages if pages is not None else [[listing("i-new")]]
        self.numeric_id = numeric_id
        self.detail_calls: list[str] = []
        self.listing_calls: list[str] = []

    async def collect_seller_via_item(self, item_id: str):
        self.detail_calls.append(item_id)
        return (
            RawSeller(
                seller_id=self.numeric_id,
                nick="小顾数码",
                source="detail",
                numeric_id=self.numeric_id,
            ),
            None,
        )

    async def collect_seller_listings(self, numeric_id, *, seller_id, seller_nick, pages):
        self.listing_calls.append(numeric_id)
        items = self.pages[min(len(self.listing_calls) - 1, len(self.pages) - 1)]
        return SearchResult(items=tuple(items), pages=1)

    async def screen(self, items, rule, *, fresh_sellers=frozenset()):
        return [Candidate(item=i, outcome=FilterOutcome(True)) for i in items]


@pytest.fixture
def wired(monkeypatch):
    """A real in-memory database holding one seller rule and one known listing
    of that seller — the listing the numeric id gets read from."""
    from tests.conftest import memory_engine

    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr("app.store.settings", scheduler.settings)

    with Session(engine) as s:
        s.add(Seller(id=OPAQUE, nick="小顾数码"))
        s.commit()
        s.add(Item(id="i-known", title="旧货", seller_id=OPAQUE, seller_nick="小顾数码"))
        rule = Monitor(name="小顾数码的新货", seller_id=OPAQUE, baseline_done=True)
        s.add(rule)
        s.commit()
        assert rule.id is not None
        return engine, rule.id


def runs(engine) -> list[CollectRun]:
    with Session(engine) as s:
        return list(s.exec(select(CollectRun).order_by(CollectRun.id)).all())


def load(engine, monitor_id: int) -> Monitor:
    with Session(engine) as s:
        monitor = s.get(Monitor, monitor_id)
        assert monitor is not None
        s.expunge(monitor)
        return monitor


# --------------------------------------------------------------------------- #
# FR-1: the numeric id, resolved once
# --------------------------------------------------------------------------- #


async def test_the_numeric_id_is_read_from_a_known_listing_and_stored(wired):
    """`sellerDO.sellerId` -> Seller.numeric_id -> the listing request.

    The whole chain in one assertion set, because each link on its own looks
    fine while the rule still collects nothing.
    """
    engine, monitor_id = wired
    pipeline = FakePipeline()

    outcome = await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert pipeline.detail_calls == ["i-known"]
    assert pipeline.listing_calls == [NUMERIC], "the opaque id here is rejected upstream"
    assert outcome.collected == 1
    with Session(engine) as s:
        assert s.get(Seller, OPAQUE).numeric_id == NUMERIC  # type: ignore[union-attr]


async def test_the_second_cycle_does_not_buy_the_same_mapping_again(wired):
    """One request per seller ever, not one per cycle. Re-resolving is exactly
    the per-cycle amplification the pacing rules exist to stop."""
    engine, monitor_id = wired
    pipeline = FakePipeline()

    await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]
    await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert pipeline.detail_calls == ["i-known"], "the stored mapping was ignored"
    assert pipeline.listing_calls == [NUMERIC, NUMERIC]


async def test_a_seller_row_already_keyed_on_the_numeric_id_costs_no_request(wired):
    """Two rows in the real database are keyed on the numeric id itself, left
    by the detail path before `8f10a77`. Spending an upstream request to
    rediscover what `id` already says would be buying what we hold.
    """
    engine, _ = wired
    with Session(engine) as s:
        s.add(Seller(id=NUMERIC, nick="小顾数码"))
        s.commit()
        rule = Monitor(name="按数字 id 建的规则", seller_id=NUMERIC, baseline_done=True)
        s.add(rule)
        s.commit()
        monitor_id = rule.id
    assert monitor_id is not None
    pipeline = FakePipeline(pages=[[listing("i-new", seller_id=NUMERIC)]])

    await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert pipeline.detail_calls == [], "no request should have been needed"
    assert pipeline.listing_calls == [NUMERIC]
    with Session(engine) as s:
        assert s.get(Seller, NUMERIC).numeric_id == NUMERIC  # type: ignore[union-attr]


async def test_a_seller_with_no_stored_listing_says_so_in_plain_words(wired):
    """The one case that cannot be resolved: every listing of theirs was
    delisted before we ever saw one. Not silently skipped and not a crash — a
    rule that never runs and never says why is what the health columns exist
    to prevent.
    """
    engine, _ = wired
    with Session(engine) as s:
        s.add(Seller(id="stranger==", nick="没抓过的卖家"))
        s.commit()
        rule = Monitor(name="没货可查", seller_id="stranger==", baseline_done=True)
        s.add(rule)
        s.commit()
        monitor_id = rule.id
    assert monitor_id is not None
    pipeline = FakePipeline()

    await scheduler._run_and_record(pipeline, monitor_id)

    stored = load(engine, monitor_id)
    assert "没抓过的卖家" in (stored.last_error or "")
    assert "no stored listing" in (stored.last_error or "")
    assert stored.consecutive_failures == 1
    assert stored.enabled, "one unresolvable cycle is not five failures"
    (row,) = [r for r in runs(engine) if r.monitor_id == monitor_id]
    assert row.ok is False
    assert pipeline.detail_calls == [] and pipeline.listing_calls == []


async def test_a_seller_rule_never_reaches_the_search_path(wired):
    """T2a made `keyword` nullable and guarded the cycle with a raise; this is
    the real routing that replaces the guard. The alternative to routing on the
    target is `collect_search(None)` formatting the literal string "None" into
    a search — so what matters is not only that the unresolvable rule fails,
    but that it fails in seller-shaped terms with the search path untouched.
    """
    engine, _ = wired

    class SearchRecorder(FakePipeline):
        def __init__(self) -> None:
            super().__init__()
            self.searches = 0

        async def collect_search(self, keyword, rows, pages):
            self.searches += 1
            return SearchResult(items=(), pages=0)

    with Session(engine) as s:
        s.add(Seller(id="stranger==", nick="没抓过的卖家"))
        s.commit()
        rule = Monitor(name="小顾数码的新货", seller_id="stranger==", baseline_done=True)
        s.add(rule)
        s.commit()
        monitor_id = rule.id
    assert monitor_id is not None
    pipeline = SearchRecorder()

    with pytest.raises(CollectorError, match="numeric id"):
        await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert pipeline.searches == 0
    assert pipeline.listing_calls == []


async def test_a_rule_pointing_at_a_seller_row_that_vanished_does_not_crash(wired):
    """`Monitor.seller_id` is a foreign key, so this needs the constraint off —
    which is precisely the state a database restored without foreign keys is
    in. It has to reach `last_error`, not a traceback.
    """
    engine, monitor_id = wired
    with Session(engine) as s:
        monitor = s.get(Monitor, monitor_id)
        assert monitor is not None
        monitor.seller_id = "gone=="
        s.commit()

    await scheduler._run_and_record(FakePipeline(), monitor_id)

    assert "not in the database" in (load(engine, monitor_id).last_error or "")


# --------------------------------------------------------------------------- #
# FR-2/FR-4: what gets stored, and who gets told
# --------------------------------------------------------------------------- #


async def test_the_collected_listings_point_at_the_opaque_seller_row(wired):
    """A numeric `Item.seller_id` here would orphan every collected listing
    from the profile and the rule, creating a second row for one person — the
    duplicate `8f10a77` removed.
    """
    engine, monitor_id = wired

    await scheduler.run_monitor_cycle(FakePipeline(), load(engine, monitor_id))  # type: ignore[arg-type]

    with Session(engine) as s:
        item = s.get(Item, "i-new")
        assert item is not None and item.seller_id == OPAQUE
        seller_ids = set(s.exec(select(Seller.id)).all())
    assert seller_ids == {OPAQUE}, "a numeric id leaked into the seller table"


async def test_the_first_cycle_of_a_new_seller_rule_tells_nobody(wired):
    """Baseline silence, unchanged: a fresh rule records what the seller
    already has on sale instead of pushing his whole shelf at the user.
    """
    engine, _ = wired
    with Session(engine) as s:
        rule = Monitor(name="新建的卖家规则", seller_id=OPAQUE)
        s.add(rule)
        s.commit()
        monitor_id = rule.id
    assert monitor_id is not None
    assert not load(engine, monitor_id).baseline_done
    pipeline = FakePipeline(pages=[[listing("i-a"), listing("i-b")], [listing("i-c")]])

    first = await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert first.collected == 2
    assert first.hits == [], "the seller's existing stock is not news"
    with Session(engine) as s:
        assert len(s.exec(select(MonitorHit).where(MonitorHit.monitor_id == monitor_id)).all()) == 2

    second = await scheduler.run_monitor_cycle(pipeline, load(engine, monitor_id))  # type: ignore[arg-type]

    assert [h.item_id for h in second.hits] == ["i-c"], "newly listed IS the hit"


async def test_a_seller_cycle_is_logged_like_any_other(wired):
    engine, monitor_id = wired

    await scheduler._run_and_record(FakePipeline(), monitor_id)

    (row,) = runs(engine)
    assert (row.monitor_id, row.ok, row.item_count, row.pages) == (monitor_id, True, 1, 1)
    assert row.collector == "mtop"
    assert load(engine, monitor_id).last_error is None


async def test_a_collection_failure_reaches_the_rule_unchanged(wired):
    """The seller path raises the same CollectorError family as the search
    path, so it needs no branch of its own in `_run_and_record`."""
    engine, monitor_id = wired
    pipeline = FakePipeline()

    async def boom(*a, **kw):
        raise CollectorError("no browser route")

    pipeline.collect_seller_listings = boom  # type: ignore[method-assign]
    await scheduler._run_and_record(pipeline, monitor_id)

    assert "no browser route" in (load(engine, monitor_id).last_error or "")
    assert runs(engine)[0].ok is False
