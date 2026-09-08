"""On-demand seller profile refresh.

One write path, reusing the collection machinery unchanged: there is no
standalone seller endpoint upstream, so a profile rides on an item-detail
fetch (`pipeline.collect_seller_via_item`) exactly as the screening path does.
The TTL that keeps cycles from re-fetching profiles applies here too — a
fresh profile answers from the database with `refreshed: false` and costs no
upstream request.
"""

import logging
from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import col, select

from app.db import SessionDep
from app.models import Item, Seller
from app.schemas import SellerRefreshResult
from app.store import fresh_seller_ids, record_seller_fetch_error, upsert_seller_profile

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sellers", tags=["sellers"])


def _public(seller: Seller, *, refreshed: bool) -> SellerRefreshResult:
    return SellerRefreshResult(
        seller_id=seller.id,
        seller_nick=seller.nick,
        seller_avatar_url=seller.avatar_url,
        seller_is_shop=seller.is_shop,
        seller_credit_level=seller.credit_level,
        seller_review_count=seller.review_count,
        seller_positive_rate=seller.positive_rate,
        seller_sold_count=seller.sold_count,
        seller_verified=seller.verified,
        fetched_at=seller.fetched_at,
        fetch_error=seller.fetch_error,
        refreshed=refreshed,
    )


@router.post("/{seller_id}/refresh", response_model=SellerRefreshResult)
async def refresh_seller(
    seller_id: str, session: SessionDep, request: Request
) -> SellerRefreshResult:
    """Fetch this seller's profile now, unless the stored one is still fresh.

    At most ONE upstream detail request per call (plus the pipeline's own
    browser degradation, same as every collection path). The profile is
    obtained through the seller's most recently seen item because that is the
    only route upstream offers — a seller with no collected items has nothing
    to fetch through, which is a 409, not a 500.
    """
    seller = session.get(Seller, seller_id)
    if seller is None:
        raise HTTPException(status_code=404, detail="seller not found")

    if seller_id in fresh_seller_ids(session, [seller_id]):
        return _public(seller, refreshed=False)

    # The freshest item is the one most likely to still resolve upstream.
    item_id = session.exec(
        select(col(Item.id))
        .where(col(Item.seller_id) == seller_id)
        .order_by(col(Item.last_seen_at).desc(), col(Item.id).asc())
        .limit(1)
    ).first()
    if item_id is None:
        raise HTTPException(
            status_code=409,
            detail="no collected item to fetch the profile through",
        )

    raw_seller, reason = await request.app.state.pipeline.collect_seller_via_item(item_id)
    if raw_seller is None:
        # Record the failure where the panel can see it (Seller.fetch_error,
        # truncated by the store), then answer with the same 502 shape the
        # other collector-touching endpoints use.
        record_seller_fetch_error(session, seller_id, reason or "no profile available")
        session.commit()
        raise HTTPException(status_code=502, detail=f"could not fetch the seller profile: {reason}")

    # Keyed on the id the ITEMS point at, never the detail response's own id:
    # the two id spaces differ, and writing the detail id would orphan the
    # profile from every item referencing it (same rule as store.persist_cycle).
    upsert_seller_profile(session, replace(raw_seller, seller_id=seller_id))
    session.commit()
    session.refresh(seller)
    return _public(seller, refreshed=True)
