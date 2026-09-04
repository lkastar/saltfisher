"""Playwright collector — the resilient path, and the only way to establish a
session at all.

Measurement (research/mtop-access-probe.md) showed the browser is what obtains
`_m_h5_tk`; a bare HTTP client never gets one. So this module has two jobs:

1. establish / refresh the upstream session for mtop to reuse
2. collect listings itself when mtop cannot

One browser and one persistent context for the process lifetime. Launching per
poll is the single most expensive mistake available in this codebase.
"""

import logging
from types import TracebackType
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.collector import base
from app.collector.base import ChallengeError, ItemGoneError, ParseError, RawItem, RawSeller
from app.collector.session import UpstreamSession
from app.config import settings

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.goofish.com/search?q={keyword}"
ITEM_URL = "https://www.goofish.com/item?id={item_id}"
SELLER_URL = "https://www.goofish.com/personal?userId={seller_id}"

# The page's own XHR carries the same JSON as the API, so intercepting it beats
# scraping the DOM: identical data, immune to markup changes.
SEARCH_XHR = "mtop.taobao.idlemtopsearch.pc.search"
ITEM_XHR = "mtop.taobao.idle.pc.detail"
SELLER_XHR = "mtop.idle.web.user.page"

# DOM fallback selectors, kept in one dict so a goofish redesign is a one-place
# fix rather than a hunt.
SELECTORS: dict[str, str] = {
    "result_card": '[class*="feeds-item"], [class*="cardWrap"]',
    "card_title": '[class*="title"]',
    "card_price": '[class*="price"]',
    "card_link": "a[href*='item?id=']",
    "card_seller": '[class*="userNick"], [class*="seller"]',
    "challenge_marker": "#baxia-dialog-content, .J_MIDDLEWARE_FRAME_WIDGET, iframe[src*='punish']",
}

