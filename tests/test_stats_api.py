"""GET /api/stats/overview: the overview KPI row.

The point of the endpoint is counting without the 200-row list caps, so the
tests here are about which rows land in which bucket — the UTC day boundary,
search vs watch cycles, and the per-channel push split.
"""

from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_session
from app.main import app
from app.models import CollectRun, Item, Monitor, NotifyChannel, NotifyLog, Seller

AUTH = {"Authorization": "Bearer testtoken123"}
NOW = datetime.now(UTC)
# Today's UTC midnight — the day boundary the endpoint documents.
TODAY_START = datetime.combine(NOW.date(), time.min, tzinfo=UTC)


def seed_item(s: Session, item_id: str, first_seen: datetime) -> None:
    s.add(
        Item(
            id=item_id,
            title=f"item {item_id}",
            seller_id="s1",
            seller_nick="老王",
            first_seen_at=first_seen,
            last_seen_at=NOW,
        )
    )


@pytest.fixture
def client():
    from tests.conftest import memory_engine

    engine = memory_engine()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def test_auth_required(client):
    c, _ = client
    assert c.get("/api/stats/overview").status_code == 401


def test_empty_database_is_zeros_not_an_error(client):
    c, _ = client
    body = c.get("/api/stats/overview", headers=AUTH).json()
    assert body["runs_24h"] == {"total": 0, "failed": 0}
    assert body["hits"] == {"today": 0, "yesterday": 0, "avg_7d": 0.0}
    assert body["pushes_24h"]["total"] == 0
    assert body["items_total"] == 0
    assert len(body["daily"]) == 14
    assert all(d["new_hits"] == 0 for d in body["daily"])


def test_hits_cut_on_the_utc_day_boundary(client):
    """An item first seen one second before UTC midnight is yesterday's, one
    second after is today's — the documented boundary, not a rolling 24h."""
    c, engine = client
    with Session(engine) as s:
        s.add(Seller(id="s1", nick="老王"))
        seed_item(s, "just-today", TODAY_START + timedelta(seconds=1))
        seed_item(s, "just-yesterday", TODAY_START - timedelta(seconds=1))
        seed_item(s, "old", TODAY_START - timedelta(days=30))
        s.commit()

    body = c.get("/api/stats/overview", headers=AUTH).json()
    assert body["hits"]["today"] == 1
    assert body["hits"]["yesterday"] == 1
    # 7-day mean counts the two in-window items over 7 days, today included.
    assert body["hits"]["avg_7d"] == pytest.approx(2 / 7)
    # items_total is cumulative: the 30-day-old item still counts.
    assert body["items_total"] == 3
    assert body["daily"][-1]["new_hits"] == 1
    assert body["daily"][-2]["new_hits"] == 1


def test_runs_24h_counts_search_and_watch_cycles(client):
    """CollectRun's two foreign keys are mutually exclusive; the KPI must not
    silently cover only the search half."""
    c, engine = client
    with Session(engine) as s:
        s.add(Monitor(name="rule", keyword="x", interval_seconds=300))
        s.add(Seller(id="s1", nick="老王"))
        seed_item(s, "w1", NOW - timedelta(days=2))
        s.commit()
        # A search cycle, a failed search cycle, a watch cycle — all inside 24h.
        s.add(CollectRun(monitor_id=None, item_id="w1", started_at=NOW, ok=True))
        s.add(CollectRun(monitor_id=1, started_at=NOW - timedelta(hours=1), ok=True))
        s.add(CollectRun(monitor_id=1, started_at=NOW - timedelta(hours=2), ok=False, error="x"))
        # Outside the rolling window: not in runs_24h, still in daily.
        s.add(CollectRun(monitor_id=1, started_at=NOW - timedelta(hours=30), ok=False, error="x"))
        s.commit()

    body = c.get("/api/stats/overview", headers=AUTH).json()
    assert body["runs_24h"] == {"total": 3, "failed": 1}
    assert sum(d["runs_total"] for d in body["daily"]) == 4
    assert sum(d["runs_failed"] for d in body["daily"]) == 2


def test_pushes_split_by_outcome_and_channel_kind(client):
    c, engine = client
    with Session(engine) as s:
        s.add(NotifyChannel(kind="email", label="邮箱", config="{}"))
        s.add(NotifyChannel(kind="telegram", label="tg", config="{}"))
        s.commit()
        s.add(NotifyLog(channel_id=1, kind="new_in_range", item_count=1, ok=True, sent_at=NOW))
        s.add(
            NotifyLog(
                channel_id=1,
                kind="price_drop",
                item_count=1,
                ok=False,
                error="smtp down",
                sent_at=NOW - timedelta(hours=3),
            )
        )
        s.add(
            NotifyLog(
                channel_id=2,
                kind="new_in_range",
                item_count=2,
                ok=True,
                sent_at=NOW - timedelta(hours=6),
            )
        )
        # Outside 24h: excluded from the KPI, present in the daily series.
        s.add(
            NotifyLog(
                channel_id=2,
                kind="new_in_range",
                item_count=1,
                ok=True,
                sent_at=NOW - timedelta(hours=30),
            )
        )
        s.commit()

    body = c.get("/api/stats/overview", headers=AUTH).json()
    assert body["pushes_24h"] == {"total": 3, "ok": 2, "failed": 1, "email": 2, "telegram": 1}
    assert sum(d["pushes"] for d in body["daily"]) == 4


def test_daily_is_14_utc_days_ending_today(client):
    c, _ = client
    body = c.get("/api/stats/overview", headers=AUTH).json()
    dates = [d["date"] for d in body["daily"]]
    assert dates[-1] == NOW.date().isoformat()
    assert dates == sorted(dates) and len(set(dates)) == 14
