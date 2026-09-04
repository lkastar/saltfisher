"""Normalisation from upstream payloads to RawItem / RawSeller.

Uses the labelled synthetic fixtures (tests/fixtures/README.md explains why
they are synthetic). What is genuinely under test here is our own logic:
price and timestamp parsing, candidate-key resolution, missing-field
bookkeeping, and the refusal to accept an unpriceable row.
"""

import json
import pathlib
from datetime import UTC, datetime

import pytest

from app.collector import base
from app.collector.base import ParseError, RawItem

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    body = json.loads((FIXTURES / "search_synthetic.json").read_text())
    return body["data"]["resultList"]


# --------------------------------------------------------------------------- #
# parse_price_cents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("¥3,200", 320000),
        ("￥3200", 320000),
        ("39.00", 3900),
        ("39.9", 3990),
        ("2999", 299900),
        (2999, 299900),
        (39.5, 3950),
        ("1200元", 120000),
        (" 88.88 ", 8888),
        ("0", 0),
    ],
)
def test_price_formats(raw, cents):
    assert base.parse_price_cents(raw) == cents


@pytest.mark.parametrize("raw", ["面议", "详聊", "", None, "abc", "-5", True])
def test_unpriceable_values_raise(raw):
    with pytest.raises(ParseError):
        base.parse_price_cents(raw)


def test_rich_text_candidate_is_skipped_not_returned():
    """`price` is a clean string in some payloads and a rich-text segment list
    in the live search response. The scalar guard must fall through to the next
    candidate instead of handing the list to the price parser, which would drop
    an otherwise perfectly good row.
    """
    item = base.normalize_item(
        {
            "itemId": "1",
            "title": "t",
            "userId": "u",
            "price": [{"text": "¥"}, {"text": "2619"}],
            "soldPrice": "2619",
        },
        source="mtop",
    )
    assert item.price_cents == 261900


def test_scalar_guard_does_not_hide_a_genuinely_missing_field():
    with pytest.raises(ParseError):
        base.normalize_item(
            {"itemId": "1", "title": "t", "userId": "u", "price": [{"text": "¥"}]},
            source="mtop",
        )


def test_no_already_in_cents_heuristic():
    """A large number is still yuan. Guessing the unit is how money code
    silently multiplies every price by 100.
    """
    assert base.parse_price_cents(320000) == 32000000


# --------------------------------------------------------------------------- #
# parse_timestamp
# --------------------------------------------------------------------------- #


def test_timestamp_accepts_millis_seconds_and_iso():
    millis = base.parse_timestamp(1788508800000)
    seconds = base.parse_timestamp(1788508800)
    assert millis == seconds
    assert millis is not None and millis.tzinfo is UTC

    iso = base.parse_timestamp("2026-09-01T10:00:00+08:00")
    assert iso == datetime(2026, 9, 1, 2, 0, tzinfo=UTC)

    naive = base.parse_timestamp("2026-09-01T10:00:00")
    assert naive is not None and naive.tzinfo is UTC


@pytest.mark.parametrize("raw", [None, "", "not a date", "9" * 30])
def test_unparseable_timestamp_returns_none_instead_of_raising(raw):
    """A missing publish time degrades one filter; it must not drop the item."""
    assert base.parse_timestamp(raw) is None


# --------------------------------------------------------------------------- #
# normalize_item
# --------------------------------------------------------------------------- #


def test_first_candidate_key_set_is_resolved(rows):
    item = base.normalize_item(rows[0], source="mtop")
    assert item == RawItem(
        item_id="801234567890",
        title="iPhone 15 128G 蓝色 国行",
        price_cents=320000,
        seller_id="9001",
        seller_nick="老王",
        source="mtop",
        description="自用一年，几乎全新无痕，包邮",
        cover_url="https://cdn/x1.jpg",
        image_urls=("https://cdn/x1.jpg", "https://cdn/x2.jpg"),
        region="上海 浦东",
        seller_avatar_url="https://cdn/a1.jpg",
        publish_time=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
        want_count=12,
        view_count=340,
        status="on_sale",
        missing_fields=(),
    )


def test_alternate_candidate_keys_resolve_the_same_fields(rows):
    """Row 2 uses id/name/soldPrice/sellerId/nick instead of the primary keys."""
    item = base.normalize_item(rows[1], source="mtop")
    assert (item.item_id, item.title, item.price_cents) == (
        "801234567891",
        "iPhone 15 Pro 手机壳 全新未拆封",
        3900,
    )
    assert (item.seller_id, item.seller_nick) == ("9002", "小李")
    # only a cover photo exists on this row; it still counts as an image
    assert item.image_urls == ("https://cdn/y1.jpg",)
    assert item.cover_url == "https://cdn/y1.jpg"


def test_absent_optional_fields_are_recorded_not_invented(rows):
    item = base.normalize_item(rows[2], source="mtop")
    assert item.want_count is None and item.view_count is None
    assert "want_count" in item.missing_fields
    assert "view_count" in item.missing_fields
    assert item.publish_time is None


def test_row_without_a_usable_price_is_rejected(rows):
    with pytest.raises(ParseError):
        base.normalize_item(rows[3], source="mtop")


def test_missing_required_field_names_what_is_missing():
    with pytest.raises(ParseError) as exc:
        base.normalize_item({"title": "x", "price": "1"}, source="mtop")
    assert "item_id" in str(exc.value) and "seller_id" in str(exc.value)


def test_image_list_is_deduped_and_capped():
    from app.config import settings

    item = base.normalize_item(
        {
            "itemId": "1",
            "title": "t",
            "price": "1",
            "userId": "u",
            "picUrl": "https://cdn/a.jpg",
            "images": ["https://cdn/a.jpg"] + [f"https://cdn/{i}.jpg" for i in range(10)],
        },
        source="mtop",
    )
    assert len(item.image_urls) == settings.max_image_urls
    assert len(set(item.image_urls)) == len(item.image_urls)


def test_single_image_string_becomes_a_one_tuple():
    item = base.normalize_item(
        {"itemId": "1", "title": "t", "price": "1", "userId": "u", "images": "https://cdn/a.jpg"},
        source="mtop",
    )
    assert item.image_urls == ("https://cdn/a.jpg",)


def test_raw_item_is_immutable():
    item = base.normalize_item(
        {"itemId": "1", "title": "t", "price": "1", "userId": "u"}, source="mtop"
    )
    with pytest.raises(AttributeError):
        item.price_cents = 999  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# normalize_seller
# --------------------------------------------------------------------------- #


def test_seller_none_is_distinguishable_from_zero():
    seller = base.normalize_seller({"nick": "老王", "soldCount": 0}, "9001", source="mtop")
    assert seller.sold_count == 0
    assert seller.credit_level is None
    assert "credit_level" in seller.missing_fields
    assert "sold_count" not in seller.missing_fields


def test_seller_bool_and_percent_coercion():
    seller = base.normalize_seller(
        {"nick": "铺子", "isShop": 1, "realNameVerified": "true", "replyRate": "98%"},
        "9002",
        source="browser",
    )
    assert seller.is_shop is True
    assert seller.verified is True
    assert seller.reply_rate == 98.0


def test_seller_never_raises_on_an_empty_profile():
    """Guest mode legitimately sees almost nothing."""
    seller = base.normalize_seller({}, "9003", source="mtop")
    assert seller.seller_id == "9003"
    assert seller.nick == ""
    assert len(seller.missing_fields) == len(base.SELLER_FIELD_MAP)
