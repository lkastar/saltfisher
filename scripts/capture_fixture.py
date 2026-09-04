"""Dump a real upstream payload into tests/fixtures/.

Why this exists: every probe from the development machine was answered with a
risk-control challenge, so the collector's field map (app/collector/base.py
ITEM_FIELD_MAP) is a set of *candidates*, not verified names. Run this once
from a network that is not challenged — ideally with cookies exported from
your own logged-in browser — and the fixtures become real data.

    uv run python -m scripts.capture_fixture --keyword "iPhone 15"

Output: tests/fixtures/search_real.json plus a report of which mapped fields
were found. Anything reported missing means the field map needs correcting.
"""

import argparse
import asyncio
import json
import pathlib
import sys

from app.collector import base
from app.collector.base import CollectorError
from app.collector.browser import BrowserCollector
from app.collector.session import UpstreamSession

FIXTURES = pathlib.Path("tests/fixtures")


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe the live upstream and capture a real payload. "
        "Replaces the separately named probe_search / probe_item scripts: "
        "one file, two flags, same coverage."
    )
    ap.add_argument("--keyword", default="iPhone 15", help="search probe")
    ap.add_argument("--item", help="detail probe for one item id")
    ap.add_argument("--seller", help="profile probe for one seller id")
    ap.add_argument("--rows", type=int, default=10)
    args = ap.parse_args()

    session = UpstreamSession()
    async with BrowserCollector(session) as browser:
        try:
            if args.item:
                item = await browser.fetch_item(args.item)
                print(f"item {args.item}: {item.title} @ {item.price_cents / 100:.2f}")
                print(f"missing fields: {item.missing_fields or 'none'}")
                return 0
            if args.seller:
                seller = await browser.fetch_seller(args.seller)
                print(f"seller {args.seller}: nick={seller.nick!r} is_shop={seller.is_shop}")
                print(f"missing fields: {seller.missing_fields or 'none'}")
                return 0
            items = await browser.search(args.keyword, rows=args.rows)
        except CollectorError as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "\nIf this is a ChallengeError, this network or session is under risk "
                "control. Import cookies from your own verified browser session and "
                "retry — see research/mtop-access-probe.md in the collector task.",
                file=sys.stderr,
            )
            return 1

        payload = [
            {k: v for k, v in vars(i).items() if k not in ("publish_time",)}
            | {"publish_time": i.publish_time.isoformat() if i.publish_time else None}
            for i in items
        ]
        (FIXTURES / "search_real.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )

        print(f"captured {len(items)} items -> {FIXTURES / 'search_real.json'}")
        print(f"session origin={session.origin} token={bool(session.token)}")
        missing: dict[str, int] = {}
        for i in items:
            for f in i.missing_fields:
                missing[f] = missing.get(f, 0) + 1
        if missing:
            print("\nFIELD MAP NEEDS WORK — fields absent from the payload:")
            for f, n in sorted(missing.items(), key=lambda kv: -kv[1]):
                print(f"  {f}: missing on {n}/{len(items)} items")
                print(f"    tried: {base.ITEM_FIELD_MAP.get(f)}")
        else:
            print("\nevery mapped field resolved — field map is correct for this payload")
    return 0


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)  # sync: before the event loop
    raise SystemExit(asyncio.run(main()))
