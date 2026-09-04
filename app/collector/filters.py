"""Local filtering and heuristics.

Two rules shape everything here:

1. **Order matters for safety, not speed.** Zero-cost local checks run first so
   that the seller-profile request — an extra HTTP call per candidate — only
   happens for items that already passed everything else. Reversing the order
   turns one request per cycle into N and walks straight into risk control.

2. **A filter whose data is unavailable lets the item through, and says so.**
   For a monitoring tool a missed deal is unrecoverable while an extra push is
   one glance to dismiss. Every waived check is recorded in `unverified` and
   must be surfaced in the notification and the hit list.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collector.base import RawItem, RawSeller

log = logging.getLogger(__name__)

# Heuristic vocabularies. These judge seller-written prose, so they are
# genuinely approximate — results are always tagged as heuristic.
_FREE_SHIPPING_HINTS = ("包邮", "免邮", "包快递", "顺丰包邮", "邮费我出")
_PAID_SHIPPING_HINTS = ("不包邮", "运费自理", "到付", "邮费自付", "运费另算")
_CONDITION_HINTS: dict[str, tuple[str, ...]] = {
    "全新": ("全新", "未拆封", "未开封", "未使用", "全新未拆"),
    "几乎全新": ("几乎全新", "99新", "999新", "9成9新", "无痕"),
    "轻微使用": ("95新", "9成新", "9新", "轻微使用", "轻微磕碰"),
    "明显使用": ("8成新", "8新", "使用痕迹", "磕碰", "划痕", "瑕疵"),
}


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    """Verdict for one item against one rule."""

    passed: bool
    rejected_by: str | None = None
    unverified: tuple[str, ...] = ()

    @property
    def needs_seller_profile(self) -> bool:
        return "seller_profile_pending" in self.unverified


@dataclass(frozen=True, slots=True)
class RuleFilters:
    """The subset of a Monitor row that filtering needs.

    A plain value object rather than the ORM model: filters are pure logic and
    must stay testable without a database.
    """

    exclude_words: str = ""
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    published_within_hours: int | None = None
    region: str | None = None
    condition: str | None = None
    free_shipping: bool | None = None
    min_seller_credit: int | None = None
    exclude_shop: bool = False


def _longest_hint_match(text: str, groups: dict[str, tuple[str, ...]]) -> str | None:
    """The label whose LONGEST matching hint wins.

    Substring collisions are the norm in this vocabulary: "全新" is inside
    "几乎全新", and "包邮" is inside "不包邮". Matching by first-hit would
    classify a 99-new phone as brand new and a shipping-excluded listing as
    free shipping — both wrong, and both invisible without this rule. Longest
    match removes the dependency on dict ordering entirely.
    """
    best_label: str | None = None
    best_len = 0
    for label, hints in groups.items():
        for hint in hints:
            if hint in text and len(hint) > best_len:
                best_label, best_len = label, len(hint)
    return best_label


def guess_free_shipping(item: RawItem) -> bool | None:
    """None when the text says nothing either way — that is not False."""
    text = f"{item.title} {item.description or ''}"
    label = _longest_hint_match(text, {"paid": _PAID_SHIPPING_HINTS, "free": _FREE_SHIPPING_HINTS})
    if label is None:
        return None
    return label == "free"


def guess_condition(item: RawItem) -> str | None:
    text = f"{item.title} {item.description or ''}"
    return _longest_hint_match(text, _CONDITION_HINTS)


def in_price_range(price_cents: int, rule: RuleFilters) -> bool:
    if rule.price_min_cents is not None and price_cents < rule.price_min_cents:
        return False
    if rule.price_max_cents is not None and price_cents > rule.price_max_cents:
        return False
    return True


def apply_local(item: RawItem, rule: RuleFilters, *, now: datetime | None = None) -> FilterOutcome:
    """Every check that needs no extra network call.

    A `seller_profile_pending` marker in `unverified` means the caller should
    fetch the profile and re-check via `apply_seller`.
    """
    now = now or datetime.now(UTC)
    unverified: list[str] = []

    # Price is the one field whose absence disqualifies: a listing we cannot
    # price cannot be matched against a budget, and the budget is the point.
    if not in_price_range(item.price_cents, rule):
        return FilterOutcome(False, rejected_by="price")

    words = [w for w in rule.exclude_words.split() if w]
    haystack = f"{item.title} {item.description or ''}"
    for word in words:
        if word in haystack:
            return FilterOutcome(False, rejected_by=f"exclude_word:{word}")

    if rule.published_within_hours is not None:
        if item.publish_time is None:
            unverified.append("published_within_hours")
        elif item.publish_time < now - timedelta(hours=rule.published_within_hours):
            return FilterOutcome(False, rejected_by="published_within_hours")

    if rule.region:
        if not item.region:
            unverified.append("region")
        elif rule.region not in item.region:
            return FilterOutcome(False, rejected_by="region")

    if rule.condition:
        guessed = guess_condition(item)
        if guessed is None:
            unverified.append("condition")
        elif guessed != rule.condition:
            return FilterOutcome(False, rejected_by="condition")

    if rule.free_shipping is not None:
        guessed_shipping = guess_free_shipping(item)
        if guessed_shipping is None:
            unverified.append("free_shipping")
        elif guessed_shipping != rule.free_shipping:
            return FilterOutcome(False, rejected_by="free_shipping")

    # exclude_shop can often be settled here: the search row itself carries a
    # merchant label (see collector/mtop.py _guess_is_shop), which costs no
    # extra request. Only an unknown verdict is deferred to the profile.
    if rule.exclude_shop:
        if item.seller_is_shop is True:
            return FilterOutcome(False, rejected_by="exclude_shop")
        if item.seller_is_shop is None:
            unverified.append("seller_profile_pending")

    if rule.min_seller_credit is not None:
        # Credit level is never present in a search row.
        if "seller_profile_pending" not in unverified:
            unverified.append("seller_profile_pending")

    return FilterOutcome(True, unverified=tuple(unverified))


def apply_seller(
    seller: RawSeller | None, rule: RuleFilters, prior: FilterOutcome
) -> FilterOutcome:
    """Re-check the seller-dependent conditions once the profile is known.

    `seller=None` means the profile could not be fetched: waive, tag, pass.
    """
    unverified = [u for u in prior.unverified if u != "seller_profile_pending"]

    if seller is None:
        if rule.min_seller_credit is not None:
            unverified.append("min_seller_credit")
        if rule.exclude_shop:
            unverified.append("exclude_shop")
        return FilterOutcome(True, unverified=tuple(unverified))

    if rule.min_seller_credit is not None:
        if seller.credit_level is None:
            unverified.append("min_seller_credit")
        elif seller.credit_level < rule.min_seller_credit:
            return FilterOutcome(False, rejected_by="min_seller_credit")

    if rule.exclude_shop:
        if seller.is_shop is None:
            unverified.append("exclude_shop")
        elif seller.is_shop:
            return FilterOutcome(False, rejected_by="exclude_shop")

    return FilterOutcome(True, unverified=tuple(unverified))


def apply_seller_from_item(item: RawItem, rule: RuleFilters, prior: FilterOutcome) -> FilterOutcome:
    """Settle deferred seller checks using only what the item already carries.

    Used when profile fetching is disabled: an item whose row already said
    "merchant" must still be rejected, and one that said nothing must still be
    labelled rather than silently passed as compliant.
    """
    if item.seller_is_shop is not None or not rule.min_seller_credit:
        stub = RawSeller(
            seller_id=item.seller_id,
            nick=item.seller_nick,
            source=item.source,
            is_shop=item.seller_is_shop,
            review_count=item.seller_review_count,
            positive_rate=item.seller_positive_rate,
        )
        return apply_seller(stub, rule, prior)
    return apply_seller(None, rule, prior)


UNVERIFIED_LABELS: dict[str, str] = {
    "published_within_hours": "发布时间未知",
    "region": "地区未知",
    "condition": "成色为描述推测",
    "free_shipping": "是否包邮未知",
    "min_seller_credit": "卖家信用未知",
    "exclude_shop": "是否商家未知",
}


def describe_unverified(unverified: tuple[str, ...]) -> list[str]:
    """Human-readable tags for the notification body and the hit list.

    Without these, "conservatively let it through" degrades into silently
    pushing items that do not actually meet the stated filters.
    """
    return [UNVERIFIED_LABELS.get(u, u) for u in unverified if u != "seller_profile_pending"]
