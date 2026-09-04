"""Fixed-input/fixed-output guards for the mtop signature and ret classifier.

A wrong signature and a risk-control block are indistinguishable from the
response alone — both come back as a failure with no useful detail. Only a
known-answer test can tell them apart, which is why these values are frozen.
"""

import hashlib

import pytest

from app.collector.base import (
    ChallengeError,
    CollectorError,
    ItemGoneError,
    TransientCollectorError,
)
from app.collector.mtop import APP_KEY, TokenStaleError, _sign, classify_ret


def test_signature_matches_the_documented_scheme():
    token, t, data = "abc123", "1700000000000", '{"keyword":"iPhone"}'
    expected = hashlib.md5(f"{token}&{t}&{APP_KEY}&{data}".encode()).hexdigest()
    assert _sign(token, t, data) == expected
    # Golden value. The assertion above verifies the formula independently;
    # this one pins the output so a later refactor cannot drift silently.
    assert _sign("abc123", "1700000000000", '{"keyword":"iPhone"}') == (
        "9ae5a175699ffe0caed6ec7bfdb387f8"
    )


def test_signature_depends_on_every_input():
    base_args = ("tok", "1700000000000", '{"a":1}')
    sig = _sign(*base_args)
    assert _sign("tok2", base_args[1], base_args[2]) != sig
    assert _sign(base_args[0], "1700000000001", base_args[2]) != sig
    assert _sign(base_args[0], base_args[1], '{"a":2}') != sig


def test_payload_whitespace_changes_the_signature():
    """Why the data string must be serialised once and reused: a re-dump with
    different separators signs a different string, which upstream rejects in a
    way that looks exactly like risk control.
    """
    assert _sign("t", "1", '{"a":1}') != _sign("t", "1", '{"a": 1}')


@pytest.mark.parametrize(
    ("ret", "expected"),
    [
        (["SUCCESS::调用成功"], None),
        (["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"], ChallengeError),
        # Token staleness is NOT a challenge: the response carries a fresh
        # token, so it is recoverable without a human. This file used to assert
        # ChallengeError here, and that expectation WAS the bug — it
        # auto-disabled every rule a few hours after startup. See
        # tests/test_token_rotation.py.
        (["FAIL_SYS_TOKEN_EMPTY::令牌为空"], TokenStaleError),
        (["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], TokenStaleError),
        (["FAIL_SYS_ILLEGAL_ACCESS::非法请求"], ChallengeError),
        (["FAIL_SYS_USER_VALIDATE"], ChallengeError),
        (["FAIL_SYS_TRAFFIC_LIMIT::限流"], TransientCollectorError),
        (["FAIL_SYS_API_NOT_FOUNDED::请求API不存在"], CollectorError),
        (["FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"], ItemGoneError),
        ([], CollectorError),
        (None, CollectorError),
    ],
)
def test_ret_classification(ret, expected):
    if expected is None:
        assert classify_ret(ret) is None
        return
    with pytest.raises(expected):
        classify_ret(ret)


def test_challenge_is_not_classified_as_transient():
    """The distinction that keeps the tool from backing off forever while
    telling the user "throttled" — observed live: RGV587 arrives with an
    x5secdata cookie and needs human verification, not a retry.
    """
    with pytest.raises(ChallengeError) as exc:
        classify_ret(["RGV587_ERROR::SM::哎哟喂"])
    assert not isinstance(exc.value, TransientCollectorError)
