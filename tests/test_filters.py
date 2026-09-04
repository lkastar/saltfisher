"""Filter matrix: pass / reject / data-unavailable for every condition.

The third column is the one that matters. The product decision is
"conservatively let it through, but label it" — a missed deal is unrecoverable
while an extra push is one glance to dismiss. These tests are what stop that
policy from silently becoming "reject when unsure", which would make the tool
look like it never finds anything.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.collector import filters
from app.collector.base import RawItem, RawSeller
from app.collector.filters import RuleFilters

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def item(**kw) -> RawItem:
    base_kw = dict(
        item_id="1",
        title="iPhone 15 128G",
        price_cents=300000,
        seller_id="9001",
        seller_nick="老王",
        source="mtop",
    )
    return RawItem(**{**base_kw, **kw})


def seller(**kw) -> RawSeller:
    return RawSeller(**{**dict(seller_id="9001", nick="老王", source="mtop"), **kw})


# --------------------------------------------------------------------------- #
# price — the one filter with no "unavailable" branch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("price", "lo", "hi", "passes"),
    [
        (300000, 200000, 350000, True),
        (300000, None, 350000, True),
        (300000, 200000, None, True),
        (100000, 200000, 350000, False),
        (400000, 200000, 350000, False),
        (200000, 200000, 350000, True),  # inclusive lower bound
        (350000, 200000, 350000, True),  # inclusive upper bound
    ],
)
def test_price_range(price, lo, hi, passes):
    out = filters.apply_local(
        item(price_cents=price),
        RuleFilters(price_min_cents=lo, price_max_cents=hi),
        now=NOW,
    )
    assert out.passed is passes
    if not passes:
        assert out.rejected_by == "price"


# --------------------------------------------------------------------------- #
# exclude words
# --------------------------------------------------------------------------- #


def test_exclude_word_matches_title_or_description():
    rule = RuleFilters(exclude_words="壳 膜 求购")
    assert not filters.apply_local(item(title="iPhone 15 手机壳"), rule, now=NOW).passed
    assert not filters.apply_local(
        item(description="顺便出个钢化膜"), RuleFilters(exclude_words="钢化膜"), now=NOW
    ).passed
    assert filters.apply_local(item(), rule, now=NOW).passed


def test_empty_exclude_words_excludes_nothing():
    assert filters.apply_local(item(), RuleFilters(exclude_words="   "), now=NOW).passed


# --------------------------------------------------------------------------- #
# publish window / region — reject vs waive
# --------------------------------------------------------------------------- #


def test_publish_window_pass_reject_and_waive():
    rule = RuleFilters(published_within_hours=48)
    fresh = filters.apply_local(item(publish_time=NOW - timedelta(hours=10)), rule, now=NOW)
    assert fresh.passed and fresh.unverified == ()

    stale = filters.apply_local(item(publish_time=NOW - timedelta(hours=72)), rule, now=NOW)
    assert not stale.passed and stale.rejected_by == "published_within_hours"

    unknown = filters.apply_local(item(publish_time=None), rule, now=NOW)
    assert unknown.passed and "published_within_hours" in unknown.unverified


def test_region_pass_reject_and_waive():
    rule = RuleFilters(region="上海")
    assert filters.apply_local(item(region="上海 浦东"), rule, now=NOW).passed
    assert not filters.apply_local(item(region="北京"), rule, now=NOW).passed
    waived = filters.apply_local(item(region=None), rule, now=NOW)
    assert waived.passed and "region" in waived.unverified


# --------------------------------------------------------------------------- #
# heuristics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("全新未拆封", "全新"),
        ("几乎全新无痕", "几乎全新"),
        ("99新", "几乎全新"),
        ("9成新轻微使用", "轻微使用"),
        ("8成新有磕碰", "明显使用"),
        ("iPhone 15 128G", None),
    ],
)
def test_condition_heuristic(text, expected):
    assert filters.guess_condition(item(title=text)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("包邮出", True),
        ("免邮", True),
        ("不包邮", False),
        ("运费自理", False),
        ("到付", False),
        ("iPhone 15", None),
    ],
)
def test_free_shipping_heuristic(text, expected):
    assert filters.guess_free_shipping(item(description=text)) is expected


def test_negative_shipping_hint_wins_over_positive():
    """ "包邮" appears inside "不包邮" — the negative must be checked first."""
    assert filters.guess_free_shipping(item(description="不包邮，介意勿拍")) is False


def test_condition_and_shipping_waive_when_text_is_silent():
    out = filters.apply_local(
        item(title="iPhone 15", description=None),
        RuleFilters(condition="全新", free_shipping=True),
        now=NOW,
    )
    assert out.passed
    assert "condition" in out.unverified and "free_shipping" in out.unverified


# --------------------------------------------------------------------------- #
# seller-dependent conditions
# --------------------------------------------------------------------------- #


def test_seller_conditions_are_deferred_not_evaluated_locally():
    """Local pass must flag that a profile request is still needed, so the
    pipeline fetches it only for survivors — one request per cycle, not N.
    """
    out = filters.apply_local(item(), RuleFilters(min_seller_credit=3), now=NOW)
    assert out.passed and out.needs_seller_profile


def test_seller_credit_pass_reject_and_waive():
    rule = RuleFilters(min_seller_credit=3)
    prior = filters.apply_local(item(), rule, now=NOW)

    assert filters.apply_seller(seller(credit_level=4), rule, prior).passed
    assert not filters.apply_seller(seller(credit_level=1), rule, prior).passed

    unknown = filters.apply_seller(seller(credit_level=None), rule, prior)
    assert unknown.passed and "min_seller_credit" in unknown.unverified

    unfetched = filters.apply_seller(None, rule, prior)
    assert unfetched.passed and "min_seller_credit" in unfetched.unverified


def test_exclude_shop_pass_reject_and_waive():
    rule = RuleFilters(exclude_shop=True)
    prior = filters.apply_local(item(), rule, now=NOW)

    assert filters.apply_seller(seller(is_shop=False), rule, prior).passed
    assert not filters.apply_seller(seller(is_shop=True), rule, prior).passed

    unknown = filters.apply_seller(seller(is_shop=None), rule, prior)
    assert unknown.passed and "exclude_shop" in unknown.unverified


def test_pending_marker_is_consumed_after_the_seller_check():
    rule = RuleFilters(min_seller_credit=3, exclude_shop=True)
    prior = filters.apply_local(item(), rule, now=NOW)
    after = filters.apply_seller(seller(credit_level=5, is_shop=False), rule, prior)
    assert "seller_profile_pending" not in after.unverified
    assert after.unverified == ()


def test_waived_checks_carry_forward_through_the_seller_stage():
    rule = RuleFilters(region="上海", min_seller_credit=3)
    prior = filters.apply_local(item(region=None), rule, now=NOW)
    after = filters.apply_seller(None, rule, prior)
    assert "region" in after.unverified and "min_seller_credit" in after.unverified


# --------------------------------------------------------------------------- #
# user-facing labels
# --------------------------------------------------------------------------- #


def test_every_waivable_filter_has_a_readable_label():
    """A waived check that cannot be explained in the push turns
    "conservatively let through" into silent mis-pushing.
    """
    waivable = {
        "published_within_hours",
        "region",
        "condition",
        "free_shipping",
        "min_seller_credit",
        "exclude_shop",
    }
    assert waivable <= set(filters.UNVERIFIED_LABELS)


def test_describe_unverified_hides_the_internal_marker():
    described = filters.describe_unverified(("region", "seller_profile_pending"))
    assert described == ["地区未知"]


def test_no_filters_configured_passes_everything_unlabelled():
    out = filters.apply_local(item(), RuleFilters(), now=NOW)
    assert out.passed and out.unverified == ()
