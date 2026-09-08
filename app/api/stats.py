"""Global overview stats behind the panel's KPI row.

One read-only endpoint over four tables. It exists because the overview page
used to derive these counts client-side from capped list endpoints
(`/api/items` and `/api/notify-logs` both stop at 200 rows) and had to render
"N+" whenever the cap cut the period. Counting server-side removes the cap
without changing any semantics: hits are still `Item.first_seen_at`, pushes
are still `NotifyLog` rows.

All windows are UTC — rolling 24h for the headline counts, UTC calendar-day
cuts for the daily series, consistent with the analytics endpoints.
"""

from datetime import timedelta

from fastapi import APIRouter
from sqlalchemy import func
from sqlmodel import col, select

from app.db import SessionDep
from app.models import CollectRun, Item, NotifyChannel, NotifyLog, utcnow
from app.schemas import OverviewDay, OverviewHits, OverviewPushes, OverviewRuns, StatsOverview

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Sparkline width. 14 days covers the 7-day averages twice over; the frontend
# slices what it draws.
DAILY_DAYS = 14


@router.get("/overview", response_model=StatsOverview)
def overview(session: SessionDep) -> StatsOverview:
    """The overview KPI row: runs, hits, pushes, cumulative items, daily series.

    `runs_24h` counts BOTH search and watch cycles — every `CollectRun` row,
    whichever of its two foreign keys is set. `hits` counts listings by
    `Item.first_seen_at` (global first sighting), the same fact the page
    previously counted client-side. Per-day rows exist for every day in the
    window, zeros included.
    """
    now = utcnow()
    day_cutoff = now - timedelta(hours=24)
    start = (now - timedelta(days=DAILY_DAYS - 1)).date()

    # ---- daily series: three GROUP BYs over UTC calendar days ---- #
    hit_day = func.date(col(Item.first_seen_at))
    hits_by_day: dict[str, int] = dict(
        session.exec(
            select(hit_day, func.count()).where(hit_day >= start.isoformat()).group_by(hit_day)
        ).all()
    )

    run_day = func.date(col(CollectRun.started_at))
    runs_by_day: dict[str, tuple[int, int]] = {
        row[0]: (int(row[1]), int(row[2]))
        for row in session.exec(
            select(run_day, func.count(), func.sum(func.iif(col(CollectRun.ok), 0, 1)))
            .where(run_day >= start.isoformat())
            .group_by(run_day)
        ).all()
    }

    push_day = func.date(col(NotifyLog.sent_at))
    pushes_by_day: dict[str, int] = dict(
        session.exec(
            select(push_day, func.count()).where(push_day >= start.isoformat()).group_by(push_day)
        ).all()
    )

    daily: list[OverviewDay] = []
    for offset in range(DAILY_DAYS):
        day = (start + timedelta(days=offset)).isoformat()
        runs_total, runs_failed = runs_by_day.get(day, (0, 0))
        daily.append(
            OverviewDay(
                date=day,
                new_hits=int(hits_by_day.get(day, 0)),
                runs_total=runs_total,
                runs_failed=runs_failed,
                pushes=int(pushes_by_day.get(day, 0)),
            )
        )

    # today / yesterday / 7-day mean fall out of the series just built —
    # daily[-1] is today by construction, and DAILY_DAYS >= 7.
    last7 = daily[-7:]
    hits = OverviewHits(
        today=daily[-1].new_hits,
        yesterday=daily[-2].new_hits,
        avg_7d=sum(d.new_hits for d in last7) / 7,
    )

    # ---- rolling 24h windows ---- #
    runs_row = session.exec(
        select(func.count(), func.sum(func.iif(col(CollectRun.ok), 0, 1))).where(
            col(CollectRun.started_at) >= day_cutoff
        )
    ).one()
    runs_24h = OverviewRuns(total=int(runs_row[0]), failed=int(runs_row[1] or 0))

    push_row = session.exec(
        select(func.count(), func.sum(func.iif(col(NotifyLog.ok), 1, 0))).where(
            col(NotifyLog.sent_at) >= day_cutoff
        )
    ).one()
    push_total = int(push_row[0])
    push_ok = int(push_row[1] or 0)
    by_kind: dict[str, int] = {
        row[0]: int(row[1])
        for row in session.exec(
            select(col(NotifyChannel.kind), func.count())
            .select_from(NotifyLog)
            .join(NotifyChannel, col(NotifyLog.channel_id) == col(NotifyChannel.id))
            .where(col(NotifyLog.sent_at) >= day_cutoff)
            .group_by(col(NotifyChannel.kind))
        ).all()
    }
    pushes_24h = OverviewPushes(
        total=push_total,
        ok=push_ok,
        failed=push_total - push_ok,
        email=by_kind.get("email", 0),
        telegram=by_kind.get("telegram", 0),
    )

    items_total = int(session.exec(select(func.count()).select_from(Item)).one())

    return StatsOverview(
        runs_24h=runs_24h,
        hits=hits,
        pushes_24h=pushes_24h,
        items_total=items_total,
        daily=daily,
    )
