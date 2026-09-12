import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import {
  monitorTrendOptions,
  type Monitor,
  type MonitorTrendDay,
} from "../api/queries";
import {
  bandPath,
  dayTicks,
  scale,
  smoothPath,
  stateColumns,
  type TrendState,
} from "../lib/chart";
import { formatPrice } from "../lib/format";
import {
  defaultMonitorId,
  isPlotted,
  meanSegments,
  trimToObserved,
} from "../lib/monitorTrend";
import { useReveal } from "../lib/fx";
import { Icon } from "./Icon";
import { Empty, ErrorState, Loading } from "./States";

/** The price level of what one rule watches, day by day.
 *
 *  One rule at a time, chosen in the header. Two rules watch different
 *  products, so overlaying their means would be arithmetic over unrelated
 *  things -- the y-axis would belong to neither.
 *
 *  Three layers, in order of how much they can be trusted:
 *
 *  1. The p25-p75 band is exact. The server computes real quantiles per day
 *     (`analytics._quantiles`, inclusive method), so the band can never leave
 *     the prices actually observed that day.
 *  2. The mean dots are exact, one per day.
 *  3. The line between them interpolates. It passes through every dot, but
 *     the curve between two days is drawing, not data.
 *
 *  Unlike the analytics charts this one carries no data table -- an overview
 *  card is a glance, and thirty rows under it is the analytics page's job.
 *  The header numbers and the SVG's aria-label carry the exact figures
 *  instead, and the band above is the layer that keeps the picture honest.
 *
 *  A day with no successful cycle is hatched across the full height rather
 *  than interpolated over: "the market held steady" and "we were not looking"
 *  must not look the same, the same distinction DailyBars draws.
 */

const W = 720;
const H = 170;
const PAD = { top: 14, right: 14, bottom: 28, left: 64 };
const PLOT = {
  left: PAD.left,
  right: W - PAD.right,
  top: PAD.top,
  bottom: H - PAD.bottom,
};

function Chart({ days }: { days: MonitorTrendDay[] }) {
  const observed = days.filter(isPlotted);
  const lows = observed.map((d) => d.p25_cents ?? d.mean_cents);
  const highs = observed.map((d) => d.p75_cents ?? d.mean_cents!);
  const lo = Math.min(...lows);
  const hi = Math.max(...highs);

  const x = (i: number) =>
    scale(i, 0, Math.max(days.length - 1, 1), PLOT.left, PLOT.right);
  // A flat window (every day the same price) would divide by a zero-width
  // domain; scale() centres it, which is the right picture for "unchanged".
  const y = (cents: number) => scale(cents, lo, hi, PLOT.bottom, PLOT.top);

  const segments = meanSegments(days);
  const states: TrendState[] = days.map((d) => (d.collected ? "ok" : "idle"));
  const columns = stateColumns(states, PLOT);
  const ticks = new Set(dayTicks(days.length));
  const last = observed[observed.length - 1]!;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height={H}
      role="img"
      aria-label={`${days.length} 天内均价从 ${formatPrice(observed[0]!.mean_cents)} 变化到 ${formatPrice(last.mean_cents)}，区间 ${formatPrice(lo)} 到 ${formatPrice(hi)}，其中 ${states.filter((s) => s !== "ok").length} 天没有成功采集`}
      // 30 days need the room; below this the days merge into a smear and the
      // wrapper scrolls instead of the page.
      style={{ display: "block", minWidth: 560 }}
    >
      <defs>
        <pattern
          id="sfd-trend-idle"
          width="6"
          height="6"
          patternTransform="rotate(45)"
          patternUnits="userSpaceOnUse"
        >
          <line
            x1="0"
            y1="0"
            x2="0"
            y2="6"
            stroke="var(--border-strong)"
            strokeWidth="1"
          />
        </pattern>
      </defs>

      {columns.map((col) => (
        <rect
          key={col.x}
          x={col.x}
          y={col.y}
          width={col.width}
          height={col.height}
          fill="url(#sfd-trend-idle)"
          opacity={0.45}
        />
      ))}

      {[...new Set([hi, lo])].map((cents) => (
        <g key={cents}>
          <line
            x1={PLOT.left}
            y1={y(cents)}
            x2={PLOT.right}
            y2={y(cents)}
            stroke={cents === lo ? "var(--line2)" : "var(--line)"}
          />
          <text
            x={PLOT.left - 8}
            y={y(cents) + 4}
            textAnchor="end"
            fontSize="11"
            fill="var(--text3)"
            fontFamily="var(--font-mono)"
          >
            {formatPrice(cents)}
          </text>
        </g>
      ))}

      {segments.map((run) => {
        const upper = run.map((i) => ({
          x: x(i),
          y: y(days[i]!.p75_cents ?? days[i]!.mean_cents!),
        }));
        const lower = run.map((i) => ({
          x: x(i),
          y: y(days[i]!.p25_cents ?? days[i]!.mean_cents!),
        }));
        const line = run.map((i) => ({ x: x(i), y: y(days[i]!.mean_cents!) }));
        return (
          <g key={run[0]}>
            {run.length < 2 ? null : (
              <path
                d={bandPath(upper, lower)}
                fill="var(--chart-band)"
                stroke="none"
              />
            )}
            <path
              // A one-day run has no curve; a short flat stroke shows the dot
              // belongs to the series rather than floating unexplained.
              d={
                run.length === 1
                  ? `M ${line[0]!.x - 4} ${line[0]!.y} L ${line[0]!.x + 4} ${line[0]!.y}`
                  : smoothPath(line)
              }
              fill="none"
              stroke="var(--acc)"
              strokeWidth="1.8"
              strokeLinecap="round"
            />
          </g>
        );
      })}

      {/* Dots mark the days we actually have a mean for -- they are the data,
          the curve between them is not. */}
      {days.map((day, i) =>
        !isPlotted(day) ? null : (
          <circle
            key={day.date}
            cx={x(i)}
            cy={y(day.mean_cents)}
            r="3"
            fill="var(--chart-dot-fill)"
            stroke="var(--acc)"
            strokeWidth="1.6"
          >
            <title>{`${day.date} · 均价 ${formatPrice(day.mean_cents)} · ${day.listing_count} 件在售${day.collected ? "" : " · 当天无成功采集"}`}</title>
          </circle>
        ),
      )}

      {days.map((day, i) =>
        ticks.has(i) ? (
          <text
            key={day.date}
            x={x(i)}
            y={H - 9}
            textAnchor={
              i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"
            }
            fontSize="11"
            fill="var(--text3)"
            fontFamily="var(--font-mono)"
          >
            {day.date.slice(5)}
          </text>
        ) : null,
      )}
    </svg>
  );
}

