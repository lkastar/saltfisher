"""Watchlist endpoints.

The watchlist is deliberately decoupled from monitor rules: deleting a rule or
changing its keyword must not stop tracking something the user is waiting for.
"""

import logging
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import col, select

from app.collector.base import CollectorError, ItemGoneError
from app.db import SessionDep
from app.models import Item, PriceSnapshot, Seller, Watchlist, utcnow
from app.schemas import WatchlistCreate, WatchlistPublic, WatchlistUpdate
from app.store import (
    latest_price,
    latest_snapshots,
    upsert_item,
    upsert_seller_profile,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

# goofish ids are long digit runs. Share text mixes Chinese, a short link and
# the id, so extracting the longest digit run beats trying to match a URL.
_ID_RE = re.compile(r"(?<!\d)(\d{9,20})(?!\d)")
_SHORT_LINK_RE = re.compile(r"https?://[^\s，。）】]+")
_GOOFISH_HOSTS = ("goofish.com", "taobao.com", "tb.cn", "m.tb.cn")


async def resolve_item_id(text: str, client: httpx.AsyncClient) -> str:
    """Pull an item id out of a URL or an app share message.

    Accepts `goofish.com/item?id=...`, a taobao short link, and the share text
    that wraps one in Chinese prose. A short link is resolved by following the
    redirect, which is the only way its id becomes visible.

    **Precedence is id-first, on purpose.** Any long digit run in the text is
    taken as the item id without checking which host it came from, because
    share messages routinely mix prose, an id and an unrelated link. A digit
    run that turns out not to be a goofish item simply fails the subsequent
    detail fetch, which answers with a readable 404 — better than refusing
    text that does contain the id.
    """
    text = text.strip()
    direct = _ID_RE.search(text)
    if direct:
        return direct.group(1)

    link = _SHORT_LINK_RE.search(text)
    if link is None:
        raise ValueError("no link or item id found in the text")
    url = link.group(0)
    if not any(host in url for host in _GOOFISH_HOSTS):
        raise ValueError("link does not point at goofish")
    try:
        response = await client.get(url, follow_redirects=True, timeout=15.0)
    except httpx.HTTPError as exc:
        raise ValueError(f"could not follow the link: {exc}") from exc

    for candidate in (str(response.url), response.text[:4000]):
        found = _ID_RE.search(candidate)
        if found:
            return found.group(1)
    raise ValueError("followed the link but found no item id")


def _public(
    entry: Watchlist, item: Item, seller: Seller | None, latest_price: int
) -> WatchlistPublic:
    change = latest_price - entry.added_price_cents
    listed = (item.last_seen_at - item.first_seen_at).total_seconds() / 86400
    return WatchlistPublic(
        item_id=entry.item_id,
        title=item.title,
        price_cents=latest_price,
        added_price_cents=entry.added_price_cents,
        change_cents=change,
        change_ratio=(change / entry.added_price_cents) if entry.added_price_cents else 0.0,
        status=item.status,
        cover_url=item.cover_url,
        seller_nick=item.seller_nick,
        seller_is_shop=seller.is_shop if seller else None,
        seller_credit_level=seller.credit_level if seller else None,
        seller_positive_rate=seller.positive_rate if seller else None,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        listed_days=round(listed, 2),
        note=entry.note,
        price_watch_enabled=entry.price_watch_enabled,
        interval_seconds=entry.interval_seconds,
        last_run_at=entry.last_run_at,
        last_error=entry.last_error,
        added_at=entry.added_at,
    )


@router.get("", response_model=list[WatchlistPublic])
def list_watchlist(
    session: SessionDep,
    limit: Annotated[int, Query(le=200)] = 100,
) -> list[WatchlistPublic]:
    entries = session.exec(
        select(Watchlist).order_by(Watchlist.added_at.desc()).limit(limit)  # type: ignore[attr-defined]
    ).all()
    # Three batch reads instead of three queries per row: the list is small
    # today, but the same helpers serve /api/items with limit<=200.
    items = {
        item.id: item
        for item in session.exec(
            select(Item).where(col(Item.id).in_([e.item_id for e in entries]))
        ).all()
        if item.id is not None
    }
    sellers = {
        seller.id: seller
        for seller in session.exec(
            select(Seller).where(col(Seller.id).in_([i.seller_id for i in items.values()]))
        ).all()
    }
    prices = latest_snapshots(session, [e.item_id for e in entries])

    out: list[WatchlistPublic] = []
    for entry in entries:
        item = items.get(entry.item_id)
        if item is None:
            continue
        snapshot = prices.get(entry.item_id)
        out.append(
            _public(
                entry,
                item,
                sellers.get(item.seller_id),
                snapshot.price_cents if snapshot else 0,
            )
        )
    return out


@router.post("", response_model=WatchlistPublic, status_code=201)
async def add_to_watchlist(
    payload: WatchlistCreate, session: SessionDep, request: Request
) -> WatchlistPublic:
    pipeline = request.app.state.pipeline
    notify_client: httpx.AsyncClient = request.app.state.notify_client

    if payload.url:
        try:
            item_id = await resolve_item_id(payload.url, notify_client)
        except ValueError as exc:
            # A 400 with a readable reason, not a 500: unparseable share text
            # is user input, not a server fault.
            raise HTTPException(
                status_code=400, detail=f"could not identify an item: {exc}"
            ) from exc
    else:
        item_id = str(payload.item_id)

    if session.get(Watchlist, item_id) is not None:
        raise HTTPException(status_code=409, detail="item is already on the watchlist")

    item = session.get(Item, item_id)
    if item is None or payload.url:
        # A pasted link is fetched immediately: without a baseline price and a
        # title there is nothing to show and nothing to compare against.
        try:
            raw, seller = await pipeline.collect_item(item_id)
        except ItemGoneError as exc:
            raise HTTPException(status_code=404, detail="that listing no longer exists") from exc
        except CollectorError as exc:
            raise HTTPException(
                status_code=502, detail=f"could not fetch that listing: {exc}"
            ) from exc

        if seller is not None:
            upsert_seller_profile(session, seller)
        elif not session.get(Seller, raw.seller_id):
            session.add(Seller(id=raw.seller_id, nick=raw.seller_nick))
        session.flush()
        item = upsert_item(session, raw)
        session.add(
            PriceSnapshot(
                item_id=item_id,
                price_cents=raw.price_cents,
                want_count=raw.want_count,
                view_count=raw.view_count,
                status=raw.status,
                source=raw.source,
            )
        )
        session.flush()
        price = raw.price_cents
    else:
        price = latest_price(session, item_id)

    entry = Watchlist(
        item_id=item_id,
        added_price_cents=price,
        note=payload.note,
        interval_seconds=payload.interval_seconds,
        added_at=utcnow(),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    session.refresh(item)
    return _public(entry, item, session.get(Seller, item.seller_id), price)


@router.patch("/{item_id}", response_model=WatchlistPublic)
def update_watchlist(
    item_id: str, payload: WatchlistUpdate, session: SessionDep
) -> WatchlistPublic:
    entry = session.get(Watchlist, item_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="not on the watchlist")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(entry, key, value)
    if changes.get("price_watch_enabled") is True:
        # Re-enabling means "try again": clear the failure state, or one more
        # error immediately re-trips the auto-disable.
        entry.consecutive_failures = 0
        entry.last_error = None
    session.commit()
    session.refresh(entry)
    item = session.get(Item, item_id)
    assert item is not None
    return _public(entry, item, session.get(Seller, item.seller_id), latest_price(session, item_id))


@router.delete("/{item_id}", status_code=204)
def remove_from_watchlist(item_id: str, session: SessionDep) -> None:
    entry = session.get(Watchlist, item_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="not on the watchlist")
    # The Item and its price history stay: they are shared data and remain a
    # useful price reference.
    session.delete(entry)
    session.commit()
