"""The `market` scenario: what reaches the model, and what a repeat click costs.

Never touches the network — every call goes through `httpx.MockTransport` on
`app.state.notify_client`, the same way `tests/test_llm_client.py` and
`tests/test_llm_api.py` do. A test that reaches a real endpoint is a flake
with a bill attached.

Two things here would fail silently in production and are therefore asserted
on the REQUEST BODY the stub received, not on the response:

1. The aperture caveat. A model told "listings vanish in 45 minutes" without
   being told that a cycle reads one page of 30 will confidently over-read the
   market, and the answer will look just as fluent as a correct one.
2. `data_days`. This tool starts with an empty database, so the model has to
   be able to say "12 days is not enough to call a trend" instead of
   inventing one.

Reverse-verified (see the task report): dropping the aperture from the
assembled stats turns `test_the_prompt_carries_the_aperture_caveat` red, and
keying the cache on `(keyword, window_days)` alone turns
`test_a_changed_aperture_is_a_cache_miss` red.
"""

import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from app.db import get_session
from app.llm import market
from app.llm.prompts import DISCLAIMER, MARKET_PROMPT
from app.main import app
from app.models import (
    CollectRun,
    Item,
    LlmEndpoint,
    LlmScenarioConfig,
    Monitor,
    MonitorHit,
    PriceSnapshot,
    Seller,
    utcnow,
)

AUTH = {"Authorization": "Bearer testtoken123"}
KEY = "sk-liveKey0123456789abcdef"
SECRET_SHAPED = re.compile(r"sk-[A-Za-z0-9_-]{8,}")

NOW = utcnow()
KW = "iPhone 15"
WINDOW = 7
HISTORY_DAYS = 12
MODEL = "deepseek-v4-pro"

# A well-formed answer in the contract `MARKET_PROMPT` states.
READING = {
    "level": "正常",
    "trend": "平稳",
    "advice": "观望",
    "reasons": ["p50 ¥4045 与 p25 ¥3607 之间的差距不大"],
    "summary": "样本 11 件，历史 12 天，只能说水位居中。",
}

# Six live listings, one asking price each (¥3280 – ¥4980).
LIVE_PRICES = (328000, 350000, 389000, 420000, 455000, 498000)
# Three that came down: (price at the window start, price now).
DROPPED = ((560000, 498000), (640000, 580000), (450000, 429000))
# Two that left the observation range, with how long they lasted.
LEFT_RANGE = ((398000, timedelta(minutes=45)), (512000, timedelta(hours=26)))


