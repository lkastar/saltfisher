"""Notification boundary types.

Adding a channel means adding one module that satisfies `Notifier`. Nothing
else in the codebase changes — the registry in __init__.py is a dict, not a
plugin loader.
"""

from dataclasses import dataclass
from typing import ClassVar, Protocol

from app.store import NotifiableHit


class ConfigError(Exception):
    """A channel's stored configuration is unusable.

    Distinct from a delivery failure: a bad config disables the channel and
    asks the user to fix it, while a delivery failure is retried by the next
    cycle.
    """


@dataclass(frozen=True, slots=True)
class Notification:
    """One message. Never one item — a cycle's hits are batched.

    `kind` doubles as the NotifyLog reason: new_in_range | price_drop | gone
    | test | challenge.

    `body` is free text rendered above the hit list. It exists for `challenge`,
    which has no hits at all -- the whole message IS the instruction. Without
    it a challenge alert would arrive as a subject line with an empty body.
    """

    kind: str
    hits: list[NotifiableHit]
    monitor_name: str | None = None
    body: str | None = None

    @property
    def title(self) -> str:
        count = len(self.hits)
        where = f"「{self.monitor_name}」" if self.monitor_name else ""
        if self.kind == "price_drop":
            return f"闲鱼降价 {count} 件{where}"
        if self.kind == "gone":
            return f"闲鱼收藏已下架 {count} 件"
        if self.kind == "test":
            return "闲鱼监控测试消息"
        if self.kind == "challenge":
            return "闲鱼采集已暂停：需要人工验证"
        return f"闲鱼新命中 {count} 件{where}"

    def sorted_hits(self) -> list[NotifiableHit]:
        """Cheapest first: the whole point is spotting the bargain."""
        return sorted(self.hits, key=lambda h: h.price_cents)


class Notifier(Protocol):
    kind: ClassVar[str]
    secret_fields: ClassVar[tuple[str, ...]]

    async def send(self, notification: Notification, config: dict) -> None:
        """Deliver, or raise. Raising is how dispatch() records a failure."""
        ...


def yuan(cents: int) -> str:
    """Cents to a display string. One implementation, so the backend and the
    frontend cannot round differently.
    """
    return f"{cents / 100:.2f}"


def describe(hit: NotifiableHit) -> str:
    """One plain-text line for a hit, shared by every channel."""
    parts = [f"¥{yuan(hit.price_cents)}"]
    if hit.previous_price_cents and hit.previous_price_cents > hit.price_cents:
        drop = hit.previous_price_cents - hit.price_cents
        parts.append(f"(↓{yuan(drop)})")
    parts.append(hit.title)
    if hit.seller_nick:
        parts.append(f"— {hit.seller_nick}")
    line = " ".join(parts)
    if hit.unverified_labels:
        # Without this the "conservatively let it through" policy silently
        # pushes items that do not actually meet the stated filters.
        line += f"  [{'/'.join(hit.unverified_labels)}]"
    return line


# --------------------------------------------------------------------------- #
# Risk-control challenge
# --------------------------------------------------------------------------- #

# The recovery steps, worded exactly as docs/operations.md「唯一的人工前置：导入
# 采集凭证」. Kept here as the single copy the user ever reads at 2am -- the doc
# is for someone already at a terminal, this is for someone whose phone just
# buzzed, and two hand-written variants of a five-step procedure drift apart on
# the first upstream change. Edit both together.
CHALLENGE_STEPS = (
    "1. 浏览器登录闲鱼，打开一个商品详情页（goofish.com/item?id=…）\n"
    "2. 出现滑块就完成它，然后刷新\n"
    "3. 开发者工具 → Network → 过滤 detail → 找到 mtop.taobao.idle.pc.detail\n"
    "4. 点它 → Headers → Request Headers → 复制 `Cookie:` 那一整行\n"
    "5. 面板「设置」页粘贴 → 导入"
)

# The upstream's own words, truncated. A ret envelope is short; anything longer
# is an interstitial page we do not want to forward into an email.
MAX_DETAIL_CHARS = 200


def challenge_body(detail: str) -> str:
    """The one actionable message for a challenged session.

    Carries no credential: `detail` is either an API name or the upstream `ret`
    string, never a cookie or a token.
    """
    return (
        "闲鱼风控要求人工验证，采集已暂停。\n"
        f"被挑战的端点：{detail[:MAX_DETAIL_CHARS]}\n"
        "相关监控规则已自动停用，在重新导入凭证之前不会再采集。\n"
        "\n"
        "自动重试不会好转——容器里没法点滑块，只能在你自己的浏览器上做一次：\n"
        "\n"
        f"{CHALLENGE_STEPS}\n"
        "\n"
        "不要按 cookie 名字找，就从上面那个请求整行复制。\n"
        "\n"
        "导入后被风控停用的规则会自动恢复。但判定成功看行为，不看 cookie 名单：\n"
        "去「监控任务」点一次「立即运行」，看到采集成功才算真的恢复。"
    )
