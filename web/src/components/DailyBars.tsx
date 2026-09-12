import type { SupplyDay } from "../api/queries";
import { firstWatchedIndex } from "../lib/supplyTrend";
import {
  bandPath,
  dayTicks,
  rollingBand,
  scale,
  smoothPath,
  stateColumns,
  type TrendState,
} from "../lib/chart";

/** New listings per day as the prototype's smoothed trend, with the days we
 *  did not collect drawn as such.
 *
 *  The whole point of the chart is the difference between "the market was
 *  quiet" and "we were not watching". Both are zero new listings, and drawn
 *  the same way the chart reports a dead market during an outage — so a quiet
 *  collected day gets a dot sitting on the axis and an uncollected day gets a
 *  hatched column across the full height (`stateColumns` in lib/chart.ts).
 *
 *  Hatching, not a colour: a colour-blind user or a greyscale print gets the
 *  pattern either way (`styling-guidelines.md` — colour never carries meaning
 *  alone).
 *
 *  Two DIFFERENT hatches for "not collected", because they are two different
 *  facts. `runs_failed > 0` means we tried and failed that day; no runs at
 *  all means there is simply no record — every day before the run log existed
 *  is in that state, and drawing those as failures turns the entire history
 *  into one long fake outage.
 *
 *  The smoothing interpolates, so the band behind the line never leaves the
 *  observed values and the table below stays the exact-values source.
 */

const W = 720;
const H = 180;
const PAD = { top: 14, right: 14, bottom: 30, left: 44 };
const PLOT = {
  left: PAD.left,
  right: W - PAD.right,
  top: PAD.top,
  bottom: H - PAD.bottom,
};

function stateOf(day: SupplyDay): TrendState {
  if (day.collected) return "ok";
  return day.runs_failed > 0 ? "fail" : "idle";
}

const PRE_HISTORY = "尚未监控";

const LEGEND: Record<TrendState, string> = {
  ok: "采集正常",
  fail: "采集失败",
  idle: "无采集记录",
};

