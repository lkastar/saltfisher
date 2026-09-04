"""Extraction from the REAL search payload.

`tests/fixtures/search_real.json` was captured live on 2026-09-04 with an
imported cookie session, then trimmed to the paths the collector reads. Unlike
the synthetic fixtures, these key names and nesting are ground truth.

Each test below pins a fact that cost a live probe to learn.
"""

import json
import pathlib

import pytest

from app.collector import base
from app.collector.mtop import _guess_is_shop, _seller_reputation, flatten_search_row

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def real_rows() -> list[dict]:
    body = json.loads((FIXTURES / "search_real.json").read_text())
    assert body["ret"][0].startswith("SUCCESS")
    return body["data"]["resultList"]


def test_every_real_row_parses(real_rows):
    """The regression this file exists for: the first implementation parsed
    0 of 10 live rows because the real payload nests fields four levels deep
    under data.item.main.exContent.
    """
    items = [base.normalize_item(flatten_search_row(r), source="mtop") for r in real_rows]
    assert len(items) == len(real_rows)
    for item in items:
        assert item.item_id.isdigit()
        assert item.title
        assert item.price_cents > 0
        assert item.seller_id


def test_price_comes_from_sold_price_not_ori_price(real_rows):
    """`oriPrice` is the struck-through ORIGINAL price and runs higher than the
    real one (¥2979 vs 2619 in this very fixture). Reading it would overstate
    every discounted listing and break the price filter.
    """
    row = next(r for r in real_rows if r["data"]["item"]["main"]["exContent"].get("oriPrice"))
    ex = row["data"]["item"]["main"]["exContent"]
    ori_cents = base.parse_price_cents(ex["oriPrice"])
    sold_cents = base.parse_price_cents(ex["detailParams"]["soldPrice"])
    assert ori_cents > sold_cents, "fixture no longer exercises the trap"

    item = base.normalize_item(flatten_search_row(row), source="mtop")
    assert item.price_cents == sold_cents
    assert item.price_cents != ori_cents


def test_rich_text_price_list_is_never_used_as_the_price(real_rows):
    """`exContent.price` is a list of rich-text segments ([{"text": "¥"},
    {"text": "2619"}]). A blind flatten let it shadow the clean value, which
    is what made every row unparseable.
    """
    flat = flatten_search_row(real_rows[0])
    assert not isinstance(flat.get("soldPrice"), list)
    assert "price" not in flat or not isinstance(flat["price"], list)


def test_long_description_is_taken_from_detail_params(real_rows):
    """`exContent.title` is the short display title; `detailParams.title`
    carries the full text the condition and shipping heuristics read.
    """
    item = base.normalize_item(flatten_search_row(real_rows[0]), source="mtop")
    assert item.description is not None
    assert len(item.description) >= len(item.title)


def test_seller_id_is_opaque_but_present(real_rows):
    """The id is an encrypted, stable token rather than a numeric user id, and
    `needDecryptKeys` came back empty — so it is usable as a grouping key as-is.
    """
    body = json.loads((FIXTURES / "search_real.json").read_text())
    assert body["data"]["needDecryptKeys"] == []
    item = base.normalize_item(flatten_search_row(real_rows[0]), source="mtop")
    assert not item.seller_id.isdigit()
    assert len(item.seller_id) > 8


def test_is_shop_is_positive_only(real_rows):
    """A missing identity label proves nothing: the live capture had a row
    nicknamed 杭州靓机汇二手机批发 — plainly a wholesaler — with an empty field.
    So unknown must stay None, never False, or exclude_shop would silently drop
    personal-looking merchants while claiming certainty.
    """
    verdicts = [_guess_is_shop(r["data"]["item"]["main"]["exContent"]) for r in real_rows]
    assert True in verdicts, "fixture should contain at least one labelled merchant"
    assert None in verdicts, "fixture should contain at least one unlabelled seller"
    assert False not in verdicts


def test_identity_label_drives_the_shop_verdict():
    assert _guess_is_shop({"userIdentityShow": "闲鱼严选卖家"}) is True
    assert _guess_is_shop({"userIdentityShow": "手机严选授权服务商"}) is True
    assert _guess_is_shop({"userIdentityShow": ""}) is None
    assert _guess_is_shop({}) is None
    assert _guess_is_shop({"userIsUseFishShopCard": True}) is True


def test_fish_shop_label_is_reputation_not_a_shop_flag(real_rows):
    """Despite the name, `userFishShopLabel` is present on every row and holds
    review count and positive rate. Treating its presence as a shop signal
    flagged all ten live rows as merchants, including 阿芳阿芳 with 0 reviews.
    """
    for row in real_rows:
        ex = row["data"]["item"]["main"]["exContent"]
        assert ex.get("userFishShopLabel", {}).get("tagList")
        reviews, rate = _seller_reputation(ex)
        assert reviews is not None and rate is not None


def test_reputation_parsing_from_label_text():
    ex = {
        "userFishShopLabel": {
            "tagList": [{"data": {"content": "8237条评价"}}, {"data": {"content": "好评率53%"}}]
        }
    }
    assert _seller_reputation(ex) == (8237, 53.0)
    assert _seller_reputation({}) == (None, None)
    zero = {
        "userFishShopLabel": {
            "tagList": [{"data": {"content": "0条评价"}}, {"data": {"content": "好评率0%"}}]
        }
    }
    # Zero reviews is information, not absence: a brand-new account.
    assert _seller_reputation(zero) == (0, 0.0)


def test_publish_time_is_only_a_fuzzy_tag(real_rows):
    """Search results carry no timestamp — only a label like 刚刚发布. The
    precise published_within_hours filter therefore cannot be evaluated from a
    list page and is waived with a label instead.
    """
    items = [base.normalize_item(flatten_search_row(r), source="mtop") for r in real_rows]
    assert all(i.publish_time is None for i in items)
    assert any(i.publish_hint for i in items)
    assert all("publish_time" in i.missing_fields for i in items)


def test_want_and_view_counts_are_absent_from_search_results(real_rows):
    items = [base.normalize_item(flatten_search_row(r), source="mtop") for r in real_rows]
    assert all(i.want_count is None for i in items)
    assert all("want_count" in i.missing_fields for i in items)


def test_cover_photo_backfills_the_image_list_without_a_false_alarm(real_rows):
    """Search rows carry only picUrl. The image list is backfilled from it, and
    image_urls must NOT then be reported as a stale-field-map symptom.
    """
    item = base.normalize_item(flatten_search_row(real_rows[0]), source="mtop")
    assert item.cover_url and item.image_urls == (item.cover_url,)
    assert "image_urls" not in item.missing_fields


def test_flatten_drops_empty_values_rather_than_passing_blanks(real_rows):
    flat = flatten_search_row(real_rows[0])
    assert "" not in flat.values()
    assert None not in flat.values()


def test_flatten_survives_a_truncated_row():
    """Upstream shape changes must surface as ParseError from normalize_item,
    not as a KeyError inside the extractor.
    """
    assert flatten_search_row({}) == {}
    assert flatten_search_row({"data": {"item": {}}}) == {}
    with pytest.raises(base.ParseError):
        base.normalize_item(flatten_search_row({}), source="mtop")
