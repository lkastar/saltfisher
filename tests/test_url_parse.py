"""Item-id extraction from links and app share text.

Without this the tool is closed inside its own search results: the specific
listing a user spotted in the app, or a friend sent them, cannot be watched at
all. The failure mode that matters is a 500 on unparseable input, which reads
as a server fault when it is ordinary user text.
"""

import httpx
import pytest

from app.api.watchlist import resolve_item_id

pytestmark = pytest.mark.asyncio


def no_network() -> httpx.AsyncClient:
    """Any request means the id should have been found in the text."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "text",
    [
        "https://www.goofish.com/item?id=1081966784098",
        "https://www.goofish.com/item?id=1081966784098&spm=a2170.1",
        "goofish.com/item?id=1081966784098",
        "1081966784098",
        "  1081966784098  ",
        "https://h5.m.goofish.com/item?id=1081966784098&x=1",
    ],
)
async def test_ids_present_in_the_text_need_no_request(text):
    assert await resolve_item_id(text, no_network()) == "1081966784098"


async def test_app_share_text_with_chinese_prose():
    """The real shape of a share message: prose, an id, and a short link."""
    text = "【闲鱼】￥2650 淘口令 iPhone 15粉色 128 成色非常好 1081966784098 快来抢"
    assert await resolve_item_id(text, no_network()) == "1081966784098"


async def test_a_short_link_is_followed_to_find_the_id():
    """A short link hides the id, so the redirect is the only way to see it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "tb.cn" in str(request.url):
            return httpx.Response(
                302, headers={"location": "https://www.goofish.com/item?id=1081966784098"}
            )
        return httpx.Response(200, text="<html>landed</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await resolve_item_id("看看这个 https://m.tb.cn/h.abcdef", client) == "1081966784098"
    assert len(seen) == 2, "the redirect must actually be followed"


async def test_an_id_in_the_text_wins_over_the_host_check():
    """Precedence is id-first: share messages mix prose, an id and unrelated
    links, so refusing text that plainly contains the id would be worse. A
    wrong id fails the later detail fetch with a readable 404.
    """
    assert (
        await resolve_item_id("https://example.com/item?id=1081966784098", no_network())
        == "1081966784098"
    )


async def test_the_id_can_be_recovered_from_the_landing_page_body():
    """Some short links land without the id in the URL but with it in the HTML."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"itemId":"1081966784098","x":1}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await resolve_item_id("https://m.tb.cn/h.zzz", client) == "1081966784098"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "这不是链接",
        "随便一句话没有数字",
        "12345",  # too short to be an item id
        "https://example.com/nothing/here",  # a link, but not goofish and no id
    ],
)
async def test_unparseable_input_raises_a_value_error(text):
    """A ValueError is what the endpoint turns into a readable 400. Anything
    that escapes as another exception type becomes a 500.
    """
    with pytest.raises(ValueError):
        await resolve_item_id(text, no_network())


async def test_a_dead_link_is_a_value_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        await resolve_item_id("https://m.tb.cn/h.dead", client)


async def test_a_link_that_resolves_to_nothing_useful_is_a_value_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no ids here</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        await resolve_item_id("https://m.tb.cn/h.empty", client)


async def test_a_longer_number_is_not_truncated():
    """The boundary guards stop a 20-digit tracking number being sliced into a
    plausible-looking item id.
    """
    assert await resolve_item_id("12345678901234567890", no_network()) == "12345678901234567890"