BLOCKED_RESOURCES = {"image", "media", "font"}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserCollector:
    """Owns the browser. Constructed and closed by the FastAPI lifespan."""

    def __init__(self, session: UpstreamSession) -> None:
        self._session = session
        self._pw: Any = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        state_path = settings.data_dir / "state.json"
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._ctx = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            storage_state=str(state_path) if state_path.exists() else None,
        )
        await self._ctx.route("**/*", self._block_heavy_resources)
        log.info("browser started", extra={"reused_state": state_path.exists()})

    async def stop(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.storage_state(path=str(settings.data_dir / "state.json"))
            except Exception:  # noqa: BLE001 - shutdown must not fail on this
                log.warning("could not persist storage state", exc_info=True)
            await self._ctx.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def __aenter__(self) -> "BrowserCollector":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    @staticmethod
    async def _block_heavy_resources(route: Any) -> None:
        if route.request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    # ----------------------------------------------------------------- #

    def _context(self) -> BrowserContext:
        if self._ctx is None:
            raise RuntimeError("BrowserCollector.start() was never awaited")
        return self._ctx

    async def _open(self, url: str, xhr_marker: str) -> tuple[Page, list[dict[str, Any]]]:
        """Navigate and collect the JSON bodies of matching XHR responses."""
        captured: list[dict[str, Any]] = []
        page = await self._context().new_page()

        async def on_response(response: Any) -> None:
            if xhr_marker in response.url and "_____tmd_____" not in response.url:
                try:
                    captured.append(await response.json())
                except Exception:  # noqa: BLE001 - non-JSON is the challenge page
                    pass

        page.on("response", on_response)
        await page.goto(
            url, wait_until="domcontentloaded", timeout=settings.browser_timeout_s * 1000
        )
        await page.wait_for_timeout(4000)
        return page, captured

    async def _adopt_session(self) -> None:
        """Hand the browser's cookies to the cheap path."""
        cookies = {c["name"]: c["value"] for c in await self._context().cookies()}
        self._session.adopt(cookies, origin="browser")

    async def _guard_challenge(self, page: Page, captured: list[dict[str, Any]]) -> None:
        """Turn a risk-control interstitial into ChallengeError.

        The observed shape is `ret: ["RGV587_ERROR::SM::..."]` with
        `data: {url, h5url, dialogSize}` — a pointer to a verification page.
        """
        for body in captured:
            ret = (body.get("ret") or [""])[0]
            if not ret.startswith("SUCCESS"):
                self._session.mark_challenged(ret)
                raise ChallengeError(ret)
        if await page.locator(SELECTORS["challenge_marker"]).count():
            self._session.mark_challenged("verification dialog rendered on page")
            raise ChallengeError("verification dialog rendered on page")

    async def search(self, keyword: str, rows: int) -> list[RawItem]:
        page, captured = await self._open(SEARCH_URL.format(keyword=keyword), SEARCH_XHR)
        try:
            await self._adopt_session()
            await self._guard_challenge(page, captured)
            for body in captured:
                data = body.get("data") or {}
                for key in ("resultList", "items", "itemList"):
                    value = data.get(key)
                    if isinstance(value, list) and value:
                        items = []
                        for row in value[:rows]:
                            if not isinstance(row, dict):
                                continue
                            try:
                                items.append(base.normalize_item(row, source="browser"))
                            except ParseError as exc:
                                log.warning("skipping row", extra={"err": str(exc)})
                        if items:
                            return items
            return await self._scrape_cards(page, rows)
        finally:
            await page.close()

    async def _scrape_cards(self, page: Page, rows: int) -> list[RawItem]:
        """DOM fallback for when XHR interception yields nothing usable."""
        cards = page.locator(SELECTORS["result_card"])
        count = min(await cards.count(), rows)
        if count == 0:
            raise ParseError("no result cards and no usable XHR payload")
        items: list[RawItem] = []
        for i in range(count):
            card = cards.nth(i)
            try:
                href = await card.locator(SELECTORS["card_link"]).first.get_attribute("href")
                item_id = (href or "").split("id=")[-1].split("&")[0]
                title = (await card.locator(SELECTORS["card_title"]).first.inner_text()).strip()
                price = (await card.locator(SELECTORS["card_price"]).first.inner_text()).strip()
                nick_loc = card.locator(SELECTORS["card_seller"]).first
                nick = (await nick_loc.inner_text()).strip() if await nick_loc.count() else ""
                if not item_id or not title:
                    continue
                items.append(
                    RawItem(
                        item_id=item_id,
                        title=title,
                        price_cents=base.parse_price_cents(price),
                        # The card markup does not expose a seller id; the
                        # detail fetch fills it in. Empty is honest here.
                        seller_id="",
                        seller_nick=nick,
                        source="browser",
                        missing_fields=("seller_id",),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad card must not kill the scrape
                log.debug("card scrape failed", extra={"index": i, "err": str(exc)})
        if not items:
            raise ParseError(f"{count} cards present, none scrapeable — check SELECTORS")
        return items

    async def fetch_item(self, item_id: str) -> RawItem:
        page, captured = await self._open(ITEM_URL.format(item_id=item_id), ITEM_XHR)
        try:
            await self._adopt_session()
            await self._guard_challenge(page, captured)
            for body in captured:
                data = body.get("data") or {}
                if data:
                    return base.normalize_item(data, source="browser")
            raise ItemGoneError(f"item {item_id}: no detail payload")
        finally:
            await page.close()

    async def fetch_seller(self, seller_id: str) -> RawSeller:
        page, captured = await self._open(SELLER_URL.format(seller_id=seller_id), SELLER_XHR)
        try:
            await self._adopt_session()
            await self._guard_challenge(page, captured)
            merged: dict[str, Any] = {}
            for body in captured:
                data = body.get("data") or {}
                if isinstance(data, dict):
                    merged.update(data)
            return base.normalize_seller(merged, seller_id=seller_id, source="browser")
        finally:
            await page.close()
