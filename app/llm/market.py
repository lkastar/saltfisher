"""Per-scenario input assembly. One function set per scenario, not one
function with a `scenario` branch (prd.md: 两条独立调用路径).

`market` feeds the model the AGGREGATED statistics `app.analytics` already
computes, never the raw listing list. The listings are the expensive half and
the useless half at once: `iPhone 15` covers 264 rows of titles that cost more
prompt than the quantiles do and say nothing the quantiles do not already say,
and a model handed those rows starts reasoning about individual listings whose
photos and descriptions it cannot see.

Every caveat the numbers need travels INSIDE the rendered `{stats}` block
rather than in the prompt template around it. That is deliberate: the template
is user-editable free text, so a caveat written there is one a user can delete
by accident, while `{stats}` is the one thing a template must keep to be worth
sending at all. Told "listings vanish in 9 minutes" without being told the
aperture was one page of 30, a model will confidently over-read the market.

No HTTP context here: this module returns `LlmRequest`s and raises the
`LlmError` family, never `HTTPException` (`spec/backend/error-handling.md`).
"""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field
from sqlmodel import Session

from app import analytics
from app.llm import prompts
from app.llm.base import LlmOutcome, LlmRequest
from app.models import utcnow

# Ours, not the user's, and that is the point: the template is free text a
# user can break, and "只依据给出的数据作答" is the one instruction that must
# survive a broken template. `prompts.MARKET_PROMPT` says it too; this says it
# from a place the config page cannot edit.
MARKET_SYSTEM = (
    "你是二手交易平台的行情分析助手。只依据用户消息里给出的统计数据作答，"
    "不要编造没有给出的数字或事实。数据不足以支撑判断时，直接说数据不足，"
    "不要凑一个结论。只输出一个 JSON 对象。"
)

# Below this many days of history, "trend" is not a question the data can
# answer, and the model is told so in words. This tool starts with an empty
# database, so the first week is the normal case rather than an edge one —
# without the sentence, "4" is just a number a model will draw a line through.
MIN_TREND_DAYS = 7

# How many of the deepest drops to quote. Magnitudes only: titles and item ids
# are raw listing data, and "which listing" is not a question this scenario
# answers (that is the `item` scenario's).
TOP_DROPS = 5

MINUTES_PER_DAY = 24 * analytics.MINUTES_PER_HOUR
# Past two days, "37 小时" stops being a unit anyone reads.
HOURS_READABLE_UP_TO = 48


class MarketReading(BaseModel):
    """The JSON contract of `prompts.MARKET_PROMPT`.

    The field names here and the JSON example in that template are ONE
    contract in two files (see its module docstring). Verified by
    `tests/test_llm_market.py::test_the_schema_matches_the_default_prompt`,
    because a rename in one place fails to parse in the other and the retry
    cannot fix it.

    Plain `str` rather than `Literal["偏低", "正常", "偏高"]`, deliberately.
    A model that answers "略偏低" has understood the question, and a Literal
    would turn that into `unparsable` — telling the user to check a prompt
    that worked, which is the misdirected error `error-handling.md` forbids.
    The substance is in `summary` and `reasons` either way.

    `reasons` defaults to empty for the same reason: an answer missing its
    bullet list is still an answer.
    """

    level: str
    trend: str
    advice: str
    reasons: list[str] = Field(default_factory=list)
    summary: str


@dataclass(frozen=True, slots=True)
class MarketInput:
    """The assembled market input, plus the two numbers the route acts on.

    `sample_size` and `data_days` are lifted out of the text because the route
    needs to decide whether to call at all: an endpoint bills per call, and
    asking a model to read a price level off zero listings buys a confident
    answer about nothing.
    """

    keyword: str
    window_days: int
    stats: str
    sample_size: int
    data_days: int


def market_input(
    session: Session, keyword: str, *, days: int, now: datetime | None = None
) -> MarketInput:
    """Assemble one keyword's aggregated statistics into prompt text.

    All four metrics share ONE window, even though three of them default to a
    different one on their own routes. Mixing a 7-day price distribution into
    a block headed "最近 30 天" would be a lie to the model about numbers it
    cannot check, and the whole design of this scenario is that the model only
    knows what this text tells it.
    """
    now = now or utcnow()
    dist = analytics.price_distribution(session, keyword, days=days, now=now)
    drops = analytics.price_drops(session, keyword, days=days, limit=TOP_DROPS, now=now)
    supply = analytics.supply_trend(session, keyword, days=days, now=now)
    duration = analytics.listing_duration(session, keyword, days=days, now=now)

    stats = "\n\n".join(
        (
            _scope_block(keyword, days, dist["data_days"]),
            _price_block(dist),
            _drops_block(drops),
            _supply_block(supply),
            _duration_block(duration),
        )
    )
    return MarketInput(
        keyword=keyword,
        window_days=days,
        stats=stats,
        sample_size=int(dist["sample_size"]),
        data_days=int(dist["data_days"]),
    )


