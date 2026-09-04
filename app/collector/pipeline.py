"""Degradation orchestration — the only module that picks a collector.

Corrected order of preference, forced by measurement
(research/mtop-access-probe.md): the browser is what establishes a session,
so mtop is only usable *after* one exists. The cheap path is therefore
preferred but not primary — it is tried first only when the session is valid.

    session usable ──yes──▶ mtop ──CollectorError──▶ browser
                   └──no───▶ browser (which also adopts a fresh session)
"""

import logging
import time
from dataclasses import dataclass

from app.collector import filters
from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    RawItem,
    RawSeller,
    TransientCollectorError,
)
from app.collector.browser import BrowserCollector
from app.collector.filters import FilterOutcome, RuleFilters
from app.collector.mtop import MtopClient
from app.collector.session import UpstreamSession

log = logging.getLogger(__name__)


def _now_ms() -> str:
    return str(int(time.time() * 1000))


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item with its filter verdict and any waived checks attached."""

    item: RawItem
    outcome: FilterOutcome

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

    # ------------------------------------------------------------------ #
    # Acquisition with degradation
    # ------------------------------------------------------------------ #

    async def collect_search(self, keyword: str, rows: int = 30) -> list[RawItem]:
        """Search listings.

        Catches CollectorError, never Exception: a bug in the normaliser must
        crash loudly instead of silently sending every cycle through a
        200 MB browser launch. TransientCollectorError and ChallengeError
        propagate — backing off and asking for verification are the scheduler's
        decisions, not this function's.
        """
        if self._session.usable:
            try:
                return await self._mtop.search(keyword, page=1, rows=rows, now_ms=_now_ms())
            except (TransientCollectorError, ChallengeError):
                raise
            except CollectorError as exc:
                log.warning(
                    "mtop search failed, falling back to browser",
                    extra={"keyword": keyword, "err": str(exc)},
                )
        return await self._browser.search(keyword, rows=rows)

    async def collect_item(self, item_id: str) -> RawItem:
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
        return await self._browser.fetch_item(item_id)

    async def collect_seller(self, seller_id: str) -> RawSeller | None:
        """Seller profile. Returns None instead of raising when unavailable.

        An unfetchable profile is a normal state (guest mode sees little of
        it), and the filter policy waives seller checks rather than dropping
        the item — so a None here must not abort the cycle.
        """
        try:
            if self._session.usable:
                try:
                    return await self._mtop.fetch_seller(seller_id, now_ms=_now_ms())
                except (TransientCollectorError, ChallengeError):
                    raise
                except CollectorError:
                    pass
            return await self._browser.fetch_seller(seller_id)
        except CollectorError as exc:
            log.warning(
                "seller profile unavailable", extra={"seller_id": seller_id, "err": str(exc)}
            )
            return None

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
        self, items: list[RawItem], rule: RuleFilters, *, fetch_seller: bool = True
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
            outcome = filters.apply_local(item, rule)
            if not outcome.passed:
                log.debug(
                    "filtered out",
                    extra={"item_id": item.item_id, "by": outcome.rejected_by},
                )
                candidates.append(Candidate(item=item, outcome=outcome))
                continue
            if outcome.needs_seller_profile:
                if fetch_seller and item.seller_id:
                    seller = await self.collect_seller(item.seller_id)
                    outcome = filters.apply_seller(seller, rule, outcome)
                else:
                    # Still honour whatever the row itself revealed rather than
                    # waving everything through as compliant.
                    outcome = filters.apply_seller_from_item(item, rule, outcome)
            candidates.append(Candidate(item=item, outcome=outcome))
        log.info(
            "screened",
            extra={"in": len(items), "passed": sum(1 for c in candidates if c.passed)},
        )
        return candidates