def chat_payload(content: str, completion: int = 120, reasoning: int = 64) -> dict:
    """Shaped like the measured DeepSeek response, reasoning breakdown included."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 1180,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def gateway(*replies: httpx.Response) -> list[httpx.Request]:
    """A stand-in gateway. Returns the log of CHAT requests only.

    Each reply is used once, the last one repeats — so a retry can be given a
    different answer than the first attempt.
    """
    chats: list[httpx.Request] = []
    queue = list(replies) or [httpx.Response(200, json=chat_payload(json.dumps(READING)))]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        chats.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    app.state.notify_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return chats


def prompt_of(request: httpx.Request) -> str:
    """The user message as it went over the wire."""
    return json.loads(request.content)["messages"][1]["content"]


# --------------------------------------------------------------------------- #
# Fixture data
# --------------------------------------------------------------------------- #


def _listing(session, item_id: str, monitor_id: int, *, prices, first_hit, last_hit) -> None:
    """One listing with its price history, wired into a rule's ledger.

    `prices` is oldest-first and is captured at `first_hit` … `last_hit`, so
    "the newest snapshot" is actually defined for it.
    """
    session.add(Seller(id=f"s{item_id}", nick="老王"))
    session.commit()
    session.add(
        Item(
            id=item_id,
            title=f"{KW} 国行 128G {item_id}",
            seller_id=f"s{item_id}",
            seller_nick="老王",
            first_seen_at=first_hit,
            last_seen_at=last_hit,
            status="on_sale",
        )
    )
    times = [first_hit, last_hit][: len(prices)] if len(prices) > 1 else [last_hit]
    for price, captured_at in zip(prices, times, strict=True):
        session.add(
            PriceSnapshot(
                item_id=item_id,
                price_cents=price,
                status="on_sale",
                source="mtop",
                captured_at=captured_at,
            )
        )
    session.add(
        MonitorHit(
            monitor_id=monitor_id,
            item_id=item_id,
            first_hit_at=first_hit,
            last_hit_at=last_hit,
            in_range=True,
        )
    )
    session.commit()


def seed_market(session, *, keyword: str = KW, pages: int = 1) -> int:
    """One keyword with 12 days of history, shaped like the real database.

    Deliberately not a tidy dataset: a skipped day, a failed cycle, listings
    that came down and listings that dropped out of range. The prompt is only
    honest if the fixture behind it can be dishonest.
    """
    monitor = Monitor(name=f"rule {keyword}", keyword=keyword, interval_seconds=300)
    session.add(monitor)
    session.commit()
    monitor_id = monitor.id
    assert monitor_id is not None
    fresh = NOW - timedelta(minutes=1)
    oldest = NOW - timedelta(days=HISTORY_DAYS - 1)

    for i, price in enumerate(LIVE_PRICES):
        _listing(
            session,
            f"live{i}",
            monitor_id,
            prices=[price],
            first_hit=NOW - timedelta(days=2),
            last_hit=fresh,
        )
    for i, (then, current) in enumerate(DROPPED):
        _listing(
            session,
            f"drop{i}",
            monitor_id,
            prices=[then, current],
            first_hit=oldest,
            last_hit=fresh,
        )
    for i, (price, lasted) in enumerate(LEFT_RANGE):
        start = NOW - timedelta(days=5 - i)
        _listing(
            session,
            f"gone{i}",
            monitor_id,
            prices=[price],
            first_hit=start,
            last_hit=start + lasted,
        )

    for day_ago in range(HISTORY_DAYS):
        if day_ago == 2:
            continue  # a day we never ran: the series must say so, not draw a 0
        started = NOW - timedelta(days=day_ago)
        ok = day_ago != 4  # a cycle that failed before fetching anything
        session.add(
            CollectRun(
                monitor_id=monitor_id,
                started_at=started,
                ok=ok,
                item_count=30 if ok else 0,
                pages=pages if ok else None,
                collector="mtop" if ok else None,
                error=None if ok else "transient: rate limited",
            )
        )
    session.commit()
    return monitor_id


def configure(session, *, enabled: bool = True, template: str | None = None) -> None:
    endpoint = LlmEndpoint(
        label="deepseek", base_url="https://api.deepseek.com", api_key=KEY, wire_format="openai"
    )
    session.add(endpoint)
    session.commit()
    session.add(
        LlmScenarioConfig(
            scenario="market",
            endpoint_id=endpoint.id,
            model=MODEL,
            prompt_template=template,
            enabled=enabled,
        )
    )
    session.commit()


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    # The cache lives on app.state, and `app` is a module singleton: without
    # this, one test's answers would be served to the next one.
    app.state.llm_market_cache = market.MarketCache()
    gateway()
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def analyze(c: TestClient, *, keyword: str = KW, days: int = WINDOW) -> httpx.Response:
    return c.post("/api/llm/analyze/market", json={"keyword": keyword, "days": days}, headers=AUTH)


# --------------------------------------------------------------------------- #
# What the model actually sees
# --------------------------------------------------------------------------- #


def test_the_prompt_carries_the_aperture_caveat(client):
    """P3 measured the aperture so that this sentence could exist.

    The duration numbers are not a time to sale: a listing stops coming back
    because it sold, because it was delisted, or because its rank fell past
    the pages we read. Sent the quantiles alone, a model reads the third case
    as the first and says the market is hot.

    Reverse verification: drop `_aperture_lines` from the assembled stats and
    this test goes red.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()

    assert analyze(c).status_code == 200
    prompt = prompt_of(chats[0])

    assert "前 1 页" in prompt, "the page count never reached the model"
    assert "每页 30 条" in prompt
    # In words, not just numbers: the caveat has to be readable as a caveat.
    assert "不是售出速度" in prompt
    assert "排名掉到" in prompt


