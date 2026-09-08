"""The `item` scenario's input assembly.

One listing, its price history, its seller, the user's own note and the market
statistics of one keyword, rendered into the item prompt. Five inputs, five
existing producers — nothing here invents a query except the one that did not
exist:

**There is no item -> keyword lookup, and the relation is one-to-many.**
Measured overlap (`analytics.py:19-24`): `iPhone 15` saw 264 listings and
`iPhone 15 128G` saw 59, only 29 of them shared, and the two keywords sit at
different price levels. So picking a keyword is picking which market to
compare against, and design §5 decides it: **the keyword of the rule whose
`MonitorHit.first_hit_at` is earliest for this listing**, because the rule
that found it first is the one the user was most likely actually shopping for.
The choice is echoed to the caller and printed in the prompt, since a
comparison whose yardstick is invisible cannot be judged.

An item can also be in no ledger at all — a watchlist entry added by pasting a
link is exactly that — and it still has to produce an answer. It gets no
statistics section and is told so, rather than being compared against nothing.

**Not cached, ever.** Price and seller state move, and a cached verdict on a
listing that has since dropped 800 yuan is worse than no verdict.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from app.analytics import price_distribution
from app.llm.base import LlmRequest
from app.llm.images import ImageBatch, fetch, supports_vision
from app.llm.prompts import render
from app.models import Item, Monitor, MonitorHit, PriceSnapshot, Seller, Watchlist
from app.store import NEWEST_FIRST, item_url

log = logging.getLogger(__name__)

# The window the keyword's statistics are taken over. Same default as the
# analytics page (`api/analytics.py`), so the numbers in an advice match the
# numbers the user can see next to it.
STATS_WINDOW_DAYS = 7
# Price points sent, newest last. A watched listing polls every few minutes
# but only writes a snapshot when the price CHANGES (`models.PriceSnapshot`),
# so 40 rows is a long history rather than a long afternoon.
MAX_PRICE_POINTS = 40
# Goofish descriptions run long and the whole thing is paid for by the token.
# ponytail: a flat truncation. Summarising the tail would need a second model
# call, which costs more than the tail is worth.
MAX_DESCRIPTION_CHARS = 1000

# The user-editable template is the USER message, and this is the system one:
# a user rewriting the prompt has to be able to change the role, the output
# schema and the tone, and these two sentences are the only things they cannot
# delete by accident. Keeping the role sentence in the template rather than
# here is deliberate for the same reason.
SYSTEM = (
    "你只依据用户给出的资料作答，不编造资料里没有的事实。只输出用户要求的 JSON，不要输出别的内容。"
)

MISSING_INTENT = "（用户没有填写备注，按通用的二手捡漏标准判断）"
NO_KEYWORD = "（这件商品不在任何关键词的采集台账里）"
NO_KEYWORD_STATS = (
    "这件商品不在任何关键词的采集台账里（例如是贴链接单独加入观察的），"
    "没有同期行情统计可以参照。请只依据商品本身、价格变化和卖家资料作答，"
    "不要凭空假设市场价。"
)


class ItemAdvice(BaseModel):
    """The JSON `prompts.ITEM_PROMPT` asks for.

    These field names ARE the contract with that template: rename one here and
    the prompt keeps asking for the old name, which no retry can fix.

    `verdict` is `str` and not a three-value Literal on purpose. A model that
    answers 「谨慎考虑」 instead of 「可考虑」 has understood the task; failing
    validation over a synonym spends a second call and then shows the user
    「输出格式不符合预期，请检查提示词」 about a prompt that worked.

    The two prices are floats, which is not the money-float the guidelines
    ban: nothing here is arithmetic on stored money, it is the model's opinion
    on its way to being displayed. `int` would reject a perfectly good 8100.5.
    """

    verdict: str
    fair_price_yuan: float
    offer_price_yuan: float
    risks: list[str] = Field(default_factory=list)
    summary: str


@dataclass(frozen=True, slots=True)
class ItemInput:
    """A rendered request plus the two things the caller has to echo.

    `keyword` is the yardstick the statistics came from (None when the listing
    is in no ledger), and `notes` are user-facing sentences about inputs that
    did NOT make it in — a degraded image path above all. An answer that
    silently dropped the photos looks identical to one that read them.
    """

    request: LlmRequest
    keyword: str | None
    notes: tuple[str, ...]


def keyword_for(session: Session, item_id: str) -> str | None:
    """The keyword of the rule that saw this listing first, or None.

    The one query the `item` path adds. Ordered by `first_hit_at` with the
    monitor id breaking ties, because two rules that first match a listing in
    the same cycle can share a timestamp to the microsecond and "whichever
    row SQLite felt like" is not a decision.

    None means no rule ever matched it, which is the normal state of a
    listing added by pasting a link.
    """
    return session.exec(
        select(col(Monitor.keyword))
        .join(MonitorHit, col(MonitorHit.monitor_id) == col(Monitor.id))
        .where(MonitorHit.item_id == item_id)
        .order_by(col(MonitorHit.first_hit_at).asc(), col(Monitor.id).asc())
        .limit(1)
    ).first()


def image_urls(item: Item) -> list[str]:
    """Photo URLs for this listing, cover first.

    Falls back to `cover_url` when the JSON column is empty or malformed: a
    surprise from upstream should cost the gallery, not the picture we know we
    have (`api/items.py:_json_list` degrades the same way, and this module is
    not allowed to import from `api/`).
    """
    urls: list[str] = []
    if item.image_urls:
        try:
            parsed = json.loads(item.image_urls)
        except (TypeError, ValueError):
            log.warning("unparseable image_urls", extra={"item_id": item.id})
            parsed = []
        if isinstance(parsed, list):
            urls = [str(url) for url in parsed if url]
    if not urls and item.cover_url:
        urls = [item.cover_url]
    return list(dict.fromkeys(urls))


async def build_request(
    session: Session,
    client: httpx.AsyncClient,
    item: Item,
    *,
    template: str,
    model: str,
    send_images: bool,
) -> ItemInput:
    """Assemble the prompt for one listing. Never raises for missing inputs.

    Every section degrades to a sentence saying what is missing instead of
    being left out: a blank `{seller}` reads to the model as "no concerns
    here", which is the opposite of what an unfetched profile means.
    """
    keyword = keyword_for(session, item.id)
    snapshots = _price_points(session, item.id)
    batch = await _images(client, item, model=model, send_images=send_images)
    watched = session.get(Watchlist, item.id)

    user_text = render(
        template,
        {
            "keyword": keyword or NO_KEYWORD,
            "user_intent": (watched.note if watched and watched.note else MISSING_INTENT),
            "item": _item_section(item, snapshots, images_sent=len(batch.images)),
            "price_history": _price_section(snapshots),
            "seller": _seller_section(session.get(Seller, item.seller_id), item),
            "stats": _stats_section(session, keyword),
        },
    )
    log.info(
        "llm item input built",
        extra={
            # No prompt text and no title: `logging-guidelines.md` keeps item
            # titles out of the log, and the prompt is user content.
            "item_id": item.id,
            "keyword": keyword,
            "price_points": len(snapshots),
            "images": len(batch.images),
            "chars": len(user_text),
        },
    )
    return ItemInput(
        request=LlmRequest(system=SYSTEM, user_text=user_text, images=batch.images),
        keyword=keyword,
        notes=batch.notes,
    )


async def _images(
    client: httpx.AsyncClient, item: Item, *, model: str, send_images: bool
) -> ImageBatch:
    """Photos, or the reason there are none. Degradation is always stated."""
    if not send_images:
        return ImageBatch(notes=("场景配置里没有开启发送图片，本次只依据文字资料分析。",))
    if not supports_vision(model):
        return ImageBatch(
            notes=(f"模型 {model} 看起来不支持图片输入，已降级为纯文本分析。",),
        )
    urls = image_urls(item)
    if not urls:
        return ImageBatch(notes=("这件商品没有可用的图片 URL，本次只依据文字资料分析。",))
    batch = await fetch(client, urls)
    if batch.images:
        return batch
    # Every URL was rejected. The per-image notes say why; this line is what
    # makes it unmissable that the verdict saw no picture at all.
    return ImageBatch(notes=(*batch.notes, "没有任何图片进入模型，本次结论只依据文字资料。"))


def _price_points(session: Session, item_id: str) -> list[PriceSnapshot]:
    """Price history oldest-first.

    The newest `MAX_PRICE_POINTS` taken and then reversed, exactly like
    `api/items.py:item_prices`: capping an ascending query returns the OLDEST
    points, i.e. a history that stops before the present price.
    """
    rows = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.item_id == item_id)
        .order_by(*NEWEST_FIRST)
        .limit(MAX_PRICE_POINTS)
    ).all()
    return list(reversed(rows))


def _item_section(item: Item, snapshots: list[PriceSnapshot], *, images_sent: int) -> str:
    current = snapshots[-1] if snapshots else None
    observed = item.last_seen_at - item.first_seen_at
    return _lines(
        [
            ("标题", item.title),
            ("描述", _clip(item.description, MAX_DESCRIPTION_CHARS)),
            ("当前价", _yuan(current.price_cents) if current else "未采集到价格"),
            ("状态", item.status),
            ("地区", item.region),
            ("发布时间", _when(item.publish_time)),
            ("我们第一次看到它", _when(item.first_seen_at)),
            ("我们最后一次看到它", _when(item.last_seen_at)),
            # NOT "on sale for N hours": it is how long OUR searches kept
            # returning it, which `analytics.py` is careful never to call a
            # time to sale. The model must not narrate it as one either.
            ("我们观察到它的时长", f"{round(observed.total_seconds() / 3600, 1)} 小时"),
            ("链接", item_url(item.id)),
            ("随本次请求发送的图片", f"{images_sent} 张" if images_sent else "无"),
        ]
    )


def _price_section(snapshots: list[PriceSnapshot]) -> str:
    if not snapshots:
        return "没有价格记录。"
    if len(snapshots) == 1:
        # Worth saying out loud: one point is the normal state of a listing
        # seen once, and a model handed a single number tends to describe it
        # as "stable".
        head = "只有 1 条价格记录（我们只观察到它一次，看不出涨跌）："
    else:
        first, last = snapshots[0].price_cents, snapshots[-1].price_cents
        moved = "没有变化" if first == last else f"{_yuan(first)} → {_yuan(last)}"
        head = f"共 {len(snapshots)} 条记录，期间{moved}："
    body = "\n".join(_price_line(row) for row in snapshots)
    return f"{head}\n{body}"


def _price_line(row: PriceSnapshot) -> str:
    when, price = _when(row.captured_at), _yuan(row.price_cents)
    return f"- {when} {price} {row.status}{_counts(row)}（来源 {row.source}）"


def _counts(row: PriceSnapshot) -> str:
    parts = []
    if row.want_count is not None:
        parts.append(f"想要 {row.want_count}")
    if row.view_count is not None:
        parts.append(f"浏览 {row.view_count}")
    return f"，{'、'.join(parts)}" if parts else ""


def _seller_section(seller: Seller | None, item: Item) -> str:
    # Profile fields stay absent when None rather than reading as 0: "0 条评价"
    # is a warning sign, "not fetched" is a gap. Same rule as
    # `api/items.py:_public`, and the reason `models.py` keeps them Optional.
    if seller is None:
        return f"只知道昵称「{item.seller_nick}」，没有卖家资料。"
    lines = _lines(
        [
            ("昵称", seller.nick),
            ("是否商家", _yes_no(seller.is_shop)),
            ("信用等级", _num(seller.credit_level)),
            ("评价数", _num(seller.review_count)),
            ("好评率", f"{seller.positive_rate}%" if seller.positive_rate is not None else None),
            ("已售件数", _num(seller.sold_count)),
            ("实名认证", _yes_no(seller.verified)),
            ("账号年龄", f"{seller.account_age_days} 天" if seller.account_age_days else None),
            # Percent, on the evidence available: the only value in the real
            # database is 100.0, sitting next to a positive_rate of 97.0 on
            # the same row, and upstream spells the key `replyRate`. Printing
            # a bare "100.0" leaves the model to invent the unit, which is
            # worse than a label that can be corrected. If a 0-1 value ever
            # shows up here, this is the line to fix.
            (
                "回复率",
                f"{seller.reply_rate}%" if seller.reply_rate is not None else None,
            ),
        ]
    )
    if seller.fetched_at is None:
        lines += "\n- 备注：卖家资料页从未抓取过，上面只有搜索结果里带的字段，缺项不代表是 0。"
    return lines


def _stats_section(session: Session, keyword: str | None) -> str:
    """The keyword's price distribution, or the reason there is none.

    Reuses `analytics.price_distribution` rather than querying prices here:
    an advice that quoted a different median than the analytics page beside it
    would be two answers to one question.
    """
    if keyword is None:
        return NO_KEYWORD_STATS

    stats = price_distribution(session, keyword, days=STATS_WINDOW_DAYS)
    # The keyword is printed in the prompt, not only returned to the caller:
    # one listing can sit in two keywords' ledgers at different price levels,
    # so an answer has to say which market it compared against.
    head = (
        f"参照关键词：「{keyword}」（这件商品最早是被这条规则发现的）。"
        f"以下是它最近 {stats['window_days']} 天的挂牌价统计，"
        f"样本 {stats['sample_size']} 件（其中仍在售 {stats['fresh_size']} 件），"
        f"该关键词累计采集 {stats['data_days']} 天。"
    )
    quantiles = stats["quantiles"]
    # Two separate emptiness tests, because they are two different states and
    # neither implies the other. `sample_size` 0 is "nothing in the window";
    # empty `quantiles` with a non-zero sample is `analytics._quantiles`
    # declining to interpolate a distribution out of ONE listing, which is an
    # entirely ordinary state for a narrow keyword. Testing only the sample
    # size and then indexing p10 is a KeyError, i.e. a 500 on a keyword whose
    # only sin is being specific.
    if not stats["sample_size"]:
        return f"{head}\n窗口内没有样本，这个关键词的行情无法参照，请不要据此判断价格水位。"
    if not quantiles:
        return (
            f"{head}\n样本太少（不足 2 件），算不出分位数。"
            "请不要据此判断价格水位，只依据商品本身和卖家资料作答。"
        )
    body = "、".join(
        f"{name.upper()} {_yuan(quantiles[name])}" for name in ("p10", "p25", "p50", "p75", "p90")
    )
    return (
        f"{head}\n- 分位数：{body}\n"
        "- 这些数字只代表我们对该关键词的搜索看到过的挂牌价，不是成交价，也不是整个市场。"
    )


def _lines(pairs: list[tuple[str, str | None]]) -> str:
    return "\n".join(f"- {name}：{value}" for name, value in pairs if value)


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit]}……（描述过长已截断）"


def _yuan(cents: int) -> str:
    """Money is int cents everywhere; yuan appear only in text for the model."""
    return f"¥{cents / 100:.2f}"


def _when(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else None


def _yes_no(value: bool | None) -> str | None:
    return None if value is None else ("是" if value else "否")


def _num(value: int | None) -> str | None:
    return None if value is None else str(value)
