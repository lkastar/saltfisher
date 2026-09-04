import { bucketRects, scale, type Bucket } from "../lib/chart";

/** One bar per bucket, one sample per listing.
 *
 *  Rectangles on the same linear `scale()` the step line uses, so there is
 *  still no chart library here. The median is marked because the shape alone
 *  does not answer "what is this thing worth", which is the question the page
 *  exists for.
 *
 *  Unit-agnostic on purpose: asking prices in cents and
 *  left-our-observation-range durations in minutes are the same chart over a
 *  linear axis, so `format` and `label` come in as props rather than a second
 *  copy of this file existing. The neutral `Bucket` from `lib/chart.ts` is the
 *  prop type, and each caller maps its own wire shape onto it.
 *
 *  The table below is the same numbers. An SVG cannot be asked "how many
 *  between ¥2000 and ¥2400", and it is a single unlabelled image to a screen
 *  reader.
 */

const W = 720;
const H = 200;
const PAD = { top: 16, right: 12, bottom: 28, left: 44 };
const PLOT = { left: PAD.left, right: W - PAD.right, top: PAD.top, bottom: H - PAD.bottom };

type HistogramProps = {
  buckets: Bucket[];
  /** Turns an axis value into words. `formatPrice` or `formatDuration`. */
  format: (value: number) => string;
  /** What the axis measures, used in the aria-label and the table header.
   *  A noun, because both places read "<label>分布" and "<label>区间". */
  label: string;
  /** The median line. Explicitly null below two samples, when there is no
   *  median to draw -- not optional, so a caller cannot forget it and TS can
   *  narrow it for the label. */
  median: number | null;
};

export default function Histogram({ buckets, format, label, median }: HistogramProps) {
  const first = buckets[0];
  const last = buckets[buckets.length - 1];
  if (!first || !last) return null;

  const rects = bucketRects(buckets, PLOT);
  const peak = Math.max(...buckets.map((b) => b.count));
  const total = buckets.reduce((sum, b) => sum + b.count, 0);
  const medianX = median === null ? null : scale(median, first.lo, last.hi, PLOT.left, PLOT.right);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          role="img"
          aria-label={`${total} 件商品的${label}分布，从 ${format(first.lo)} 到 ${format(last.hi)}，最高一档 ${peak} 件，详细数值见下方表格`}
          // Below this the bars are too thin to read a shape from, so the
          // wrapper scrolls instead of squeezing the chart -- the page body
          // still never scrolls sideways.
          style={{ display: "block", minWidth: 480 }}
        >
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
          {rects.map((rect, i) => (
            <rect
              key={i}
              x={rect.x + 0.5}
              y={rect.y}
              width={Math.max(rect.width - 1, 1)}
              height={rect.height}
              fill="var(--primary)"
            />
          ))}
          {medianX === null || median === null ? null : (
            <>
              <line
                x1={medianX}
                y1={PLOT.top - 4}
                x2={medianX}
                y2={PLOT.bottom}
                stroke="var(--text)"
                strokeWidth="1"
                strokeDasharray="4 3"
              />
              <text
                x={medianX}
                y={PLOT.top - 7}
                textAnchor="middle"
                fontSize="11"
                fill="var(--text)"
                fontFamily="var(--font-mono)"
              >
                中位 {format(median)}
              </text>
            </>
          )}
          <text
            x={PLOT.left}
            y={H - 9}
            fontSize="11"
            fill="var(--text-muted)"
            fontFamily="var(--font-mono)"
          >
            {format(first.lo)}
          </text>
          <text
            x={PLOT.right}
            y={H - 9}
            textAnchor="end"
            fontSize="11"
            fill="var(--text-muted)"
            fontFamily="var(--font-mono)"
          >
            {format(last.hi)}
          </text>
        </svg>
      </div>

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
            每档的商品件数。图上是同一份数据。
          </caption>
          <thead>
            <tr>
              <th>{label}区间</th>
              <th style={{ textAlign: "right" }}>件数</th>
              <th style={{ textAlign: "right" }}>占比</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((bucket) => (
              <tr key={bucket.lo}>
                <td className="mono" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {format(bucket.lo)} – {format(bucket.hi)}
                </td>
                <td className="num">{bucket.count}</td>
                <td className="num muted">
                  {total === 0 ? "—" : `${((bucket.count / total) * 100).toFixed(1)}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