def test_the_prompt_carries_the_data_age_and_the_sample_size(client):
    """An empty-ish database is the normal state of this tool. The model can
    only say "not enough history to call a trend" if it is told the history.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()
    analyze(c)
    prompt = prompt_of(chats[0])

    assert f"data_days={HISTORY_DAYS}" in prompt
    assert "sample_size=11" in prompt
    # 11 = 6 live + 3 dropped + 2 that left the range, all seen inside the window.
    assert analyze(c).json()["sample_size"] == 11


def test_a_short_history_says_so_in_words(client):
    """Under a week, "trend" is not a question the data can answer, and that
    instruction is in the prompt rather than left to the model's judgement.
    """
    c, engine = client
    with Session(engine) as s:
        monitor = Monitor(name="r", keyword=KW, interval_seconds=300)
        s.add(monitor)
        s.commit()
        assert monitor.id is not None
        _listing(
            s,
            "young",
            monitor.id,
            prices=[398000],
            first_hit=NOW - timedelta(days=3),
            last_hit=NOW - timedelta(minutes=1),
        )
        configure(s)
    chats = gateway()
    analyze(c)

    prompt = prompt_of(chats[0])
    assert "data_days=4" in prompt
    assert "不足以判断趋势" in prompt


def test_a_missed_day_is_not_a_zero(client):
    """ "No new listings" and "we never ran" are different facts. Collapsing
    them is how a series tells the model the market went quiet when really the
    process was down.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()
    analyze(c)

    prompt = prompt_of(chats[0])
    assert "未采集" in prompt
    assert "不代表那几天没有新货" in prompt

    # And the count for such a day is still printed, not masked by the
    # marker. Masking it looked tidier and lost real listings: every day
    # before `CollectRun` existed reads as uncollected while having genuine
    # first sightings. Found by reading the assembled prompt -- the masked
    # series summed to 1 of the 8 listings this fixture holds.
    series = next(line for line in prompt.splitlines() if line.startswith("2026-"))
    assert sum(int(n) for n in re.findall(r":(\d+)", series)) == 8, series


def test_a_mixed_duration_clock_says_so(client):
    """`legacy_clock_rows` is the other caveat P3 left behind: ledger rows
    written before `MonitorHit.last_hit_at` existed are timed by the global
    `Item.last_seen_at`, which a second rule can keep fresh. Those samples
    under-report, so a distribution that mixes them has to admit it.
    """
    c, engine = client
    with Session(engine) as s:
        monitor_id = seed_market(s)
        _listing(
            s,
            "legacy",
            monitor_id,
            prices=[405000],
            first_hit=NOW - timedelta(days=3),
            last_hit=NOW - timedelta(days=3) + timedelta(hours=2),
        )
        row = s.get(MonitorHit, (monitor_id, "legacy"))
        assert row is not None
        row.last_hit_at = None  # as written before the column existed
        s.add(row)
        s.commit()
        configure(s)
    chats = gateway()
    analyze(c)

    prompt = prompt_of(chats[0])
    assert "1 条样本仍由旧的全局时钟计时" in prompt
    assert "混了两种测量" in prompt


