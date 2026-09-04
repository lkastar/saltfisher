"""Token rotation and the retry that depends on it.

`_m_h5_tk` is short-lived, and the response reporting it stale is the one that
carries the replacement. Measured on the live API: without absorbing that
cookie the stored token goes stale within hours, every call fails, and — under
the original classification — every rule auto-disables and asks the user to
re-import cookies for a problem the server had already solved.

None of the earlier live verification caught this: it all ran inside one
token's lifetime.
"""

import time

import httpx
import pytest

from app.collector.base import ChallengeError, CollectorError
from app.collector.mtop import MtopClient, TokenStaleError, classify_ret
from app.collector.session import UpstreamSession

EXPIRED = {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}}
CHALLENGED = {"ret": ["RGV587_ERROR::SM::哎哟喂"], "data": {"url": "https://punish"}}
OK = {
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "resultInfo": {"hasNextPage": False},
        "resultList": [
            {
                "data": {
                    "item": {
                        "main": {
                            "exContent": {
                                "itemId": "1",
                                "title": "iPhone 15",
                                "detailParams": {"soldPrice": "2619", "title": "iPhone 15 长描述"},
                                "picUrl": "https://cdn/a.jpg",
                                "userNickName": "老王",
                            },
                            "clickParam": {"args": {"seller_id": "opaque-seller-id"}},
                        }
                    }
                }
            }
        ],
    },
}


def session_with(token: str = "oldtoken_1") -> UpstreamSession:
    s = UpstreamSession()
    s.adopt({"_m_h5_tk": token, "cookie2": "x"}, origin="imported")
    return s


def transport(responses: list[dict], new_token: str | None = "freshtoken_2"):
    """Serve the given bodies in order, optionally rotating the token cookie."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = responses[min(len(calls) - 1, len(responses) - 1)]
        headers = {}
        if new_token:
            headers["set-cookie"] = f"_m_h5_tk={new_token}; Path=/"
        return httpx.Response(200, json=body, headers=headers)

    return httpx.MockTransport(handler), calls


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_token_staleness_is_its_own_error_not_a_challenge():
    """The distinction the whole fix rests on: stale is recoverable without a
    human, challenged is not.
    """
    with pytest.raises(TokenStaleError):
        classify_ret(["FAIL_SYS_TOKEN_EXOIRED::令牌过期"])
    with pytest.raises(TokenStaleError):
        classify_ret(["FAIL_SYS_TOKEN_EMPTY::令牌为空"])
    with pytest.raises(ChallengeError) as exc:
        classify_ret(["RGV587_ERROR::SM::x"])
    assert not isinstance(exc.value, TokenStaleError)


def test_upstream_typo_and_correct_spelling_both_match():
    """The real error reads EXOIRED. Matching only EXPIRED would miss it."""
    for ret in ("FAIL_SYS_TOKEN_EXOIRED::x", "FAIL_SYS_TOKEN_EXPIRED::x"):
        with pytest.raises(TokenStaleError):
            classify_ret([ret])


# --------------------------------------------------------------------------- #
# Session absorption
# --------------------------------------------------------------------------- #


def test_refresh_absorbs_a_rotated_token_without_touching_health():
    s = session_with()
    s.refresh({"_m_h5_tk": "freshtoken_2"})
    assert s.token == "freshtoken"
    assert s.origin == "imported", "rotation is not a new session"
    assert s.usable and not s.needs_verification


def test_refresh_ignores_an_empty_cookie_jar():
    s = session_with()
    s.refresh({})
    assert s.token == "oldtoken"


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_token_is_retried_once_and_succeeds():
    tr, calls = transport([EXPIRED, OK])
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)

    items = await client.search("iPhone 15", page=1, rows=5, now_ms=str(int(time.time() * 1000)))
    assert len(items) == 1
    assert len(calls) == 2, "expected exactly one retry"
    assert sess.token == "freshtoken"
    assert sess.usable and not sess.needs_verification


@pytest.mark.asyncio
async def test_the_retry_is_signed_with_the_new_token():
    """Retrying with the stale token would fail identically — the point of the
    retry is that the signature uses the token that just arrived.
    """
    tr, calls = transport([EXPIRED, OK])
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)
    await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")

    first_sign = calls[0].url.params["sign"]
    second_sign = calls[1].url.params["sign"]
    assert first_sign != second_sign
    assert calls[0].url.params["t"] != calls[1].url.params["t"]


@pytest.mark.asyncio
async def test_a_second_failure_escalates_to_needing_verification():
    """If the refreshed token also fails, the session really is unusable."""
    tr, calls = transport([EXPIRED, EXPIRED])
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)

    with pytest.raises(ChallengeError):
        await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")
    assert len(calls) == 2, "must not retry forever"
    assert sess.needs_verification


@pytest.mark.asyncio
async def test_a_challenge_is_not_retried():
    """Retrying a risk-control challenge just adds traffic while blocked."""
    tr, calls = transport([CHALLENGED, OK], new_token=None)
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)

    with pytest.raises(ChallengeError):
        await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")
    assert len(calls) == 1
    assert sess.needs_verification


@pytest.mark.asyncio
async def test_an_auxiliary_call_degrades_instead_of_escalating():
    """The seller profile is auxiliary: two token failures there report
    unavailability rather than declaring the shared session dead.
    """
    tr, calls = transport([EXPIRED, EXPIRED])
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)

    with pytest.raises(CollectorError) as exc:
        await client.fetch_seller("s1", now_ms="1700000000000")
    assert not isinstance(exc.value, ChallengeError)
    assert not sess.needs_verification


@pytest.mark.asyncio
async def test_a_successful_call_still_absorbs_the_rotated_cookie():
    """Rotation happens on success too, so the stored token must keep up even
    when nothing ever fails.
    """
    tr, calls = transport([OK])
    sess = session_with()
    client = MtopClient(sess)
    client._client = httpx.AsyncClient(transport=tr)

    await client.search("iPhone 15", page=1, rows=5, now_ms="1700000000000")
    assert len(calls) == 1
    assert sess.token == "freshtoken"
