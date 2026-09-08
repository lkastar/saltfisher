import type { PricePoint } from "../api/queries";
import { bandPath, rollingBand, scale, smoothPath } from "../lib/chart";
import {
  formatChangeRatio,
  formatDateTime,
  formatPrice,
  formatRelativeTime,
  parseUtc,
} from "../lib/format";
import { Icon } from "./Icon";

/** Price history as the prototype's smoothed trend, plus the same numbers as
 *  a table.
 *
 *  Catmull-Rom smoothed line with a rolling min/max band (decision hycai
 *  2026-09-08, superseding the earlier step-line rule). The curve passes
 *  exactly through every observation but interpolates between them, so two
 *  honest layers back it up: the band behind the line never leaves the
 *  observed values, and the table below is the exact-values source.
 *
 *  Hand-rolled SVG rather than a chart library, still: a smooth line and a
 *  band are one path element each, and the geometry lives as pure functions
 *  in `lib/chart.ts` where it carries unit tests. The library trigger is zoom
 *  or brushing, or geometry that is not rectangles-and-lines -- interaction is
 *  the cost driver, not the count of chart types.
 */

const W = 720;
const H = 200;
const PAD = { top: 18, right: 14, bottom: 24, left: 64 };

export default function PriceChart({ points }: { points: PricePoint[] }) {
  if (points.length === 0) {
    return (
      <section className="card">
        <div className="card-h">
          <h2>
            <Icon name="chart-spline" size={15} />
            价格观测历史
          </h2>
        </div>
        <p className="muted" style={{ fontSize: 13 }}>
          还没有价格观测。
        </p>
      </section>
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
  const linePath =
    points.length === 1
      ? `M ${PAD.left} ${y(prices[0]!)} L ${W - PAD.right} ${y(prices[0]!)}`
      : smoothPath(pixels);

  const band = rollingBand(prices);
  const areaPath =
    points.length < 2
      ? null
      : bandPath(
          band.upper.map((v, i) => ({ x: pixels[i]!.x, y: y(v) })),
          band.lower.map((v, i) => ({ x: pixels[i]!.x, y: y(v) })),
        );

  const first = points[0]!;
  const last = points[points.length - 1]!;
  const lastPixel = pixels[pixels.length - 1]!;
  const change = last.price_cents - first.price_cents;

  return (
    <section
      className="card"
      style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}
    >
      <div className="card-h" style={{ marginBottom: 0 }}>
        <h2>
          <Icon name="chart-spline" size={15} />
          价格观测历史
        </h2>
        <span className="note">{points.length} SNAPSHOTS</span>
      </div>

      <div className="mono" style={{ fontSize: 12, color: "var(--text2)" }}>
        当前{" "}
        <span style={{ color: "var(--text)", fontWeight: 600 }}>
          {formatPrice(last.price_cents)}
        </span>
        {change !== 0 ? (
          <>
            {" · "}较首次观测{" "}
            <span style={{ color: change < 0 ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
              {change < 0 ? "▼" : "▲"} {formatPrice(Math.abs(change))} ·{" "}
              {formatChangeRatio(change / first.price_cents)}
            </span>
          </>
        ) : (
          <> · 与首次观测持平</>
        )}
        {" · "}最后观测 {formatRelativeTime(last.captured_at)} · 共 {points.length} 次
      </div>

      <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`价格从 ${formatPrice(first.price_cents)} 变化到 ${formatPrice(last.price_cents)}，共 ${points.length} 次观测，详细数值见下方表格`}
          style={{ display: "block", minWidth: 320, overflow: "visible" }}
        >
          {[...new Set([hiPrice, loPrice])].map((price) => (
            <g key={price}>
              <line
                x1={PAD.left}
                y1={y(price)}
                x2={W - PAD.right}
                y2={y(price)}
                stroke={price === loPrice ? "var(--line2)" : "var(--line)"}
              />
              <text
                x={PAD.left - 8}
                y={y(price) + 4}
                textAnchor="end"
                fontSize="11"
                fill="var(--text3)"
                fontFamily="var(--font-mono)"
              >
                {formatPrice(price)}
              </text>
            </g>
          ))}
          <text
            x={PAD.left}
            y={H - 8}
            fontSize="11"
            fill="var(--text3)"
            fontFamily="var(--font-mono)"
          >
            {formatDateTime(first.captured_at).slice(0, 10)}
          </text>
          <text
            x={W - PAD.right}
            y={H - 8}
            textAnchor="end"
            fontSize="11"
            fill="var(--text3)"
            fontFamily="var(--font-mono)"
          >
            {formatDateTime(last.captured_at).slice(0, 10)}
          </text>
          {areaPath === null ? null : <path d={areaPath} fill="var(--chart-band)" stroke="none" />}
          <path
            d={linePath}
            fill="none"
            stroke="var(--acc)"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
          {pixels.map((pixel, i) => (
            <circle
              key={i}
              cx={pixel.x}
              cy={pixel.y}
              r="3"
              fill="var(--chart-dot-fill)"
              stroke="var(--acc)"
              strokeWidth="1.6"
            />
          ))}
          <circle
            cx={lastPixel.x}
            cy={lastPixel.y}
            r="6.5"
            fill="none"
            stroke="var(--acc)"
            strokeWidth="1"
            opacity="0.5"
          />
        </svg>
      </div>

      <div className="table-scroll">
        <table>
          <caption className="muted" style={{ captionSide: "top", textAlign: "left", padding: "var(--space-2)", fontSize: 12 }}>
            每次观测的价格。曲线经过每个观测点，点与点之间是插值；精确数值以本表为准。
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
                            ? "var(--text3)"
                            : delta < 0
                              ? "var(--green)"
                              : "var(--red)",
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
