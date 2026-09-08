"""What the seller profile does when it works, and when it does not.

Three properties, each of which was silently absent before P5 and each of
which was measured on the real database (253 seller rows) rather than guessed:

1. **A failed profile fetch says why.** `fetch_error` was NULL on all 253 rows
   because `upsert_seller_profile` only ever cleared it, so "why is this
   seller's profile empty" had no answer at all.
2. **A failure counts as "asked this cycle".** Recording the reason without
   moving `fetched_at` leaves the seller outside `fresh_seller_ids`, and the
   7-day TTL then re-asks a question that just failed — one extra upstream
   request per candidate per cycle, the exact amplification `a8baa25` removed.
3. **`listing_count` reaches the database.** `sellerDO.itemCount` has been
   parsed since M1 and dropped on the floor because `Seller` had no column.
"""

import json
import pathlib
import re
from datetime import timedelta

import pytest
from sqlmodel import Session

from app.collector.base import ChallengeError, RawSeller, normalize_seller
from app.collector.filters import FilterOutcome, RuleFilters
from app.collector.mtop import flatten_seller
from app.collector.pipeline import Candidate, Pipeline
from app.collector.session import UpstreamSession
from app.models import Seller, utcnow
from app.store import MAX_FETCH_ERROR_CHARS, fresh_seller_ids, persist_cycle
from tests.test_pipeline import StubBrowser, StubMtop, make_item
from tests.test_session_api import REAL_PASTE

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
NOW = utcnow()
MONITOR = 1

# The rule shape that makes `screen` want a profile at all. Without a seller
# filter no profile is fetched and none of this is exercised.
NEEDS_PROFILE = RuleFilters(min_seller_credit=1)


@pytest.fixture
def usable_session() -> UpstreamSession:
    """A session holding the full credential set a real paste installs.

    The real cookies are the point: the credential test below asserts against
    every value that is actually in the jar, not against one literal.
    """
    from app.api.session import parse_cookie_header

    return UpstreamSession(cookies=parse_cookie_header(REAL_PASTE), origin="browser")


async def failing_cycle(session: Session, exc: Exception, *, now=NOW) -> StubMtop:
    """One cycle where the profile lookup fails, persisted."""
    mtop = StubMtop(raises=exc)
    upstream = UpstreamSession(cookies={"_m_h5_tk": "tok_123"}, origin="browser")
    pipeline = Pipeline(mtop, StubBrowser(raises=exc), upstream)
    candidates = await pipeline.screen([make_item("1", seller_id="s1")], NEEDS_PROFILE)
    persist_cycle(session, MONITOR, candidates, baseline_done=True, now=now)
    session.commit()
    return mtop


# --------------------------------------------------------------------------- #
# A failed fetch leaves a reason behind
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_profile_fetch_records_why(session):
    await failing_cycle(session, ChallengeError("FAIL_SYS_USER_VALIDATE"))

    seller = session.get(Seller, "s1")
    assert seller is not None, "the row is created from the item either way"
    assert seller.fetch_error is not None, "the failure left no trace at all"
    assert "FAIL_SYS_USER_VALIDATE" in seller.fetch_error


@pytest.mark.asyncio
async def test_a_seller_whose_profile_failed_is_not_re_asked_next_cycle(session):
    """The failure must move `fetched_at`, or the TTL re-buys the failure.

    Leaving it NULL keeps the seller out of `fresh_seller_ids` forever, so the
    next cycle — and every cycle after it — spends another upstream request on
    the same unfetchable profile.
    """
    await failing_cycle(session, ChallengeError("FAIL_SYS_USER_VALIDATE"))
    assert session.get(Seller, "s1").fetched_at == NOW

    later = NOW + timedelta(minutes=5)
    fresh = fresh_seller_ids(session, ["s1"], now=later)
    assert "s1" in fresh, "a failure did not count as asked, so the TTL will re-ask"

    # And the next cycle really does skip the request, rather than merely being
    # entitled to: the freshness set is what `scheduler` passes into `screen`.
    mtop = StubMtop()
    pipeline = Pipeline(mtop, StubBrowser(), UpstreamSession(cookies={"_m_h5_tk": "t"}))
    await pipeline.screen([make_item("1", seller_id="s1")], NEEDS_PROFILE, fresh_sellers=fresh)
    assert mtop.seller_calls == 0, "re-bought a profile fetch that just failed"