def market_request(source: MarketInput, template: str) -> LlmRequest:
    """Render `template` with the market placeholder contract."""
    return LlmRequest(
        system=MARKET_SYSTEM,
        user_text=prompts.render(
            template,
            {
                "keyword": source.keyword,
                "window_days": str(source.window_days),
                "stats": source.stats,
            },
        ),
    )


def market_cache_key(request: LlmRequest, *, endpoint_id: int, model: str) -> str:
    """A digest of everything that decides the answer.

    design.md §5 spells the key as `(keyword, window_days, aperture, 统计量的
    指纹)`, and all four are in `request.user_text` because the assembly above
    wrote them there — including the aperture, which is why hashing the prompt
    is what makes a mid-window aperture change a cache MISS. Paging changed
    the aperture, so the same keyword over the same window is no longer the
    same statistics, and serving the old answer would answer a question
    nobody asked.

    Model, endpoint and (via `user_text`) the template join them because
    editing the prompt or switching models is asking a different question, and
    a hit there would hide the effect of the edit the user just made.
    """
    material = "\x00".join((str(endpoint_id), model, request.system, request.user_text))
    return hashlib.sha256(material.encode()).hexdigest()


class MarketCache:
    """Market answers keyed by what produced them, so a repeat click is free.

    FR-P4-3: a second click inside one window must not bill a second call.

    No TTL, and that is not an omission. The key is a digest of the prompt, so
    the next collection cycle that moves any number in it lands on a different
    key by itself. A TTL would either expire an answer that is still exactly
    right or keep serving one after the statistics moved — the expiry question
    the data already answers.

    ponytail: bounded LRU dict, ~10 lines. Keys turn over with the statistics,
    so the capacity is about not leaking memory, not about hit rate. Reach for
    a real cache library the day this needs to be shared between processes.
    """

    def __init__(self, capacity: int = 32) -> None:
        self._entries: OrderedDict[str, LlmOutcome] = OrderedDict()
        self._capacity = capacity

    def get(self, key: str) -> LlmOutcome | None:
        outcome = self._entries.get(key)
        if outcome is not None:
            self._entries.move_to_end(key)
        return outcome

    def put(self, key: str, outcome: LlmOutcome) -> None:
        self._entries[key] = outcome
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)


# --------------------------------------------------------------------------- #
# The `market` stats block
# --------------------------------------------------------------------------- #


def _scope_block(keyword: str, days: int, data_days: int) -> str:
    lines = [
        f"【口径】关键词「{keyword}」最近 {days} 天的观测结果。",
        "这些数字只是本工具的搜索看到的部分，不是整个平台；"
        "所有价格都是卖家的挂牌价，本工具看不到成交价。",
        f"已累计采集 {data_days} 天历史（data_days={data_days}）。",
    ]
    if data_days < MIN_TREND_DAYS:
        # Spelled out rather than left to the model's judgement: this is the
        # state every fresh install is in, and it is the one place where a
        # confident answer would be worse than no answer.
        lines.append(
            f"历史只有 {data_days} 天，短于 {MIN_TREND_DAYS} 天，"
            "不足以判断趋势——请直接说数据不够，不要据此给出趋势方向。"
        )
    return "\n".join(lines)


def _price_block(dist: dict) -> str:
    sample = int(dist["sample_size"])
    head = (
        f"【挂牌价分位数】样本 {sample} 件（sample_size={sample}），"
        f"其中 {dist['fresh_size']} 件最近两轮仍在观测范围内。"
    )
    if not dist["quantiles"]:
        return f"{head}\n样本不足 2 件，给不出分位数。"
    return f"{head}\n{_quantiles(dist['quantiles'], _yuan)}"


def _drops_block(drops: dict) -> str:
    sample = int(drops["sample_size"])
    rows = drops["rows"]
    if not rows:
        return (
            f"【降价】窗口内没有可比的降价样本（sample_size={sample}）。"
            "窗口内才上架的商品没有更早的报价，不参与比较。"
        )
    quoted = " / ".join(_percent(int(row["drop_bps"])) for row in rows)
    return (
        f"【降价】窗口起点和现在都有报价、且价格下降的共 {sample} 件"
        f"（sample_size={sample}）。\n"
        f"降幅最大的 {len(rows)} 件：{quoted}"
    )