export default function DailyBars({
  days,
  dataDays,
}: {
  days: SupplyDay[];
  /** From the API's `data_days`: how many days of history this keyword has. */
  dataDays: number;
}) {
  const lastDay = days[days.length - 1];
  if (!lastDay) return null;

  const watchedFrom = firstWatchedIndex(days, dataDays);

  const values = days.map((d) => d.new_count);
  const states = days.map(stateOf);
  const peak = Math.max(...values);
  const total = values.reduce((sum, v) => sum + v, 0);
  const failed = states.filter((s) => s === "fail").length;
  const idle = states.filter((s) => s === "idle").length;
  const ticks = new Set(dayTicks(days.length));

  const x = (i: number) => scale(i, 0, days.length - 1, PLOT.left, PLOT.right);
  // Math.max(peak, 1): a window where nothing was ever new has peak 0, and
  // scale() centres a zero-width domain -- the axis would float mid-chart.
  const y = (v: number) =>
    scale(v, 0, Math.max(peak, 1), PLOT.bottom, PLOT.top);
  const pixels = values.map((v, i) => ({ x: x(i), y: y(v) }));
  const lastPixel = pixels[pixels.length - 1]!;

  // A single observation has no line; extend it flat so the chart is not an
  // empty box, which reads as "no data" rather than "one observed day".
  const linePath =
    days.length === 1
      ? `M ${PLOT.left} ${lastPixel.y} L ${PLOT.right} ${lastPixel.y}`
      : smoothPath(pixels);

  const band = rollingBand(values);
  const areaPath =
    days.length < 2
      ? null
      : bandPath(
          band.upper.map((v, i) => ({ x: pixels[i]!.x, y: y(v) })),
          band.lower.map((v, i) => ({ x: pixels[i]!.x, y: y(v) })),
        );

  const columns = stateColumns(states, PLOT);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
      }}
    >
      <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`${days.length} 天内共新增 ${total} 件商品，最多一天 ${peak} 件；其中 ${idle} 天没有采集记录、${failed} 天采集失败，详细数值见下方表格`}
          // 30 days need the room; under this the days merge into a smear.
          // The wrapper scrolls, not the page.
          style={{ display: "block", minWidth: 560 }}
        >
          <defs>
            <pattern
              id="sfd-hatch-idle"
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
            <pattern
              id="sfd-hatch-fail"
              width="4"
              height="4"
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <line
                x1="0"
                y1="0"
                x2="0"
                y2="4"
                stroke="var(--red)"
                strokeWidth="1.5"
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
              fill={`url(#sfd-hatch-${col.state})`}
              opacity={col.state === "fail" ? 0.6 : 0.45}
            />
          ))}

          <line
            x1={PLOT.left}
            y1={PLOT.bottom}
            x2={PLOT.right}
            y2={PLOT.bottom}
            stroke="var(--line2)"
          />
          {[...new Set([peak, 0])].map((count) => (
            <text
              key={count}
              x={PLOT.left - 8}
              y={y(count) + 4}
              textAnchor="end"
              fontSize="11"
              fill="var(--text3)"
              fontFamily="var(--font-mono)"
            >
              {count}
            </text>
          ))}

          {areaPath === null ? null : (
            <path d={areaPath} fill="var(--chart-band)" stroke="none" />
          )}
          <path
            d={linePath}
            fill="none"
            stroke="var(--acc)"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
          {/* Dots mark OBSERVED days only: a collected zero-day gets its dot
              on the axis, which is exactly what tells it apart from the
              hatched columns behind the line. */}
          {days.map((day, i) =>
            states[i] === "ok" ? (
              <circle
                key={day.date}
                cx={pixels[i]!.x}
                cy={pixels[i]!.y}
                r="3"
                fill="var(--chart-dot-fill)"
                stroke="var(--acc)"
                strokeWidth="1.6"
              />
            ) : null,
          )}
          <circle
            cx={lastPixel.x}
            cy={lastPixel.y}
            r="6.5"
            fill="none"
            stroke="var(--acc)"
            strokeWidth="1"
            opacity="0.5"
          />

          {days.map((day, i) =>
            ticks.has(i) ? (
              <text
                key={day.date}
                x={pixels[i]!.x}
                y={H - 10}
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
      </div>

      <ul className="legend">
        <li className="lg">
          <span className="sw sw-line" />
          每日新增（平滑）
        </li>
        <li className="lg">
          <span className="sw sw-dot" />
          观测日
        </li>
        <li className="lg">
          <span className="sw sw-band" />
          滚动高低区间带
        </li>
        <li className="lg">
          <span className="sw sw-hatch" />
          无采集记录（斜纹）
        </li>
        <li className="lg">
          <span className="sw sw-hatch-red" />
          采集失败（密斜纹）
        </li>
      </ul>

      <div className="table-scroll">
        <table>
          <caption
            className="muted"
            style={{
              captionSide: "top",
              textAlign: "left",
              padding: "var(--space-2)",
              fontSize: 12,
            }}
          >
            每天首次见到的商品数。日界按 UTC
            切分，不是本地时区。曲线经过每个观测点，点与点之间是插值；精确数值以本表为准。
          </caption>
          <thead>
            <tr>
              <th>日期（UTC）</th>
              <th style={{ textAlign: "right" }}>新增</th>
              <th>采集</th>
              <th style={{ textAlign: "right" }}>成功 / 失败</th>
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map((day, reversedIndex) => {
              const state = stateOf(day);
              const preHistory = days.length - 1 - reversedIndex < watchedFrom;
              return (
                <tr key={day.date}>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {day.date}
                  </td>
                  <td className="num">{day.new_count}</td>
                  <td>
                    {preHistory ? (
                      <span className="muted">{PRE_HISTORY}</span>
                    ) : state !== "fail" ? (
                      // --warn is reserved for risk control and a degraded
                      // collector (styling-guidelines.md). Thirty rows of
                      // "no record" is history, not an alarm, and spending
                      // yellow on it drowns the case that matters.
                      <span className="muted">{LEGEND[state]}</span>
                    ) : (
                      <span className="pill" data-tone="danger">
                        {LEGEND.fail}
                      </span>
                    )}
                  </td>
                  <td className="num muted">
                    {day.runs_ok} / {day.runs_failed}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