def test_no_raw_listing_reaches_the_prompt(client):
    """Aggregated statistics only (FR-P4-3).

    264 rows of titles cost more prompt than the quantiles do and say nothing
    the quantiles do not already say. This is the assertion that keeps a
    later "just add the top 5 titles" from happening quietly.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()
    analyze(c)

    prompt = prompt_of(chats[0])
    for item_id in ("live0", "drop0", "gone1"):
        assert item_id not in prompt
    assert "国行 128G" not in prompt


def test_the_schema_matches_the_default_prompt():
    """One contract in two files (`prompts.py` docstring): the JSON example in
    the template and `MarketReading`'s fields. A rename in one place fails to
    parse in the other and no retry can fix it.
    """
    example = max(re.findall(r"\{[^{}]*\}", MARKET_PROMPT, re.DOTALL), key=len)
    assert set(json.loads(example)) == set(market.MarketReading.model_fields)


# --------------------------------------------------------------------------- #
# Caching: a repeat click must not bill a second call
# --------------------------------------------------------------------------- #


def test_a_repeat_click_does_not_bill_a_second_call(client):
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()

    first = analyze(c).json()
    second = analyze(c).json()

    assert len(chats) == 1, "the second click called the endpoint again"
    assert (first["cached"], second["cached"]) == (False, True)
    assert second["reading"] == first["reading"] == READING


def test_a_changed_aperture_is_a_cache_miss(client):
    """design.md §5: the key includes the observation aperture, not just
    keyword + window.

    Paging changed the aperture, so the same keyword over the same window is
    no longer the same statistics — a wider net makes a listing "leave" later,
    which moves the distribution without the market moving at all.

    The fixture widens the LAST cycle only, leaving every other number in the
    prompt untouched, so this cannot pass because something else changed.

    Reverse verification: key the cache on `(keyword, window_days)` and this
    goes red.
    """
    c, engine = client
    with Session(engine) as s:
        monitor_id = seed_market(s)
        configure(s)
    chats = gateway()

    analyze(c)
    with Session(engine) as s:
        # The most recent cycle now reads three pages. Widening one cycle
        # rather than adding one keeps every other figure in the prompt
        # identical, so a miss here can only be the aperture.
        run = s.exec(
            select(CollectRun)
            .where(col(CollectRun.monitor_id) == monitor_id)
            .order_by(col(CollectRun.started_at).desc())
        ).first()
        assert run is not None
        run.pages = 3
        s.add(run)
        s.commit()
    second = analyze(c).json()

    assert len(chats) == 2, "the answer for a one-page aperture was reused for three"
    assert second["cached"] is False
    prompt = prompt_of(chats[1])
    assert "前 3 页" in prompt
    # And the model is told the window is not comparable with itself.
    assert "1→3 页" in prompt
    assert "前后不可比" in prompt


def test_a_failed_answer_is_not_cached(client):
    """`starved` is fixed by raising max_tokens and `unparsable` by editing the
    template. A cached failure would keep serving the old complaint after the
    user fixed the thing it complained about.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway(httpx.Response(200, json=chat_payload("", completion=60, reasoning=60)))

    assert analyze(c).json()["kind"] == "starved"
    assert analyze(c).json()["kind"] == "starved"
    assert len(chats) == 2, "a starved answer was cached, so the retry after the fix is blocked"


# --------------------------------------------------------------------------- #
# The three failure modes stay three (design.md section 4)
# --------------------------------------------------------------------------- #


def test_a_starved_endpoint_talks_about_the_token_budget(client):
    """Measured: max_tokens=60 returned 60 reasoning tokens, an empty string,
    HTTP 200 and `finish_reason: stop`. Telling that user to check the prompt
    sends them to debug the one thing that is not wrong.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    gateway(httpx.Response(200, json=chat_payload("", completion=60, reasoning=60)))

    body = analyze(c).json()
    assert body["kind"] == "starved"
    assert "max_tokens" in body["message"]
    assert "提示词" not in body["message"]
    assert body["reading"] is None


def test_a_broken_template_shows_the_model_text_verbatim(client):
    """A user breaking the template is a normal event, not an exception: the
    answer comes back as `unparsable` with the model's own words to look at.

    `{stats}` is kept deliberately. Dropping it is a DIFFERENT failure with a
    different answer -- the prompt would carry no data while still demanding
    cited numbers, so the route refuses before spending anything. This test is
    about a template that still hands over the evidence and merely asks for
    prose back.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s, template="只回答一句话：{keyword} 现在贵不贵？\n{stats}")
    chats = gateway(httpx.Response(200, json=chat_payload("不算贵，最近还降了点。")))

    body = analyze(c).json()
    assert body["kind"] == "unparsable"
    assert body["text"] == "不算贵，最近还降了点。"
    assert "提示词" in body["message"]
    assert len(chats) == 2, "the one retry did not happen"


def test_an_unreachable_endpoint_is_a_502_not_a_500(client):
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    gateway(httpx.Response(500, text="upstream exploded"))

    r = analyze(c)
    assert r.status_code == 502
    assert "端点" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# The states that are not the model's fault
# --------------------------------------------------------------------------- #