def _supply_block(supply: dict) -> str:
    days = supply["days"]
    if not days:
        return f"【每日新增】窗口内没有任何一天的记录（sample_size={supply['sample_size']}）。"
    # "No new listings" and "we never ran" are different facts, and a zero for
    # both is how a series tells the model the market went quiet when really
    # the process was down (`analytics.supply_trend`).
    #
    # The count is printed even for an uncollected day, with the marker
    # appended rather than replacing it. Masking it looked tidier and threw
    # away real data: every day before `CollectRun` existed reads as
    # uncollected while having genuine first sightings, and on the fixture
    # behind `tests/test_llm_market.py` the masked series summed to 1 of 8
    # listings -- a supply collapse the data never showed.
    series = " / ".join(
        f"{day['date']}:{day['new_count']}" + ("" if day["collected"] else "(未采集)")
        for day in days
    )
    missed = sum(1 for day in days if not day["collected"])
    lines = [
        f"【每日新增挂牌】窗口内共新增 {supply['sample_size']} 件"
        f"（sample_size={supply['sample_size']}）。",
        series,
    ]
    if missed:
        lines.append(
            f"其中 {missed} 天标注为「未采集」：那几天没有成功的采集记录，"
            "所以那天的数字不完整，也不代表那几天没有新货，不要把它读成供应下降。"
        )
    return "\n".join(lines)


def _duration_block(duration: dict) -> str:
    """The metric that most needs its caveat carried in words, not numbers.

    "Left our observation range" is not "sold". A listing stops coming back
    because it was bought, because it was delisted, or because its rank fell
    past the pages we read — and the third one is our own aperture rather than
    the market's behaviour.
    """
    sample = int(duration["sample_size"])
    lines = [f"【离开观测范围的时长】样本 {sample} 件（sample_size={sample}）。"]
    if sample and duration["quantiles"]:
        lines.append(_quantiles(duration["quantiles"], _span))
    elif sample:
        lines.append("样本不足 2 件，给不出分位数。")
    else:
        lines.append("窗口内首次见到的商品都还在搜索结果里，没有「已经离开」的样本。")

    lines.extend(_aperture_lines(duration))
    lines.append(
        "一件商品从观测范围里消失，可能是卖掉了、可能是下架了，"
        "也可能只是排名掉到了我们没有读的页数之后。"
        "所以这是「多久就看不到了」，不是售出速度，不要据此推断卖得多快。"
    )
    if duration["legacy_clock_rows"]:
        lines.append(
            f"其中 {duration['legacy_clock_rows']} 条样本仍由旧的全局时钟计时"
            "（会偏短），这段分布混了两种测量。"
        )
    return "\n".join(lines)


def _aperture_lines(duration: dict) -> list[str]:
    """The observation aperture, in words. P3 added it for exactly this."""
    low, high = duration["aperture_pages_min"], duration["aperture_pages_max"]
    rows = duration["aperture_rows"]
    if high is None:
        # None is not 1: "we never recorded looking" and "we looked at one
        # page" are different claims (`analytics.listing_duration`).
        return ["观测口径：窗口内没有任何一轮采集记录下读了几页，所以观测范围本身未知。"]
    lines = [
        f"观测口径：每轮采集只读搜索结果的前 {high} 页、每页 {rows} 条，"
        f"也就是最多 {high * rows} 条。"
    ]
    if low != high:
        lines.append(
            f"窗口内这个口径变化过（{low}→{high} 页）：口径变宽会让商品更晚「离开」，"
            "所以这段分布前后不可比，不要据此说时长在变长或变短。"
        )
    return lines


def _quantiles(quantiles: dict, unit) -> str:
    return " / ".join(f"{name} {unit(int(value))}" for name, value in quantiles.items())


def _yuan(cents: int) -> str:
    """Whole yuan. Integer division, like every other price in this project:
    a ¥2413.57 in a prompt reads as precision the sample does not have."""
    return f"¥{cents // analytics.CENTS_PER_YUAN}"


def _span(minutes: int) -> str:
    if minutes < analytics.MINUTES_PER_HOUR:
        return f"{minutes} 分钟"
    hours = minutes // analytics.MINUTES_PER_HOUR
    if hours < HOURS_READABLE_UP_TO:
        return f"{hours} 小时"
    return f"{minutes // MINUTES_PER_DAY} 天"


def _percent(drop_bps: int) -> str:
    """Basis points to one decimal place, without touching a float."""
    return f"{drop_bps // 100}.{drop_bps % 100 // 10}%"
