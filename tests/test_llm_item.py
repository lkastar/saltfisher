"""Single-item advice: the five inputs, the keyword choice, the image traps.

No network anywhere, image fetches included: `app.state.notify_client` is
replaced by an `httpx.MockTransport` that answers both the chat route and the
CDN. A test that calls the real endpoint is a flake with a bill attached, and
one that pulls a real photo tells Alibaba when the suite runs.

The PNGs are built with zlib + struct (see `tests/test_llm_images.py`), so a
1x1 sentinel and a real photo are both a few lines and neither is a committed
binary nobody can check.

Two assertions here are the ones worth keeping if the file ever shrinks:
every one of the five inputs is asserted INSIDE the request body the stub
received, and the keyword is asserted to be the earliest rule's — the choice
design §5 makes, which no other test in the suite can see.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_session
from app.llm.images import MAX_BYTES
from app.main import app
from app.models import Item, Monitor, MonitorHit, PriceSnapshot, Seller, Watchlist
from tests.test_llm_images import png

AUTH = {"Authorization": "Bearer testtoken123"}
KEY = "sk-liveKey0123456789abcdef"
SECRET_SHAPED = re.compile(r"sk-[A-Za-z0-9_-]{8,}")

NOW = datetime.now(UTC)
ITEM_ID = "1079150111050"
COVER = "http://img.alicdn.com/bao/uploaded/i2/195943285/cover.jpg"
SECOND = "http://img.alicdn.com/bao/uploaded/i4/195943285/second.jpg"
NOTE = "实盘闭环验证，只收带票的"

# The broad rule found it first, and it has the HIGHER id. A "take any ledger
# row" query returns the narrow rule (lower primary key, first in index
# order), so the two keywords tell the design's choice apart from the
# convenient one.
BROAD = "索尼 a7c2"
NARROW = "索尼 a7c2 单机"

ADVICE = {
    "verdict": "可考虑",
    "fair_price_yuan": 7800,
    "offer_price_yuan": 7400,
    "risks": ["热靴有掉漆", "只观察到一次价格"],
    "summary": "价格贴近该关键词的中位数，成色描述完整但有掉漆。",
}


def chat_payload(content: str, completion: int = 60, reasoning: int = 20) -> dict:
    """Shaped like the measured response, reasoning breakdown included."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 219,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def gateway(
    *, chat: httpx.Response | None = None, photo: httpx.Response | None = None
) -> list[httpx.Request]:
    """A stand-in LLM endpoint and CDN in one transport. Returns the log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "img.alicdn.com" in request.url.host:
            return photo or httpx.Response(
                200, content=png(64, 64), headers={"content-type": "image/png"}
            )
        return chat or httpx.Response(200, json=chat_payload(json.dumps(ADVICE)))

    app.state.notify_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def seed(engine, *, in_ledger: bool = True, note: str | None = NOTE) -> None:
    """One listing with all five inputs present, plus a market to compare to.

    Timestamps hang off the real clock because `analytics.price_distribution`
    filters on a window ending now; a fixed T0 would silently empty the
    statistics section the day this test is run.
    """
    with Session(engine) as s:
        s.add(
            Seller(
                id="s1",
                nick="中国村长",
                is_shop=False,
                credit_level=5,
                review_count=289,
                positive_rate=97.0,
                sold_count=591,
                verified=True,
                account_age_days=4104,
                fetched_at=NOW - timedelta(hours=2),
            )
        )
        s.add(Monitor(id=2, name="narrow", keyword=NARROW))
        s.add(Monitor(id=7, name="broad", keyword=BROAD))
        s.commit()

        s.add(
            Item(
                id=ITEM_ID,
                title="索尼A7C2 机身 带原包装",
                description="今年1月购买，用了几个月，无拆无修无进水。热靴装闪光灯有掉漆，贩子勿扰！",
                cover_url=COVER,
                image_urls=json.dumps([COVER, SECOND]),
                region="运城",
                seller_id="s1",
                seller_nick="中国村长",
                publish_time=NOW - timedelta(days=1),
                first_seen_at=NOW - timedelta(hours=6),
                last_seen_at=NOW - timedelta(minutes=5),
            )
        )
        # Four more listings in the broad rule's ledger, so the keyword has a
        # distribution to quote rather than an empty section.
        for i in range(4):
            s.add(
                Item(
                    id=f"peer{i}",
                    title=f"索尼A7C2 第{i}台",
                    seller_id="s1",
                    seller_nick="中国村长",
                    first_seen_at=NOW - timedelta(hours=5),
                    last_seen_at=NOW - timedelta(minutes=9),
                )
            )
        s.commit()

        s.add(
            PriceSnapshot(
                item_id=ITEM_ID,
                price_cents=830000,
                status="on_sale",
                source="mtop",
                captured_at=NOW - timedelta(hours=6),
            )
        )
        s.add(
            PriceSnapshot(
                item_id=ITEM_ID,
                price_cents=810000,
                status="on_sale",
                source="detail",
                want_count=8,
                view_count=127,
                captured_at=NOW - timedelta(minutes=5),
            )
        )
        for i in range(4):
            s.add(
                PriceSnapshot(
                    item_id=f"peer{i}",
                    price_cents=700000 + i * 50000,
                    status="on_sale",
                    source="mtop",
                    captured_at=NOW - timedelta(minutes=9),
                )
            )
        if in_ledger:
            # The narrow rule saw it three hours LATER than the broad one.
            s.add(MonitorHit(monitor_id=2, item_id=ITEM_ID, first_hit_at=NOW - timedelta(hours=3)))
            s.add(MonitorHit(monitor_id=7, item_id=ITEM_ID, first_hit_at=NOW - timedelta(hours=6)))
            for i in range(4):
                s.add(
                    MonitorHit(
                        monitor_id=7, item_id=f"peer{i}", first_hit_at=NOW - timedelta(hours=5)
                    )
                )
        if note is not None:
            s.add(Watchlist(item_id=ITEM_ID, added_price_cents=830000, note=note))
        s.commit()


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    gateway()
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def configure(
    c: TestClient,
    *,
    model: str = "deepseek-v4-flash-vision-exp",
    send_images: bool = True,
    enabled: bool = True,
    template: str | None = None,
) -> None:
    r = c.post(
        "/api/llm/endpoints",
        json={
            "label": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": KEY,
            "wire_format": "openai",
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    body = {
        "endpoint_id": r.json()["id"],
        "model": model,
        "send_images": send_images,
        "enabled": enabled,
    }
    if template is not None:
        body["prompt_template"] = template
    assert c.put("/api/llm/scenarios/item", json=body, headers=AUTH).status_code == 200


def analyze(c: TestClient, item_id: str = ITEM_ID) -> httpx.Response:
    return c.post(f"/api/llm/analyze/item/{item_id}", headers=AUTH)


def chat_requests(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if "chat/completions" in r.url.path]


def prompt_of(request: httpx.Request) -> str:
    """The user text as the endpoint received it, images or not."""
    content = json.loads(request.content)["messages"][1]["content"]
    return content if isinstance(content, str) else content[0]["text"]


def image_parts(request: httpx.Request) -> list[dict]:
    content = json.loads(request.content)["messages"][1]["content"]
    if isinstance(content, str):
        return []
    return [part for part in content if part.get("type") == "image_url"]


# --------------------------------------------------------------------------- #
# The five inputs
# --------------------------------------------------------------------------- #


def test_all_five_inputs_reach_the_prompt(client):
    """Asserted against the request body the stub received, not against a
    helper's return value: the question is what the model was shown.
    """
    c, engine = client
    seed(engine)
    configure(c)
    seen = gateway()

    assert analyze(c).status_code == 200
    prompt = prompt_of(chat_requests(seen)[0])

    assert "索尼A7C2 机身 带原包装" in prompt  # 1. the listing
    assert "热靴装闪光灯有掉漆" in prompt
    assert "¥8100.00" in prompt and "¥8300.00" in prompt  # 2. its price history
    assert "想要 8" in prompt
    assert "中国村长" in prompt and "97.0%" in prompt  # 3. the seller
    assert NOTE in prompt  # 4. the user's note as {user_intent}
    assert "参照关键词" in prompt and "P50" in prompt  # 5. the keyword's stats


def test_a_missing_note_says_so_instead_of_leaving_a_dangling_sentence(client):
    """The template reads 「用户的关注点是：{user_intent}」. An empty
    substitution leaves the model staring at a colon, and the watchlist holds
    one row in the real database — most items reached from /items have no
    note at all.
    """
    c, engine = client
    seed(engine, note=None)
    configure(c)
    seen = gateway()

    analyze(c)
    assert "用户没有填写备注" in prompt_of(chat_requests(seen)[0])


def test_an_unfetched_seller_profile_is_not_read_as_zero(client):
    """`models.py`: None means "not fetched", which is a different thing from
    0. Omitting the field and saying so is what keeps a gap from reading as a
    warning sign.
    """
    c, engine = client
    seed(engine)
    with Session(engine) as s:
        seller = s.get(Seller, "s1")
        seller.fetched_at = None
        seller.review_count = None
        s.add(seller)
        s.commit()
    configure(c)
    seen = gateway()

    analyze(c)
    prompt = prompt_of(chat_requests(seen)[0])
    assert "评价数" not in prompt
    assert "缺项不代表是 0" in prompt


# --------------------------------------------------------------------------- #
# The keyword decision (design §5)
# --------------------------------------------------------------------------- #


def test_the_keyword_is_the_earliest_rules_and_it_is_echoed(client):
    """One listing, two rules, two different price levels. Design §5 takes the
    rule whose `first_hit_at` is earliest, and the answer says which one it
    used — a comparison whose yardstick is invisible cannot be judged.
    """
    c, engine = client
    seed(engine)
    configure(c)
    seen = gateway()

    body = analyze(c).json()
    assert body["keyword"] == BROAD

    prompt = prompt_of(chat_requests(seen)[0])
    assert f"「{BROAD}」" in prompt
    # The narrow rule saw it three hours later, so its statistics must not be
    # the ones quoted.
    assert NARROW not in prompt


def test_an_item_in_no_ledger_still_answers(client):
    """A watchlist entry added by pasting a link is in no rule's ledger, so
    there are no keyword statistics to compare against. That is a listing the
    user asked about, not an error.
    """
    c, engine = client
    seed(engine, in_ledger=False)
    configure(c)
    seen = gateway()

    body = analyze(c).json()
    assert body["kind"] == "ok"
    assert body["keyword"] is None

    prompt = prompt_of(chat_requests(seen)[0])
    assert "不在任何关键词的采集台账里" in prompt
    assert "不要凭空假设市场价" in prompt


# --------------------------------------------------------------------------- #
# Images: three measured traps
# --------------------------------------------------------------------------- #


def test_a_real_photo_reaches_the_model(client):
    c, engine = client
    seed(engine)
    configure(c)
    seen = gateway()

    body = analyze(c).json()
    # Two, because this fixture's listing has a gallery. The real data almost
    # never does: 325 of 327 items hold one back-filled cover URL.
    assert body["images_sent"] == 2
    assert body["notes"] == []

    parts = image_parts(chat_requests(seen)[0])
    assert len(parts) == 2
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0K")
    # And the prompt says how many the model is looking at, so a text-only
    # answer cannot be mistaken for one that ignored the pictures.
    assert "随本次请求发送的图片：2 张" in prompt_of(chat_requests(seen)[0])


def test_a_1x1_pixel_at_http_200_is_not_sent_to_the_model(client):
    """This CDN answers a missing file with 200 and a 1x1 pixel. Trusting the
    status code means paying a vision model to look at a blank dot — and the
    verdict would then read as if it had seen the item.
    """
    c, engine = client
    seed(engine)
    configure(c)
    seen = gateway(
        photo=httpx.Response(200, content=png(1, 1), headers={"content-type": "image/png"})
    )

    body = analyze(c).json()
    assert body["images_sent"] == 0
    assert any("1×1" in note for note in body["notes"])
    assert any("只依据文字资料" in note for note in body["notes"])
    assert image_parts(chat_requests(seen)[0]) == []


def test_an_oversized_image_is_skipped_and_reported(client):
    c, engine = client
    seed(engine)
    configure(c)
    gateway(photo=httpx.Response(200, content=b"\xff\xd8" + b"\x00" * (MAX_BYTES + 1)))

    body = analyze(c).json()
    assert body["images_sent"] == 0
    assert any("上限" in note for note in body["notes"])


def test_only_the_first_three_urls_are_fetched(client):
    """The cap is 3. On the real data 325 of 327 listings hold exactly one URL
    (a back-filled cover), so this ceiling is not reached today.
    """
    c, engine = client
    seed(engine)
    with Session(engine) as s:
        item = s.get(Item, ITEM_ID)
        item.image_urls = json.dumps([f"{COVER}?{i}" for i in range(5)])
        s.add(item)
        s.commit()
    configure(c)
    seen = gateway()

    assert analyze(c).json()["images_sent"] == 3
    assert len([r for r in seen if "img.alicdn.com" in r.url.host]) == 3


# --------------------------------------------------------------------------- #
# Degradation to text, always stated
# --------------------------------------------------------------------------- #


def test_send_images_off_is_stated_in_the_result(client):
    c, engine = client
    seed(engine)
    configure(c, send_images=False)
    seen = gateway()

    body = analyze(c).json()
    assert body["images_sent"] == 0
    assert any("没有开启发送图片" in note for note in body["notes"])
    assert image_parts(chat_requests(seen)[0]) == []
    # And nothing was downloaded either.
    assert [r for r in seen if "img.alicdn.com" in r.url.host] == []


def test_a_model_without_vision_degrades_and_says_so(client):
    """Measured: `deepseek-v4-flash-vision-exp` accepts images and
    `deepseek-v4-flash` does not. Sending anyway buys a rejection whose reason
    points at the endpoint rather than at the model choice.
    """
    c, engine = client
    seed(engine)
    configure(c, model="deepseek-v4-flash")
    gateway()

    body = analyze(c).json()
    assert body["images_sent"] == 0
    assert any("不支持图片输入" in note for note in body["notes"])
    assert any("降级为纯文本" in note for note in body["notes"])


# --------------------------------------------------------------------------- #
# The three HTTP-200 failure modes, and the one that is ours
# --------------------------------------------------------------------------- #


def test_a_starved_response_talks_about_the_token_budget(client):
    """Measured: max_tokens=60 returned 60 reasoning tokens, an empty string,
    finish_reason "stop" and no error. Saying 「检查提示词」 here sends the user
    to debug the one thing that is not wrong.
    """
    c, engine = client
    seed(engine)
    configure(c)
    gateway(chat=httpx.Response(200, json=chat_payload("", completion=60, reasoning=60)))

    body = analyze(c).json()
    assert body["kind"] == "starved"
    assert "max_tokens" in body["message"]
    assert "提示词" not in body["message"]


def test_a_broken_template_shows_the_model_text_verbatim(client):
    """A user editing the prompt into something that no longer asks for JSON
    is a normal event. The model's own words are the only useful thing to
    show, and one retry is spent first because sampling is nondeterministic.
    """
    c, engine = client
    seed(engine)
    configure(c, template="随便聊聊这件商品：{item}")
    seen = gateway(chat=httpx.Response(200, json=chat_payload("这台机子还不错，可以谈谈。")))

    body = analyze(c).json()
    assert body["kind"] == "unparsable"
    assert body["text"] == "这台机子还不错，可以谈谈。"
    assert "请检查提示词" in body["message"]
    assert len(chat_requests(seen)) == 2  # one retry, not a loop


def test_nothing_is_cached(client):
    """The `market` scenario caches; this one must not. Price and seller state
    are what the answer turns on, and a snapshot write is not an event that
    could invalidate a stored verdict.
    """
    c, engine = client
    seed(engine)
    configure(c)
    seen = gateway()

    assert analyze(c).status_code == 200
    assert analyze(c).status_code == 200
    assert len(chat_requests(seen)) == 2


def test_an_unconfigured_scenario_is_400_and_a_missing_item_is_404(client):
    c, engine = client
    seed(engine)
    assert analyze(c).status_code == 400  # nothing configured at all
    configure(c, enabled=False)
    assert analyze(c).status_code == 400  # configured but switched off
    configure(c)
    assert analyze(c, "nope").status_code == 404


def test_the_disclaimer_is_always_there(client):
    c, engine = client
    seed(engine)
    configure(c)
    gateway()
    assert "不构成投资或交易建议" in analyze(c).json()["disclaimer"]


def test_the_api_key_never_appears_in_the_advice_response(client):
    """The router-wide sweep in `test_llm_api.py` predates this route; the
    audit is by pattern rather than by the value we happen to know.
    """
    c, engine = client
    seed(engine)
    configure(c)
    gateway(chat=httpx.Response(401, json={"error": f"invalid key: {KEY}"}))

    r = analyze(c)
    assert r.status_code == 502
    assert KEY not in r.text
    assert SECRET_SHAPED.search(r.text) is None


def test_a_keyword_with_one_listing_says_so_instead_of_500ing(client):
    """`analytics._quantiles` returns {} below two samples, deliberately — it
    refuses to extrapolate a distribution out of one listing. A section that
    tested only `sample_size` and then read p10 would 500 on a narrow keyword,
    which is the normal state of a keyword the user just added.

    Found by the reverse-verification of the keyword choice: pointing the
    lookup at the narrow rule turned every test in this file into a KeyError.
    """
    c, engine = client
    seed(engine, in_ledger=False)
    with Session(engine) as s:
        s.add(Monitor(id=9, name="single", keyword="索尼 a7c2 单机套装"))
        s.commit()
        s.add(MonitorHit(monitor_id=9, item_id=ITEM_ID, first_hit_at=NOW - timedelta(hours=1)))
        s.commit()
    configure(c)
    seen = gateway()

    body = analyze(c).json()
    assert body["kind"] == "ok"
    assert body["keyword"] == "索尼 a7c2 单机套装"
    prompt = prompt_of(chat_requests(seen)[0])
    assert "样本太少" in prompt
    assert "P50" not in prompt