def test_an_unconfigured_scenario_is_a_409_not_a_500(client):
    """The request is well-formed; the install is unfinished. Three ways to be
    unconfigured, and all three have to say so instead of failing inside the
    call path.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
    chats = gateway()

    no_row = analyze(c)
    with Session(engine) as s:
        configure(s, enabled=False)
    disabled = analyze(c)
    with Session(engine) as s:
        row = s.get(LlmScenarioConfig, "market")
        assert row is not None
        row.enabled, row.model = True, None
        s.add(row)
        s.commit()
    no_model = analyze(c)

    for r in (no_row, disabled, no_model):
        assert r.status_code == 409, r.text
        assert "设置页" in r.json()["detail"]
    assert chats == [], "an unconfigured scenario still called an endpoint"


def test_a_keyword_with_no_listings_does_not_call_the_model(client):
    """A billed call whose honest answer is "there is nothing here" is a call
    worth not making — and a model asked to read a price level off zero
    listings answers just as fluently as if there were data.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)
    chats = gateway()

    body = analyze(c, keyword="没人卖的东西").json()
    assert body["kind"] == "no_data"
    assert (body["sample_size"], body["data_days"]) == (0, 0)
    assert body["reading"] is None
    assert "没有可分析的统计量" in body["message"]
    assert chats == [], "zero listings still cost a call"


def test_every_answer_carries_the_disclaimer(client):
    """FR-P4-3: fixed text, appended by us. A sentence the model was asked to
    include is optional in practice.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)

    gateway()
    good = analyze(c).json()
    app.state.llm_market_cache = market.MarketCache()
    gateway(httpx.Response(200, json=chat_payload("", completion=60, reasoning=60)))
    starved = analyze(c).json()
    empty = analyze(c, keyword="没人卖的东西").json()

    for body in (good, starved, empty):
        assert body["disclaimer"] == DISCLAIMER
        assert "不构成投资" in body["disclaimer"]


# --------------------------------------------------------------------------- #
# Secrets and triggering
# --------------------------------------------------------------------------- #


def test_the_key_stays_out_of_the_analysis_and_the_logs(client, caplog):
    """Checked by PATTERN, not for the one value this test happens to know
    (`docs/m1-report.md`). The 502 path is where a key usually escapes: a
    gateway can echo the auth header back in its body.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s)

    with caplog.at_level("DEBUG"):
        gateway()
        ok = analyze(c)
        gateway(httpx.Response(401, json={"error": f"invalid key: {KEY}"}))
        app.state.llm_market_cache = market.MarketCache()
        refused = analyze(c)

    for r in (ok, refused):
        assert KEY not in r.text
        assert SECRET_SHAPED.search(r.text) is None, r.text
    assert "***" in refused.json()["detail"]
    assert KEY not in caplog.text
    assert SECRET_SHAPED.search(caplog.text) is None


def test_nothing_automatic_can_reach_the_market_analysis():
    """FR-P4-3 and the risk table: user click only. Automatic invocation on a
    300 s poll interval is per-minute billing, so the scheduler and the
    collector are kept unable to reach this path rather than merely told not
    to.
    """
    import pathlib

    reaches = re.compile(r"llm|analyze_market|market_input")
    for source in [pathlib.Path("app/scheduler.py"), *pathlib.Path("app/collector").glob("*.py")]:
        assert not reaches.search(source.read_text()), source


def test_a_template_without_its_data_is_refused_before_it_bills(client):
    """The prompt this would send is worse than an empty string.

    Measured: deleting `{stats}` takes the market prompt from 1626 characters
    to 352 while keeping "下面是聚合统计数据" and "每条 reason 必须指向上面给出
    的某个具体数字". The model is told to cite numbers it never received, so
    the only answer it can give is invented -- and the call bills in full,
    around 150 seconds, sometimes twice.

    Refused at analyze time rather than rejected on save: someone mid-edit
    should not lose their template to a validation error, and the money is
    spent here.
    """
    c, engine = client
    with Session(engine) as s:
        seed_market(s)
        configure(s, template="给我讲讲 {keyword} 的行情，要引用具体数字。")
    chats = gateway(httpx.Response(200, json=chat_payload("{}")))

    answer = analyze(c)

    assert answer.status_code == 409
    assert "{stats}" in answer.json()["detail"]
    assert chats == [], "billed for a prompt that could only be answered by inventing"
