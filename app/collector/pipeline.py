"""Degradation orchestration — the only module that picks a collector.

Corrected order of preference, forced by measurement
(research/mtop-access-probe.md): the browser is what establishes a session,
so mtop is only usable *after* one exists. The cheap path is therefore
preferred but not primary — it is tried first only when the session is valid.

    session usable ──yes──▶ mtop ──CollectorError──▶ browser
                   └──no───▶ browser (which also adopts a fresh session)
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from app.collector import filters
from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    RawItem,
    RawSeller,
    SearchResult,
    TransientCollectorError,
)
from app.collector.browser import BrowserCollector
from app.collector.filters import FilterOutcome, RuleFilters
from app.collector.mtop import MtopClient
from app.collector.session import UpstreamSession

log = logging.getLogger(__name__)

# Spacing between two pages of one search, jittered for the same reason
# scheduler.INTER_MONITOR_PAUSE is: a fixed cadence is the easiest bot
# signature there is, and paging turns one request per cycle into N.
INTER_PAGE_PAUSE = (3.0, 8.0)


def _now_ms() -> str:
    return str(int(time.time() * 1000))


# A profile missing because the SESSION was challenged, as opposed to
# anything about this seller. Re-importing credentials clears these, so the
# profile is fetched again on the next cycle instead of sitting out the full
# profile TTL -- see `scheduler.resume_challenge_disabled`.
CHALLENGE_REASON_PREFIX = "challenged:"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item with its filter verdict and any waived checks attached."""

    item: RawItem
    outcome: FilterOutcome
    # The profile fetched while screening, so the cycle can store what it
    # already paid a request for. It used to be consumed by the filter and
    # dropped: 249 seller rows with `fetched_at` on 6 of them -- every cycle
    # bought this data and threw it away, which is the request amplification
    # the pacing rules exist to prevent.
    seller: RawSeller | None = None
    # Why the profile is missing, when it is. Written to `Seller.fetch_error`
    # so "this seller's profile is empty" has an answer; None means it was
    # never asked for, which is not a failure.
    seller_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome.passed


