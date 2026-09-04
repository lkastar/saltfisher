"""The write-after-send rule, and the isolation of channel failures.

`notified_at` is what stops an item being announced twice. Writing it before
the send means a crashed message is lost forever; writing it after risks at
most one duplicate. For an alerting tool that is the correct direction to fail
in, and this file is what keeps it that way.
"""

import pytest
from sqlmodel import Session, select

from app import scheduler
from app.models import Item, Monitor, MonitorChannel, MonitorHit, NotifyChannel, NotifyLog, Seller
from app.notify.base import Notification
from app.store import NotifiableHit

pytestmark = pytest.mark.asyncio


def hit(item_id: str = "i1", price_cents: int = 299900, reason: str = "new_in_range"):
    return NotifiableHit(
        item_id=item_id,
        title="iPhone 15",
        price_cents=price_cents,
        previous_price_cents=None,
        reason=reason,
        url=f"https://www.goofish.com/item?id={item_id}",
        cover_url=None,
        seller_nick="老王",
        unverified_labels=(),
    )


@pytest.fixture
def wired(monkeypatch):
    """A real database with one monitor, one item, one bound channel."""
    from tests.conftest import memory_engine

    engine = memory_engine()
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr("app.store.settings", scheduler.settings)

    with Session(engine) as s:
        monitor = Monitor(name="rule", keyword="iPhone 15", baseline_done=True)
        channel = NotifyChannel(kind="stub", label="stub", config="{}")
        s.add(monitor)
        s.add(channel)
        s.add(Seller(id="s1", nick="老王"))
        s.commit()
        s.add(Item(id="i1", title="iPhone 15", seller_id="s1", seller_nick="老王"))
        s.add(MonitorChannel(monitor_id=monitor.id, channel_id=channel.id))
        s.add(MonitorHit(monitor_id=monitor.id, item_id="i1", in_range=True))
        s.commit()
        return engine, monitor.id, channel.id


class Stub:
    kind = "stub"
    secret_fields = ()

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[Notification] = []

    async def send(self, notification, config):
        self.sent.append(notification)
        if self.error:
            raise self.error


async def test_successful_send_stamps_the_ledger(wired):
    engine, monitor_id, channel_id = wired
    stub = Stub()
    await scheduler.dispatch_hits({"stub": stub}, monitor_id, "rule", [hit()])

    with Session(engine) as s:
        row = s.exec(select(MonitorHit)).one()
        assert row.notified_at is not None
        assert row.notified_price_cents == 299900
        entry = s.exec(select(NotifyLog)).one()
        assert entry.ok is True and entry.error is None
    assert len(stub.sent) == 1


async def test_failed_send_leaves_the_ledger_unstamped(wired):
    """So the next cycle retries instead of the message being lost."""
    engine, monitor_id, channel_id = wired
    stub = Stub(error=OSError("smtp down"))
    await scheduler.dispatch_hits({"stub": stub}, monitor_id, "rule", [hit()])

    with Session(engine) as s:
        row = s.exec(select(MonitorHit)).one()
        assert row.notified_at is None
        assert row.notified_price_cents is None
        entry = s.exec(select(NotifyLog)).one()
        assert entry.ok is False and "smtp down" in (entry.error or "")


async def test_a_dead_channel_does_not_raise_into_the_cycle(wired):
    engine, monitor_id, _ = wired
    stub = Stub(error=RuntimeError("boom"))
    # no exception escapes
    await scheduler.dispatch_hits({"stub": stub}, monitor_id, "rule", [hit()])
    with Session(engine) as s:
        assert s.exec(select(NotifyLog)).one().ok is False


async def test_no_channels_bound_is_not_an_error(wired, monkeypatch):
    engine, monitor_id, channel_id = wired
    with Session(engine) as s:
        link = s.exec(select(MonitorChannel)).one()
        s.delete(link)
        s.commit()

    stub = Stub()
    await scheduler.dispatch_hits({"stub": stub}, monitor_id, "rule", [hit()])
    assert stub.sent == []
    with Session(engine) as s:
        # nothing sent, so nothing stamped: the hit stays pending
        assert s.exec(select(MonitorHit)).one().notified_at is None


async def test_empty_hit_list_sends_nothing(wired):
    engine, monitor_id, _ = wired
    stub = Stub()
    await scheduler.dispatch_hits({"stub": stub}, monitor_id, "rule", [])
    assert stub.sent == []
    with Session(engine) as s:
        assert s.exec(select(NotifyLog)).all() == []


async def test_reasons_are_sent_as_separate_messages(wired):
    engine, monitor_id, _ = wired
    with Session(engine) as s:
        s.add(Item(id="i2", title="iPhone 15 Pro", seller_id="s1", seller_nick="老王"))
        s.add(MonitorHit(monitor_id=monitor_id, item_id="i2", in_range=True))
        s.commit()

    stub = Stub()
    await scheduler.dispatch_hits(
        {"stub": stub},
        monitor_id,
        "rule",
        [hit("i1"), hit("i2", reason="price_drop")],
    )
    kinds = sorted(n.kind for n in stub.sent)
    assert kinds == ["new_in_range", "price_drop"]
    with Session(engine) as s:
        assert len(s.exec(select(NotifyLog)).all()) == 2
        assert all(r.notified_at is not None for r in s.exec(select(MonitorHit)).all())


async def test_one_failing_channel_does_not_block_another(wired):
    engine, monitor_id, _ = wired
    with Session(engine) as s:
        good = NotifyChannel(kind="good", label="good", config="{}")
        s.add(good)
        s.commit()
        s.add(MonitorChannel(monitor_id=monitor_id, channel_id=good.id))
        s.commit()

    bad, good_stub = Stub(error=OSError("down")), Stub()
    good_stub.kind = "good"
    await scheduler.dispatch_hits({"stub": bad, "good": good_stub}, monitor_id, "rule", [hit()])
    assert len(good_stub.sent) == 1
    with Session(engine) as s:
        entries = s.exec(select(NotifyLog)).all()
        assert sorted(e.ok for e in entries) == [False, True]
        # one channel succeeded, so the user was informed: stamp it
        assert s.exec(select(MonitorHit)).one().notified_at is not None
