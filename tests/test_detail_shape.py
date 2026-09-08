"""Extraction from the REAL item detail payload.

`tests/fixtures/detail_real.json` was captured live on 2026-09-04 and trimmed
to the paths the collector reads. The nesting and key names are ground truth,
not candidates.

The detail route matters beyond watchlist tracking: it states as FACTS what a
search row can only guess (成色, 包邮) or omit entirely (publish time, want and
view counts, the full image list, and the item status that tells us a watched
listing is gone).
"""

import json
import pathlib

import pytest

from app.collector import base
from app.collector.mtop import detail_status, flatten_detail, flatten_seller

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def payload() -> dict:
    body = json.loads((FIXTURES / "detail_real.json").read_text())
    assert body["ret"][0].startswith("SUCCESS")
    return body["data"]


@pytest.fixture(scope="module")
def item(payload) -> base.RawItem:
    return base.normalize_item(
        flatten_detail(payload["itemDO"], payload["sellerDO"], "fallback"), source="detail"
    )


@pytest.fixture(scope="module")
def seller(payload) -> base.RawSeller:
    return base.normalize_seller(
        flatten_seller(payload["sellerDO"]),
        seller_id=str(payload["sellerDO"]["sellerId"]),
        source="detail",
    )


# --------------------------------------------------------------------------- #
# Item
# --------------------------------------------------------------------------- #


def test_detail_parses_into_a_raw_item(item):
    assert item.item_id == "1079129727205"
    assert item.title == "苹果15游戏机"
    assert item.price_cents == 72000
    assert item.source == "detail"


def test_price_comes_from_sold_price_not_original_price(payload):
    """`originalPrice` is "0" on this listing. Reading it would report a free
    phone; the same trap as `oriPrice` in the search row.
    """
    assert payload["itemDO"]["originalPrice"] == "0"
    item = base.normalize_item(
        flatten_detail(payload["itemDO"], payload["sellerDO"], "x"), source="detail"
    )
    assert item.price_cents == 72000


def test_detail_supplies_the_three_fields_search_cannot(item):
    """Publish time, want count and view count are absent from a search row and
    present here — which is why a watched item is tracked through detail.
    """
    assert item.publish_time is not None
    assert item.publish_time.year == 2026
    assert item.want_count == 0, "zero is a value, not absence"
    assert item.view_count == 3
    assert "publish_time" not in item.missing_fields


def test_the_full_image_list_replaces_the_lone_cover(item):
    """A search row carries one photo; detail carries the album, which is what
    the image-based advice needs.
    """
    assert len(item.image_urls) >= 3
    assert item.cover_url == item.image_urls[0]


def test_condition_is_a_fact_not_a_guess(item):
    """成色 is a structured cpvLabel here. Presenting it as a heuristic guess
    would understate what we know, and the filter must not tag it "推测".
    """
    assert item.condition_fact == "屏幕破损或外壳破碎"


def test_free_shipping_is_a_fact_not_a_guess(item):
    assert item.free_shipping_fact is True


def test_description_is_the_full_text(item):
    """The heuristics and the LLM advice both read this; a truncated title is
    not a substitute.
    """
    assert item.description is not None
    assert len(item.description) > len(item.title)
    assert "电池87" in item.description


# --------------------------------------------------------------------------- #
# Status — how a watched item is detected as gone
# --------------------------------------------------------------------------- #


def test_online_status_is_recognised(item):
    assert item.status == "on_sale"


@pytest.mark.parametrize(
    ("raw", "text", "expected"),
    [
        ("0", "在线", "on_sale"),
        ("0", "", "on_sale"),
        ("", "在线", "on_sale"),
        ("1", "已售出", "sold"),
        ("1", "交易成功", "sold"),
        ("2", "已下架", "removed"),
        ("9", "未知状态", "removed"),
        ("", "", "removed"),
    ],
)
def test_status_mapping_errs_toward_gone(raw, text, expected):
    """Only the ONLINE value has been observed live; the sold and delisted
    strings here are guesses at the wording.

    Two things are pinned regardless of the wording: an unfamiliar status
    always means "no longer on sale", and that direction is deliberate — a
    watched item wrongly reported gone is noticed at once, while one wrongly
    reported on sale is silently never followed up. The sold/removed split is
    best-effort labelling only; the PRD does not claim we can tell a sale from
    a delisting.
    """
    assert detail_status({"itemStatus": raw, "itemStatusStr": text}) == expected


