"""Fill the database from the captured fixtures so the panel has real data.

Every listing, price, seller, and image here came off goofish for real and was
saved under tests/fixtures. That matters more than it sounds: hand-written demo
rows drift into nonsense (a Kindle title over a photo of a phone, an iPhone at
¥410), and once the data is incoherent a genuine display bug is
indistinguishable from a bad fixture. Real captured rows are self-consistent by
construction, and they run the actual parsing path on the way in.

The one thing a single capture CANNOT contain is a price history, so earlier
observations are synthesised backwards from the real current price. That is
labelled in the output rather than hidden.

    uv run python scripts/seed_demo.py [--break-one-image] [--reset]
"""

import argparse
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, delete, select  # noqa: E402

from app.collector.base import (  # noqa: E402
    RawSeller,
    normalize_item,
    normalize_seller,
)
from app.collector.mtop import (  # noqa: E402
    flatten_detail,
    flatten_search_row,
    flatten_seller,
)
from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Item,
    Monitor,
    MonitorHit,
    PriceSnapshot,
    Seller,
    utcnow,
)
from app.store import maybe_snapshot, upsert_item, upsert_seller_profile  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Fractions of the real current price, oldest first. A real listing that has
# been up for days has usually been marked down, which is exactly the shape the
# step chart exists to show.
HISTORY = ((1.18, 96), (1.08, 48), (1.0, 3))


def load_search() -> list:
    payload = json.loads((FIXTURES / "search_real.json").read_text(encoding="utf-8"))
    rows = payload.get("data", {}).get("resultList") or []
    return [normalize_item(flatten_search_row(row), "mtop") for row in rows]


def load_detail():
    payload = json.loads((FIXTURES / "detail_real.json").read_text(encoding="utf-8"))
    data = payload.get("data") or {}
    item_do = data.get("itemDO") or {}
    seller_do = data.get("sellerDO") or {}
    item_id = str(item_do.get("itemId") or "")
    item = normalize_item(flatten_detail(item_do, seller_do, item_id), "detail")
    seller = (
        normalize_seller(flatten_seller(seller_do), item.seller_id, "detail") if seller_do else None
    )
    return item, seller


def reset(session: Session) -> None:
    for table in (MonitorHit, PriceSnapshot, Item, Seller):
        session.exec(delete(table))
    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset", action="store_true", help="clear items, sellers, and history first"
    )
    parser.add_argument(
        "--break-one-image",
        action="store_true",
        help="point one cover at a missing file, to exercise the placeholder path",
    )
    args = parser.parse_args()

    init_db()
    now = utcnow()
    items = load_search()
    detail_item, detail_seller = load_detail()
    by_id = {item.item_id: item for item in items}
    by_id[detail_item.item_id] = detail_item  # detail carries description and more images

    with Session(engine) as session:
        if args.reset:
            reset(session)

        monitor = session.exec(select(Monitor).where(Monitor.name == "演示：苹果 15")).first()
        if monitor is None:
            monitor = Monitor(
                name="演示：苹果 15",
                keyword="iPhone 15 128G",
                price_min_cents=200000,
                price_max_cents=320000,
                interval_seconds=300,
                enabled=False,  # a demo rule must not start hitting upstream
                baseline_done=True,
            )
            session.add(monitor)
            session.commit()
            session.refresh(monitor)

        if detail_seller is not None:
            upsert_seller_profile(session, detail_seller)
            session.commit()

        for item in by_id.values():
            # Search rows already carry the seller's review count, positive
            # rate and shop flag. Writing only id and nick would throw away
            # real captured data and leave the profile panel showing 未知 for
            # sellers we actually know about.
            upsert_seller_profile(
                session,
                RawSeller(
                    seller_id=item.seller_id,
                    nick=item.seller_nick,
                    source="mtop",
                    avatar_url=item.seller_avatar_url,
                    is_shop=item.seller_is_shop,
                    review_count=item.seller_review_count,
                    positive_rate=item.seller_positive_rate,
                ),
            )
            session.commit()

            for factor, hours_ago in HISTORY:
                # RawItem is a frozen slots dataclass: no __dict__ to splat.
                observed = replace(item, price_cents=int(item.price_cents * factor))
                upsert_item(session, observed, now=now - timedelta(hours=hours_ago))
                maybe_snapshot(session, observed, now=now - timedelta(hours=hours_ago))
                session.commit()

            session.merge(
                MonitorHit(
                    monitor_id=monitor.id,
                    item_id=item.item_id,
                    first_hit_at=now - timedelta(hours=3),
                    in_range=True,
                    unverified_filters=json.dumps(["region", "min_seller_credit"]),
                )
            )
            session.commit()

        if args.break_one_image:
            victim = session.get(Item, next(iter(by_id)))
            victim.cover_url = "https://img.alicdn.com/bao/uploaded/no-such-file.jpg"
            session.add(victim)
            session.commit()

        # hit_count is a denormalised counter the scheduler increments; writing
        # MonitorHit rows directly leaves it at 0, and the page would then show
        # "0 hits" next to a list of four.
        monitor.hit_count = len(
            session.exec(select(MonitorHit).where(MonitorHit.monitor_id == monitor.id)).all()
        )
        session.add(monitor)
        session.commit()

        count = len(session.exec(select(Item)).all())
        snapshots = len(session.exec(select(PriceSnapshot)).all())
        monitor_name, monitor_id = monitor.name, monitor.id

    print(f"seeded {count} real listings from tests/fixtures, {snapshots} price snapshots")
    print(f"monitor: {monitor_name} (id={monitor_id}, disabled so it will not call upstream)")
    print("prices, titles, sellers, regions and images are REAL captures;")
    print("the earlier points of each price series are synthesised from the real current price")
    if args.break_one_image:
        print("one cover deliberately points at a missing file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