@pytest.mark.asyncio
async def test_a_later_success_clears_the_error(session):
    """`fetch_error` is a current state, not a log. A stale reason next to a
    fetched profile reads as "still broken"."""
    await failing_cycle(session, ChallengeError("FAIL_SYS_USER_VALIDATE"))
    assert session.get(Seller, "s1").fetch_error is not None

    good = Candidate(
        item=make_item("1", seller_id="s1"),
        outcome=FilterOutcome(True),
        seller=RawSeller(seller_id="whatever", nick="老王", source="detail", credit_level=5),
    )
    persist_cycle(session, MONITOR, [good], baseline_done=True, now=NOW)
    session.commit()

    seller = session.get(Seller, "s1")
    assert seller.fetch_error is None
    assert seller.credit_level == 5


# --------------------------------------------------------------------------- #
# The reason is classification, never a credential
# --------------------------------------------------------------------------- #

# Matched by SHAPE as well as by value: a redaction test pinned to one literal
# passes happily while a different field leaks. Same list as
# `test_challenge_recovery.CREDENTIAL_SHAPE`.
CREDENTIAL_SHAPE = re.compile(
    r"(_m_h5_tk|_m_h5_tk_enc|cookie2|unb|sgcookie|_tb_token_|Bearer|Authorization)", re.I
)


@pytest.mark.asyncio
async def test_the_recorded_reason_carries_no_credential(session, usable_session):
    """`fetch_error` is the easy place to leak one: it comes from an exception
    nobody wrote with redaction in mind, and it is stored rather than logged.

    Asserted against EVERY value in the jar — a challenged session holds the
    whole credential set, and checking only `_m_h5_tk` would pass while
    `cookie2` sat in the column.
    """
    assert len(usable_session.cookies) >= 6, "the jar must be the real paste"

    mtop = StubMtop(raises=ChallengeError("RGV587_ERROR::SM::error"))
    pipeline = Pipeline(mtop, StubBrowser(raises=ChallengeError("challenged")), usable_session)
    candidates = await pipeline.screen([make_item("1", seller_id="s1")], NEEDS_PROFILE)
    persist_cycle(session, MONITOR, candidates, baseline_done=True, now=NOW)
    session.commit()

    stored = session.get(Seller, "s1").fetch_error
    assert stored, "nothing was stored, so nothing is proved"
    for name, value in usable_session.cookies.items():
        assert value not in stored, f"cookie {name} leaked its VALUE"
    assert not CREDENTIAL_SHAPE.search(stored), CREDENTIAL_SHAPE.search(stored)


@pytest.mark.asyncio
async def test_upstream_text_cannot_run_away_with_the_column(session):
    """A risk-control interstitial is a whole HTML page. Storing it verbatim is
    not an explanation, and the truncation lives at the write so no caller can
    route around it."""
    await failing_cycle(session, ChallengeError("x" * 10_000))
    assert len(session.get(Seller, "s1").fetch_error) <= MAX_FETCH_ERROR_CHARS


# --------------------------------------------------------------------------- #
# listing_count, from the real payload to the column
# --------------------------------------------------------------------------- #


def test_item_count_from_the_real_payload_reaches_the_database(session):
    """`sellerDO.itemCount` -> RawSeller.listing_count -> Seller.listing_count.

    Driven by the captured payload rather than a hand-written RawSeller: a
    fabricated input cannot tell a parsing bug apart from a missing column,
    which is precisely how this value went missing for four milestones.
    """
    payload = json.loads((FIXTURES / "detail_real.json").read_text())["data"]
    raw = normalize_seller(
        flatten_seller(payload["sellerDO"]),
        seller_id=str(payload["sellerDO"]["sellerId"]),
        source="detail",
    )
    assert raw.listing_count == 189, "the parse changed; this test is not the news"

    persist_cycle(
        session,
        MONITOR,
        [Candidate(item=make_item("1", seller_id="s1"), outcome=FilterOutcome(True), seller=raw)],
        baseline_done=True,
        now=NOW,
    )
    session.commit()

    assert session.get(Seller, "s1").listing_count == 189