# --------------------------------------------------------------------------- #
# Seller — the profile arrives with the item, not from a separate endpoint
# --------------------------------------------------------------------------- #


def test_seller_profile_comes_from_the_same_response(seller):
    assert seller.seller_id == "2218219939144"
    assert seller.nick == "小顾数码"
    assert seller.source == "detail"


def test_every_seller_judgement_field_is_populated(seller):
    """These are what the seller filters and the LLM item advice read. All of
    them arrive free with the item detail; a separate profile endpoint would be
    a second request for less.
    """
    assert seller.credit_level == 5
    assert seller.positive_rate == 97.0
    assert seller.sold_count == 131
    assert seller.account_age_days == 795
    assert seller.listing_count == 189
    assert seller.reply_rate == 100.0
    assert seller.verified is True
    assert seller.review_count == 61, "40 good + 1 bad + 20 default"


def test_the_upstream_gives_a_seller_LEVEL_and_never_a_SCORE(payload):
    """Why `Seller.credit_score` was DELETED in P5 rather than repaired.

    `SELLER_FIELD_MAP` used to look for `creditScore` / `score` / `sesameScore`
    and found none of the three, so the column was NULL on all 253 rows of the
    real database while `credit_level` was populated on every row whose profile
    had actually been fetched. The whole credit block upstream is one key:

        sellerDO.idleFishCreditTag.trackParams == {"sellerLevel": "5"}

    goofish hands out a LEVEL, not a score. Anyone reading `sellerLevel` and
    reaching for a `credit_score` column again should read this test first —
    the rationale also lives in `spec/backend/collector-guidelines.md`.
    """
    assert payload["sellerDO"]["idleFishCreditTag"]["trackParams"] == {"sellerLevel": "5"}
    blob = json.dumps(payload["sellerDO"], ensure_ascii=False)
    for candidate in ("creditScore", "sesameScore", '"score"'):
        assert candidate not in blob, candidate
    assert "credit_score" not in base.SELLER_FIELD_MAP


def test_percent_strings_are_parsed_as_numbers(payload):
    assert payload["sellerDO"]["newGoodRatioRate"] == "97%"
    assert payload["sellerDO"]["replyRatio24h"] == "100%"


def test_seller_id_is_numeric_here_unlike_in_search(seller):
    """The two routes use different id spaces for the same seller: search
    returns an opaque token, detail a numeric id, and `needDecryptKeys` was
    empty so they are not convertible. The Seller row is therefore keyed on
    whichever id the item already points at.
    """
    assert seller.seller_id.isdigit()


def test_a_missing_seller_block_is_not_an_error():
    """Guest-mode and browser-scraped routes have no sellerDO at all."""
    flat = flatten_seller({})
    assert flat == {}
    empty = base.normalize_seller(flat, seller_id="x", source="detail")
    assert empty.credit_level is None
    assert len(empty.missing_fields) == len(base.SELLER_FIELD_MAP)


def test_flatten_detail_survives_a_truncated_payload():
    with pytest.raises(base.ParseError):
        base.normalize_item(flatten_detail({}, {}, "i1"), source="detail")


def test_a_deleted_listing_is_gone_not_a_generic_failure():
    """Measured 2026-09-05 against two genuinely deleted listings. The marker
    list had three GUESSED names and the real one matched none of them --
    note it is ITEM_DEL_NOT_FOUND, so "ITEM_NOT_FOUND" is not a substring.

    Classifying it as a plain CollectorError meant the watchlist recorded a
    failure instead of marking the entry gone, so the `gone` notification
    could never fire for a deleted item, and a pasted link to one answered
    502 "could not fetch" rather than 404 "no longer exists".
    """
    from app.collector.base import ItemGoneError
    from app.collector.mtop import classify_ret

    with pytest.raises(ItemGoneError):
        classify_ret(["FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!"])


def test_the_observed_online_status_still_maps_to_on_sale():
    """Guards the values that ARE measured, so a marker change cannot quietly
    reclassify a live listing.
    """
    from app.collector.mtop import detail_status

    assert detail_status({"itemStatus": 0, "itemStatusStr": "在线"}) == "on_sale"
    # Measured 2026-09-05 on a real listing that had been taken down.
    assert detail_status({"itemStatus": -2, "itemStatusStr": "已下架"}) == "removed"