class Pipeline:
    """Owns the two collectors. Constructed by the lifespan, one per process."""

    def __init__(
        self, mtop: MtopClient, browser: BrowserCollector, session: UpstreamSession
    ) -> None:
        self._mtop = mtop
        self._browser = browser
        self._session = session

    @property
    def session(self) -> UpstreamSession:
        """The shared upstream session.

        Exposed for the scheduler alone: a challenge is process-wide state, and
        the alert has to be deduped against it rather than against a per-cycle
        variable that resets every 15 seconds.
        """
        return self._session

    # ------------------------------------------------------------------ #
    # Acquisition with degradation
    # ------------------------------------------------------------------ #

    async def collect_search(self, keyword: str, rows: int = 30, pages: int = 1) -> SearchResult:
        """Search listings across `pages` pages, deduped by item_id.

        `pages` is passed in rather than read from Settings here: the scheduler
        owns the request budget, and this stays a plain argument so a test can
        ask for one page without touching global config.

        Termination is an EMPTY page or the page count — never "returned fewer
        rows than asked for". Measured 2026-09-05: page 1 of `iPhone 15`
        returned 28 listings for rows=30 while pages 2 and 3 returned 30 each,
        with zero overlap between any two pages. A count-based stop would have
        read page 1 as the last page and thrown away two thirds of the
        reachable inventory.

        Catches CollectorError, never Exception: a bug in the normaliser must
        crash loudly instead of silently sending every cycle through a
        200 MB browser launch. TransientCollectorError and ChallengeError
        propagate — backing off and asking for verification are the scheduler's
        decisions, not this function's, and neither means "fetch one page
        fewer".

        A later page failing returns the earlier pages plus `partial_error`
        instead of raising. Collapsing the cycle to zero would make the supply
        trend draw "page 3 timed out" as "no stock that day".
        """
        # ponytail: the browser is a fallback for page 1 only, and deliberately
        # so. browser.search() drives the mobile search URL, which has no page
        # parameter we have verified — paging it means either synthesising a
        # query string or clicking through infinite scroll, both untested
        # against the live DOM. A degraded path that fetches the narrower
        # aperture is honest; `pages` reports 1 and analytics reads the real
        # aperture from it. Widen only after a probe records how page 2 is
        # actually addressed in the DOM.
        if not self._session.usable:
            items = await self._browser.search(keyword, rows=rows)
            return SearchResult(items=tuple(items), pages=1)

        merged: dict[str, RawItem] = {}
        # Counts pages that WIDENED the aperture, not pages requested. The
        # frontend renders `pages x rows` as "how many listings we watched",
        # so a page that returned nothing, or returned only duplicates,
        # must not inflate it -- overstating the aperture defeats the only
        # reason the field is reported at all.
        fetched = 0
        for page in range(1, pages + 1):
            if page > 1:
                await asyncio.sleep(random.uniform(*INTER_PAGE_PAUSE))
            try:
                items = await self._mtop.search(keyword, page=page, rows=rows, now_ms=_now_ms())
            except (TransientCollectorError, ChallengeError):
                raise
            except CollectorError as exc:
                if page > 1:
                    log.warning(
                        "search page failed, keeping the pages already fetched",
                        extra={"keyword": keyword, "page": page, "err": str(exc)},
                    )
                    return SearchResult(
                        items=tuple(merged.values()), pages=fetched, partial_error=str(exc)
                    )
                log.warning(
                    "mtop search failed, falling back to browser",
                    extra={"keyword": keyword, "err": str(exc)},
                )
                items = await self._browser.search(keyword, rows=rows)
                return SearchResult(items=tuple(items), pages=1)
            if not items:
                break
            # First page wins a duplicate: the earlier observation is the one
            # whose rank we actually saw. Zero overlap was measured, but that
            # is an observation, not a guarantee — the pause between two pages
            # is long enough for upstream to re-rank.
            before = len(merged)
            for item in items:
                merged.setdefault(item.item_id, item)
            if len(merged) == before:
                # A whole page that contributed nothing new. Measured upstream,
                # three consecutive pages shared zero items, so this is not
                # ordinary churn: either the result set is exhausted, or
                # `pageNumber` is being ignored and every page is page one.
                # Both mean stop.
                #
                # Without this the second case is silent and expensive: five
                # requests an hour, every hour, for thirty listings — against
                # this phase's stated top risk, more requests tripping risk
                # control — and `pages` would report 5, so `aperture_pages_max`
                # would tell the user we watched 150 listings when we watched
                # 30. Overstating the aperture defeats the only reason that
                # field exists.
                log.info(
                    "search page added nothing, stopping early",
                    extra={"keyword": keyword, "page": page, "have": len(merged)},
                )
                break
            fetched = page
        return SearchResult(items=tuple(merged.values()), pages=fetched)

    async def collect_seller_listings(
        self, numeric_id: str, *, seller_id: str, seller_nick: str, pages: int = 1
    ) -> SearchResult:
        """A seller's on-sale listings across `pages` pages, deduped by item_id.

        Same return shape as `collect_search` on purpose, so `CollectRun.pages`
        keeps meaning one thing: pages that WIDENED the aperture, not pages
        requested.

        **A challenged or unestablished session fails outright — there is no
        browser fallback and pretending otherwise would be a lie.**
        `browser.search()` drives the mobile SEARCH url; a seller's page has a
        different DOM that has never been looked at, so there is nothing here
        to degrade to. An honest failure lands in `last_error` where the user
        can see it; a silent "collected 0 listings" reads as a seller who has
        stopped posting.
        """
        if not self._session.usable:
            # Establish one first, rather than only reporting its absence.
            #
            # A keyword rule heals this for free as a side effect: `usable` is
            # false, so `collect_search` takes the browser route, a real page
            # load happens, and `_adopt_session` hands the token back. A seller
            # rule has no such route -- so on a deployment whose rules are ALL
            # seller rules, reporting the absence would mean backing off, failing
            # five times, auto-disabling with a generic message, and never once
            # telling the user to re-import. `ensure_session` existed for exactly
            # this and had no callers at all until now.
            if not await self.ensure_session():
                # Its contract: False means risk control wants a human. Raising
                # the challenge class is what routes this to the notification
                # and the "needs verification" disable rather than to backoff.
                raise ChallengeError(
                    "cannot establish a session for a seller rule: needs verification"
                )
        if not self._session.usable:
            raise CollectorError(
                "seller listings need a live session and have no browser route: "
                "import a cookie session, or let a keyword rule establish one"
            )

        merged: dict[str, RawItem] = {}
        fetched = 0
        for page in range(1, pages + 1):
            if page > 1:
                await asyncio.sleep(random.uniform(*INTER_PAGE_PAUSE))
            try:
                items, next_page = await self._mtop.seller_listings(
                    numeric_id,
                    page=page,
                    now_ms=_now_ms(),
                    seller_id=seller_id,
                    seller_nick=seller_nick,
                )
            except (TransientCollectorError, ChallengeError):
                raise
            except CollectorError as exc:
                if page == 1:
                    raise
                # Later pages behave like search's: keep what page 1 got and
                # report the short aperture, because collapsing the cycle to
                # zero would draw "page 2 failed" as "nothing on sale".
                log.warning(
                    "seller listing page failed, keeping the pages already fetched",
                    extra={"seller_id": seller_id, "page": page, "err": str(exc)},
                )
                return SearchResult(
                    items=tuple(merged.values()), pages=fetched, partial_error=str(exc)
                )
            if not items:
                break
            before = len(merged)
            for item in items:
                merged.setdefault(item.item_id, item)
            if len(merged) == before:
                # Same guard as the search path, for the same measured risk: a
                # `pageNumber` that is ignored makes every page page one, which
                # is silent, expensive, and would overstate `pages`.
                log.info(
                    "seller listing page added nothing, stopping early",
                    extra={"seller_id": seller_id, "page": page, "have": len(merged)},
                )
                break
            fetched = page
            if not next_page:
                break
        return SearchResult(items=tuple(merged.values()), pages=fetched)

    async def collect_item(self, item_id: str) -> tuple[RawItem, RawSeller | None]:
        """Item detail and its seller profile — one request, both answers.

        The detail payload states 成色, 包邮, publish time, want/view counts and
        the item status as facts, all of which a search row can only guess at
        or omit entirely.
        """
        if self._session.usable:
            try:
                return await self._mtop.fetch_item(item_id, now_ms=_now_ms())
            except (ItemGoneError, TransientCollectorError, ChallengeError):
                raise
            except CollectorError as exc:
                log.warning(
                    "mtop detail failed, falling back to browser",
                    extra={"item_id": item_id, "err": str(exc)},
                )
        return await self._browser.fetch_item(item_id), None

    async def collect_seller_via_item(self, item_id: str) -> tuple[RawSeller | None, str | None]:
        """Seller profile, obtained by fetching the item's detail page.

        There is no standalone seller endpoint in our path (three candidate
        names returned FAIL_SYS_API_NOT_FOUNDED). Going through the item costs
        the same single request, uses an API we have actually verified, and
        avoids the opaque-vs-numeric seller id mismatch: the caller already
        knows which seller row this item points at.

        Returns `(None, reason)` rather than raising when unavailable — an
        unfetchable profile is a normal state, and the filter policy waives
        seller checks rather than dropping the item, so a failure here must not
        abort the cycle. The reason is returned instead of only logged because
        a log line does not answer "why is this seller's profile empty" three
        days later; `Seller.fetch_error` does.

        The reason is the exception TYPE plus the upstream's own short words (a
        ret envelope or an API name), the same material `notify.challenge_body`
        forwards. Never a cookie, a token, or a response body — `store` also
        truncates, so an interstitial page cannot land in the column.
        """
        try:
            _, seller = await self.collect_item(item_id)
        except ChallengeError as exc:
            # An auxiliary lookup must not decide the session is dead.
            log.warning("seller profile unavailable: session challenged")
            return None, f"{CHALLENGE_REASON_PREFIX} {exc}"
        except CollectorError as exc:
            log.warning("seller profile unavailable", extra={"item_id": item_id, "err": str(exc)})
            return None, f"{type(exc).__name__}: {exc}"
        if seller is None:
            # The browser answered. It scrapes the search DOM and has no
            # profile to give, which is a gap rather than a failure.
            return None, "no profile on the browser route"
        return seller, None

    async def ensure_session(self) -> bool:
        """Establish or refresh the upstream session via the browser.

        Returns False when risk control demands human verification, which the
        caller must surface rather than retry.
        """
        try:
            await self._browser.search("test", rows=1)
        except ChallengeError as exc:
            log.error("session establishment challenged", extra={"err": str(exc)})
            return False
        except CollectorError as exc:
            log.warning("session establishment incomplete", extra={"err": str(exc)})
        return self._session.usable

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #

    async def screen(
        self,
        items: list[RawItem],
        rule: RuleFilters,
        *,
        fetch_seller: bool = True,
        fresh_sellers: frozenset[str] = frozenset(),
    ) -> list[Candidate]:
        """Verdict for EVERY item, not only the survivors.

        The rejected ones matter: an item that was in budget and has since
        risen out of it must have that recorded, otherwise the next time it
        drops back the ledger still says "already in range" and the user is
        never told. Callers filter with `[c for c in candidates if c.passed]`.

        Seller profiles are still fetched only for items that passed the local
        checks — that ordering is what keeps a cycle at one request.
        """
        candidates: list[Candidate] = []
        for item in items:
            seller: RawSeller | None = None
            seller_error: str | None = None
            outcome = filters.apply_local(item, rule)
            if not outcome.passed:
                log.debug(
                    "filtered out",
                    extra={"item_id": item.item_id, "by": outcome.rejected_by},
                )
                candidates.append(Candidate(item=item, outcome=outcome))
                continue
            if outcome.needs_seller_profile:
                if fetch_seller and item.seller_id not in fresh_sellers:
                    seller, seller_error = await self.collect_seller_via_item(item.item_id)
                    outcome = filters.apply_seller(seller, rule, outcome)
                elif fetch_seller:
                    # Profile already stored and still inside the TTL. The
                    # freshness set is passed IN rather than queried here:
                    # `collector/` is forbidden from touching the database.
                    outcome = filters.apply_seller_from_item(item, rule, outcome)
                else:
                    # Still honour whatever the row itself revealed rather than
                    # waving everything through as compliant.
                    outcome = filters.apply_seller_from_item(item, rule, outcome)
            candidates.append(
                Candidate(item=item, outcome=outcome, seller=seller, seller_error=seller_error)
            )
        log.info(
            "screened",
            extra={"in": len(items), "passed": sum(1 for c in candidates if c.passed)},
        )
        return candidates
