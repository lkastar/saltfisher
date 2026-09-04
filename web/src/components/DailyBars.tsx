import type { SupplyDay } from "../api/queries";
import { bucketRects, dayTicks, scale } from "../lib/chart";

/** New listings per day, with the days we did not collect drawn as such.
 *
 *  The whole point of the chart is the difference between "the market was
 *  quiet" and "we were not watching". Both are zero new listings, and drawn
 *  the same way the chart reports a dead market during an outage — so a quiet
 *  day is an outlined stub sitting on the axis and an uncollected day is a
 *  hatched column across the full height.
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
 */

const W = 720;
const H = 180;
const PAD = { top: 12, right: 12, bottom: 30, left: 44 };
const PLOT = { left: PAD.left, right: W - PAD.right, top: PAD.top, bottom: H - PAD.bottom };

type DayState = "collected" | "failed" | "unknown";

function stateOf(day: SupplyDay): DayState {
  if (day.collected) return "collected";
  return day.runs_failed > 0 ? "failed" : "unknown";
}

const LEGEND: Record<DayState, string> = {
  collected: "采集正常",
  failed: "采集失败",
  unknown: "无采集记录",
};

export default function DailyBars({ days }: { days: SupplyDay[] }) {
  if (days.length === 0) return null;

  const rects = bucketRects(
    days.map((day, i) => ({ lo: i, hi: i + 1, count: day.new_count })),
    PLOT,
  );
  const peak = Math.max(...days.map((d) => d.new_count));
  const total = days.reduce((sum, d) => sum + d.new_count, 0);
  const failed = days.filter((d) => stateOf(d) === "failed").length;
  const unknown = days.filter((d) => stateOf(d) === "unknown").length;
  const ticks = new Set(dayTicks(days.length));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`${days.length} 天内共新增 ${total} 件商品，最多一天 ${peak} 件；其中 ${unknown} 天没有采集记录、${failed} 天采集失败，详细数值见下方表格`}
          // 30 columns need the room; under this the days merge into a
          // smear. The wrapper scrolls, not the page.
          style={{ display: "block", minWidth: 560 }}
        >
          <defs>
            <pattern
              id="sfd-hatch-unknown"
              width="6"
              height="6"
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <line x1="0" y1="0" x2="0" y2="6" stroke="var(--border-strong)" strokeWidth="1" />
            </pattern>
            <pattern
              id="sfd-hatch-failed"
              width="4"
              height="4"
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <line x1="0" y1="0" x2="0" y2="4" stroke="var(--danger)" strokeWidth="1.5" />
            </pattern>
          </defs>

          {days.map((day, i) => {
            const rect = rects[i];
            const state = stateOf(day);
            if (!rect || state === "collected") return null;
            return (
              <rect
                key={day.date}
                x={rect.x}
                y={PLOT.top}
                width={rect.width}
                height={PLOT.bottom - PLOT.top}
                fill={`url(#sfd-hatch-${state})`}
                opacity={state === "failed" ? 0.6 : 0.45}
              />
            );
          })}

          <line
            x1={PLOT.left}
            y1={PLOT.bottom}
            x2={PLOT.right}
            y2={PLOT.bottom}
            stroke="var(--border)"
          />
          {[...new Set([peak, 0])].map((count) => (
            <text
              key={count}
              x={PLOT.left - 8}
              y={scale(count, 0, peak, PLOT.bottom, PLOT.top) + 4}
              textAnchor="end"
              fontSize="11"
              fill="var(--text-muted)"
              fontFamily="var(--font-mono)"
            >
              {count}
            </text>
          ))}

          {days.map((day, i) => {
            const rect = rects[i];
            if (!rect) return null;
            const x = rect.x + 1.5;
            const width = Math.max(rect.width - 3, 1);
            // A collected day with nothing new gets a visible outlined stub:
            // no bar at all would be indistinguishable from the hatched
            // columns behind it, which is exactly the confusion this chart is
            // built to remove.
            if (day.new_count === 0) {
              return day.collected ? (
                <rect
                  key={day.date}
                  x={x}
                  y={PLOT.bottom - 4}
                  width={width}
                  height={4}
                  fill="none"
                  stroke="var(--border-strong)"
                />
              ) : null;
            }
            return (
              <rect
                key={day.date}
                x={x}
                y={rect.y}
                width={width}
                height={rect.height}
                fill="var(--primary)"
              />
            );
          })}

          {days.map((day, i) =>
            ticks.has(i) && rects[i] ? (
              <text
                key={day.date}
                x={rects[i]!.x + rects[i]!.width / 2}
                y={H - 10}
                textAnchor="middle"
                fontSize="11"
                fill="var(--text-muted)"
                fontFamily="var(--font-mono)"
              >
                {day.date.slice(5)}
              </text>
            ) : null,
          )}
        </svg>
      </div>

      <ul
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-4)",
          listStyle: "none",
          margin: 0,
          padding: 0,
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <li style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span
            style={{ width: 12, height: 12, background: "var(--primary)", display: "inline-block" }}
          />
          有新增
        </li>
        <li style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span
            style={{
              width: 12,
              height: 5,
              border: "1px solid var(--border-strong)",
              display: "inline-block",
            }}
          />
          采集正常，零新增
        </li>
        <li style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span
            style={{
              width: 12,
              height: 12,
              display: "inline-block",
              backgroundImage:
                "repeating-linear-gradient(45deg, var(--border-strong) 0 1px, transparent 1px 6px)",
              border: "1px solid var(--border)",
            }}
          />
          无采集记录（斜纹）
        </li>
        <li style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span
            style={{
              width: 12,
              height: 12,
              display: "inline-block",
              backgroundImage:
                "repeating-linear-gradient(45deg, var(--danger) 0 1.5px, transparent 1.5px 4px)",
              border: "1px solid var(--border)",
            }}
          />
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
            每天首次见到的商品数。日界按 UTC 切分，不是本地时区。图上是同一份数据。
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
            {[...days].reverse().map((day) => {
              const state = stateOf(day);
              return (
                <tr key={day.date}>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {day.date}
                  </td>
                  <td className="num">{day.new_count}</td>
                  <td>
                    {state !== "failed" ? (
                      // --warn is reserved for risk control and a degraded
                      // collector (styling-guidelines.md). Thirty rows of
                      // "no record" is history, not an alarm, and spending
                      // yellow on it drowns the case that matters.
                      <span className="muted">{LEGEND[state]}</span>
                    ) : (
                      <span className="pill" data-tone="danger">
                        {LEGEND.failed}
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
