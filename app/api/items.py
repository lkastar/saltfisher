"""Collected listings, their price history, and per-rule hit context.

The three read endpoints behind the hit list and the item detail page. Nothing
here writes: collection owns that path.
"""

import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col, select

from app.collector.filters import describe_unverified
from app.db import SessionDep
from app.models import Item, MonitorHit, PriceSnapshot, Seller
from app.schemas import ItemPublic, PricePoint
from app.store import NEWEST_FIRST, newest_snapshot_ids

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/items", tags=["items"])

# Literal rather than str: FastAPI turns a bad value into 422 instead of a
# 500, the value reaches OpenAPI as an enum so the generated frontend types
# give a union, and a query parameter can never be concatenated into ORDER BY.
Sort = Literal["-first_seen", "first_seen", "-last_seen", "price", "-price"]
Status = Literal["on_sale", "sold", "removed"]


def _json_list(raw: str | None) -> list[str]:
    """A malformed JSON column must not take a page down with it.

    These columns are written by the collector from upstream payloads, so a
    surprise there should cost one field, not the whole detail view.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("unparseable json column", extra={"raw": raw[:120]})
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _public(
    item: Item,
    snapshot: PriceSnapshot | None,
    seller: Seller | None,
    hit: MonitorHit | None,
) -> ItemPublic:
    return ItemPublic(
        id=item.id,
        title=item.title,
        description=item.description,
        cover_url=item.cover_url,
        image_urls=_json_list(item.image_urls),
        region=item.region,
        status=item.status,
        price_cents=snapshot.price_cents if snapshot else 0,
        publish_time=item.publish_time,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        seller_id=item.seller_id,
        seller_nick=item.seller_nick,
        seller_avatar_url=item.seller_avatar_url,
        # Every seller field stays None when unfetched. Defaulting to 0 would
        # read as "zero reviews", which is a warning sign rather than a gap.
        seller_is_shop=seller.is_shop if seller else None,
        seller_credit_level=seller.credit_level if seller else None,
        seller_credit_score=seller.credit_score if seller else None,
        seller_review_count=seller.review_count if seller else None,
        seller_positive_rate=seller.positive_rate if seller else None,
        seller_sold_count=seller.sold_count if seller else None,
        seller_verified=seller.verified if seller else None,
        first_hit_at=hit.first_hit_at if hit else None,
        notified_at=hit.notified_at if hit else None,
        in_range=hit.in_range if hit else None,
        # The readable labels come from the collector's own vocabulary so the
        # frontend does not keep a second copy of the filter names.
        unverified_filters=(
            describe_unverified(tuple(_json_list(hit.unverified_filters))) if hit else None
        ),
    )


@router.get("", response_model=list[ItemPublic])
def list_items(
    session: SessionDep,
    monitor_id: int | None = None,
    min_price_cents: Annotated[int | None, Query(ge=0)] = None,
    max_price_cents: Annotated[int | None, Query(ge=0)] = None,
    status: Status | None = None,
    sort: Sort = "-first_seen",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ItemPublic]:
    """One row per ITEM, not per hit.

    An item can match several rules; expanding by hit would list the same
    phone three times when the question being asked is "which items are
    there". Naming a monitor_id narrows the rows to that rule's matches and
    fills in the hit fields.
    """
    newest = newest_snapshot_ids().subquery()
    # Outer joins throughout: an item without a snapshot should not exist
    # (the first sighting always writes one), but if that invariant ever
    # breaks, a monitoring tool must not silently drop the row.
    price_cents = func.coalesce(col(PriceSnapshot.price_cents), 0)

    stmt = (
        select(Item, PriceSnapshot, Seller)
        .join(newest, col(Item.id) == newest.c.item_id, isouter=True)
        .join(PriceSnapshot, col(PriceSnapshot.id) == newest.c.snapshot_id, isouter=True)
        .join(Seller, col(Item.seller_id) == col(Seller.id), isouter=True)
    )

    if monitor_id is not None:
        stmt = stmt.where(
            col(Item.id).in_(select(MonitorHit.item_id).where(MonitorHit.monitor_id == monitor_id))
        )
    if status is not None:
        stmt = stmt.where(Item.status == status)
    if min_price_cents is not None:
        stmt = stmt.where(price_cents >= min_price_cents)
    if max_price_cents is not None:
        stmt = stmt.where(price_cents <= max_price_cents)

    orderings: dict[str, ColumnElement[Any]] = {
        "-first_seen": col(Item.first_seen_at).desc(),
        "first_seen": col(Item.first_seen_at).asc(),
        "-last_seen": col(Item.last_seen_at).desc(),
        "price": price_cents.asc(),
        "-price": price_cents.desc(),
    }
    # The id tiebreak is not cosmetic: without a total order, two rows sharing
    # a sort key can swap between pages and the same item shows up twice while
    # another never appears.
    stmt = stmt.order_by(orderings[sort], col(Item.id).asc()).offset(offset).limit(limit)

    rows = session.exec(stmt).all()

    hits: dict[str, MonitorHit] = {}
    if monitor_id is not None and rows:
        hits = {
            hit.item_id: hit
            for hit in session.exec(
                select(MonitorHit).where(
                    MonitorHit.monitor_id == monitor_id,
                    col(MonitorHit.item_id).in_([item.id for item, _, _ in rows]),
                )
            ).all()
        }

    return [_public(item, snapshot, seller, hits.get(item.id)) for item, snapshot, seller in rows]


@router.get("/{item_id}", response_model=ItemPublic)
def get_item(
    session: SessionDep,
    item_id: str,
    monitor_id: int | None = None,
) -> ItemPublic:
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    snapshot = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == item_id)
        .order_by(*NEWEST_FIRST)
        .limit(1)
    ).first()
    hit = session.get(MonitorHit, (monitor_id, item_id)) if monitor_id is not None else None
    return _public(item, snapshot, session.get(Seller, item.seller_id), hit)


@router.get("/{item_id}/prices", response_model=list[PricePoint])
def item_prices(
    session: SessionDep,
    item_id: str,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
) -> list[PricePoint]:
    """Ascending by time, because that is the axis a step chart draws against.

    The newest `limit` points are taken and then reversed: capping an ascending
    query would return the OLDEST points and quietly plot a chart that stops
    before the present. The ordering is by `captured_at` and not by `id` --
    ordering a time series by insertion and trusting the two to agree plots a
    chart that never happened the first time they do not.
    """
    if session.get(Item, item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")

    rows = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == item_id)
        .order_by(*NEWEST_FIRST)
        .limit(limit)
    ).all()
    return [
        PricePoint(
            price_cents=row.price_cents,
            status=row.status,
            captured_at=row.captured_at,
            source=row.source,
            want_count=row.want_count,
            view_count=row.view_count,
        )
        for row in reversed(rows)
    ]
