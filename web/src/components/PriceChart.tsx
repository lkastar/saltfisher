import type { PricePoint } from "../api/queries";
import { scale, stepPath } from "../lib/chart";
import { formatDateTime, formatPrice, formatRelativeTime, parseUtc } from "../lib/format";

/** Price history as a step line, plus the same numbers as a table.
 *
 *  A STEP line, never a smooth curve: a snapshot means "this was the price
 *  from here until the next observation". Interpolating between two
 *  observations draws prices that never existed, which for a tool people buy
 *  things from is not a cosmetic problem.
 *
 *  Hand-rolled SVG rather than ECharts. M1 needs exactly one chart type, and
 *  ECharts is roughly four times the size of this entire bundle; drawn by hand
 *  it also reads the CSS tokens directly instead of carrying two theme
 *  objects. The table below is not decoration -- an SVG plot answers "when did
 *  it drop" far worse than a list of dates does, and it is what a screen
 *  reader gets.
 *
 *  ponytail: the old trigger here said "when a third chart type appears".
 *  Two more arrived in M2 (Histogram, DailyBars) and neither needed a library:
 *  both are rectangles on a linear axis sharing this file's `scale()`, about
 *  40 lines each. The accurate trigger is zoom or brushing, or geometry that
 *  is not rectangles-and-lines -- stacked areas, pies, anything needing a
 *  layout pass. Count of chart types is not the cost driver; interaction is.
 */

const W = 720;
const H = 200;
const PAD = { top: 12, right: 12, bottom: 24, left: 64 };

export default function PriceChart({ points }: { points: PricePoint[] }) {
  if (points.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 13 }}>
        还没有价格观测。
      </p>
    );
  }

  const prices = points.map((p) => p.price_cents);
  const times = points.map((p) => parseUtc(p.captured_at).getTime());
  const loPrice = Math.min(...prices);
  const hiPrice = Math.max(...prices);
  const loTime = Math.min(...times);
  const hiTime = Math.max(...times);

  const x = (t: number) => scale(t, loTime, hiTime, PAD.left, W - PAD.right);
  const y = (p: number) => scale(p, loPrice, hiPrice, H - PAD.bottom, PAD.top);

  const pixels = points.map((point, i) => ({
    x: x(times[i]!),
    y: y(point.price_cents),
  }));
  // A single observation has no line; extend it flat so the chart is not an
  // empty box, which reads as "no data" rather than "one unchanged price".
  const path =
    points.length === 1
      ? `${stepPath(pixels)} L ${W - PAD.right} ${y(prices[0]!)}`
      : stepPath(pixels);

  const first = points[0]!;
  const last = points[points.length - 1]!;
  const change = last.price_cents - first.price_cents;

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <header style={{ display: "flex", gap: "var(--space-4)", flexWrap: "wrap", alignItems: "baseline" }}>
        <h2>价格历史</h2>
        <span className="mono" style={{ fontSize: 16, fontWeight: 600 }}>
          {formatPrice(last.price_cents)}
        </span>
        {change !== 0 ? (
          <span
            className="mono"
            style={{
              color: change < 0 ? "var(--success)" : "var(--danger)",
              fontWeight: 600,
              fontSize: 13,
            }}
          >
            {change < 0 ? "▼" : "▲"} {formatPrice(Math.abs(change))}
          </span>
        ) : (
          <span className="muted" style={{ fontSize: 13 }}>
            与首次观测持平
          </span>
        )}
        <span className="muted" style={{ fontSize: 12 }}>
          最后观测 {formatRelativeTime(last.captured_at)} · 共 {points.length} 次
        </span>
      </header>

      <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`价格从 ${formatPrice(first.price_cents)} 变化到 ${formatPrice(last.price_cents)}，共 ${points.length} 次观测，详细数值见下方表格`}
          style={{ display: "block", minWidth: 320 }}
        >
          <line
            x1={PAD.left}
            y1={PAD.top}
            x2={PAD.left}
            y2={H - PAD.bottom}
            stroke="var(--border)"
          />
          <line
            x1={PAD.left}
            y1={H - PAD.bottom}
            x2={W - PAD.right}
            y2={H - PAD.bottom}
            stroke="var(--border)"
          />
          {[hiPrice, loPrice].map((price, i) => (
            <text
              key={i}
              x={PAD.left - 8}
              y={y(price) + 4}
              textAnchor="end"
              fontSize="11"
              fill="var(--text-muted)"
              fontFamily="var(--font-mono)"
            >
              {formatPrice(price)}
            </text>
          ))}
          <text
            x={PAD.left}
            y={H - 8}
            fontSize="11"
            fill="var(--text-muted)"
            fontFamily="var(--font-mono)"
          >
            {formatDateTime(first.captured_at).slice(0, 10)}
          </text>
          <text
            x={W - PAD.right}
            y={H - 8}
            textAnchor="end"
            fontSize="11"
            fill="var(--text-muted)"
            fontFamily="var(--font-mono)"
          >
            {formatDateTime(last.captured_at).slice(0, 10)}
          </text>
          <path
            d={path}
            fill="none"
            stroke="var(--primary)"
            strokeWidth="2"
            strokeLinejoin="miter"
          />
          {points.map((point, i) => (
            <circle
              key={i}
              cx={x(times[i]!)}
              cy={y(point.price_cents)}
              r="3"
              fill="var(--primary)"
            />
          ))}
        </svg>
      </div>

      <div className="table-scroll">
        <table>
          <caption className="muted" style={{ captionSide: "top", textAlign: "left", padding: "var(--space-2)", fontSize: 12 }}>
            每次观测的价格。图上是同一份数据。
          </caption>
          <thead>
            <tr>
              <th>时间</th>
              <th style={{ textAlign: "right" }}>价格</th>
              <th style={{ textAlign: "right" }}>较上次</th>
              <th>状态</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            {points
              .map((point, i) => ({ point, prev: points[i - 1] }))
              .reverse()
              .map(({ point, prev }) => {
                const delta = prev ? point.price_cents - prev.price_cents : null;
                return (
                  <tr key={point.captured_at}>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {formatDateTime(point.captured_at)}
                    </td>
                    <td className="num">{formatPrice(point.price_cents)}</td>
                    <td
                      className="num"
                      style={{
                        color:
                          delta === null || delta === 0
                            ? "var(--text-muted)"
                            : delta < 0
                              ? "var(--success)"
                              : "var(--danger)",
                      }}
                    >
                      {delta === null
                        ? "首次"
                        : delta === 0
                          ? "持平"
                          : `${delta < 0 ? "▼" : "▲"} ${formatPrice(Math.abs(delta))}`}
                    </td>
                    <td className="muted">{point.status}</td>
                    <td className="muted">{point.source}</td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