export default function MonitorTrend({ monitors }: { monitors: Monitor[] }) {
  const reveal = useReveal();
  // Ephemeral, not URL state: the spec's URL rule is about filters on list
  // pages that get shared and reloaded. Which rule a dashboard card is
  // glancing at is neither.
  const [picked, setPicked] = useState<number | null>(null);
  const monitorId = picked ?? defaultMonitorId(monitors);
  const trend = useQuery({
    ...monitorTrendOptions(monitorId ?? 0),
    enabled: monitorId !== null,
  });

  // Trimmed, not the raw 30 days: the empty margin before a young rule's first
  // hit is most of the width and none of the information. See trimToObserved.
  const days = trimToObserved(trend.data?.days ?? []);
  const observed = days.filter(isPlotted);
  const latest = observed[observed.length - 1];
  const gaps = days.filter((d) => !d.collected).length;

  return (
    <section className="card" data-reveal ref={reveal}>
      <div className="card-h">
        <h2>
          <Icon name="chart-spline" size={15} />
          监控任务趋势
        </h2>
        <div className="actions">
          {monitors.length > 0 ? (
            <select
              aria-label="选择监控任务"
              value={monitorId ?? ""}
              onChange={(e) => setPicked(Number(e.target.value))}
            >
              {monitors.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}（{m.hit_count}）
                </option>
              ))}
            </select>
          ) : null}
          <span className="note">
            {days.length > 0 ? `${days.length}D · UTC` : "30D · UTC"}
          </span>
        </div>
      </div>

      {monitors.length === 0 ? (
        <Empty
          message="还没有监控任务，没有可画的行情。"
          action={<Link to="/monitors">去建一条</Link>}
        />
      ) : null}

      {monitorId !== null && trend.isPending ? <Loading rows={4} /> : null}
      {trend.isError ? (
        <ErrorState
          title="拉取趋势失败"
          error={trend.error}
          onRetry={() => void trend.refetch()}
        />
      ) : null}

      {trend.isSuccess && observed.length === 0 ? (
        <Empty message="这条规则还没有采集到在售商品，跑几轮之后这里就有行情了。" />
      ) : null}

      {trend.isSuccess && observed.length > 0 && latest ? (
        <>
          <div className="mono" style={{ fontSize: 12, color: "var(--text2)" }}>
            {/* "今日" would be a lie whenever the last day was not collected:
                `latest` is the newest day we actually measured, which may be
                several days back during an outage. Name the date instead. */}
            {latest.date.slice(5)} 均价{" "}
            <span style={{ color: "var(--text)", fontWeight: 600 }}>
              {formatPrice(latest.mean_cents)}
            </span>
            {latest.p25_cents !== null && latest.p75_cents !== null ? (
              <>
                {" · "}中间一半落在 {formatPrice(latest.p25_cents)}–
                {formatPrice(latest.p75_cents)}
              </>
            ) : null}
            {" · "}
            {latest.listing_count} 件在售
            {gaps > 0 ? <> · {gaps} 天无成功采集</> : null}
          </div>

          <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
            <Chart days={days} />
          </div>

          <ul className="legend">
            <li className="lg">
              <span className="sw sw-line" />
              每日均价（平滑）
            </li>
            <li className="lg">
              <span className="sw sw-dot" />
              观测日
            </li>
            <li className="lg">
              <span className="sw sw-band" />
              中间一半（p25–p75）
            </li>
            <li className="lg">
              <span className="sw sw-hatch" />
              当天无成功采集
            </li>
          </ul>
        </>
      ) : null}
    </section>
  );
}
