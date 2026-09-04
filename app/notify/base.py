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
    | test.
    """

    kind: str
    hits: list[NotifiableHit]
    monitor_name: str | None = None

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
