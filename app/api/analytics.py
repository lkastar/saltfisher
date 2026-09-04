"""Market analytics endpoints.

Three read-only views over what collection has accumulated. All of them are
scoped to one keyword, because `Item` has no keyword of its own and the scope
has to be resolved through `MonitorHit` — see `app.analytics` for what that
implies about how the numbers should be read.

An unknown keyword is a 200 with `sample_size: 0`, never a 404. A deleted rule
or a stale shared link is ordinary here, and making the frontend handle a
special status for it buys nothing.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app import analytics
from app.db import SessionDep
from app.schemas import PriceDistribution, PriceDrops, SupplyTrend

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Constraints live in the signature, not in the body: FastAPI answers a bad
# value with 422 rather than a 500, and the bounds reach OpenAPI so the
# generated frontend types carry them too.
Keyword = Annotated[str, Query(min_length=1, max_length=200)]
Days = Annotated[int, Query(ge=1, le=365)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/price-distribution", response_model=PriceDistribution)
def price_distribution(session: SessionDep, keyword: Keyword, days: Days = 7) -> PriceDistribution:
    """Where this keyword's asking prices sit, one sample per listing.

    `days` bounds which listings count: those seen at least once in the
    window. The default is a week rather than "only what is on sale right
    now" because a cycle reads the first 30 search results, so the live set is
    about 30 listings per keyword — enough to draw, not enough to trust.
    `fresh_size` is what says how much of the window is still live.
    """
    return PriceDistribution(**analytics.price_distribution(session, keyword, days=days))


@router.get("/price-drops", response_model=PriceDrops)
def price_drops(
    session: SessionDep, keyword: Keyword, days: Days = 7, limit: Limit = 20
) -> PriceDrops:
    """Listings that have come down over the window, deepest first.

    Here `days` sets what "before" means: the price at the start of the
    window, compared with the current one. A listing that first appeared
    inside the window has no earlier price and is excluded.
    """
    return PriceDrops(**analytics.price_drops(session, keyword, days=days, limit=limit))


@router.get("/supply-trend", response_model=SupplyTrend)
def supply_trend(session: SessionDep, keyword: Keyword, days: Days = 30) -> SupplyTrend:
    """New listings per day, with days we did not collect marked as such.

    Here `days` is simply the width of the chart. Every day in the window is
    present, including ones with no records at all — a gap in the series would
    let the frontend close it up and invent a trend.
    """
    return SupplyTrend(**analytics.supply_trend(session, keyword, days=days))
