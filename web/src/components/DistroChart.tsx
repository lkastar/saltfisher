import { kdeGeometry, type Bucket } from "../lib/chart";

/** A distribution as the prototype's `SFD.distro`: KDE density curve with a
 *  soft area fill, a beeswarm rug (one dot per sample), and a dashed median
 *  line -- the shape answers "where do prices sit", the median answers "what
 *  is this thing worth".
 *
 *  Unit-agnostic on purpose: asking prices in cents and
 *  left-our-observation-range durations in minutes are the same chart over a
 *  linear axis, so `format` and `label` come in as props rather than a second
 *  copy of this file existing.
 *
 *  The bucket table below is the exact-numbers source. A smoothed density
 *  cannot be asked "how many between ¥2000 and ¥2400", and it is a single
 *  unlabelled image to a screen reader -- the chart-needs-a-table rule holds.
 */

const W = 720;
const H = 190;
const PAD = { top: 26, right: 12, bottom: 24, left: 12 };
const AXIS_Y = H - PAD.bottom;
const RUG_TOP = AXIS_Y - 30;
/** Where the curve lives; the 30px strip below it belongs to the rug. */
const CURVE_BOX = { left: PAD.left, right: W - PAD.right, top: PAD.top, bottom: RUG_TOP };
const TICKS = 5;

type DistroChartProps = {
  /** Raw sample values, one per listing. The backend may have downsampled
   *  them (cap 500, first and last kept); `sampleSize` stays the true count. */
  samples: number[];
  sampleSize: number;
  /** The exact-numbers table rows -- the same histogram the backend computes. */
  buckets: Bucket[];
  /** Turns an axis value into words. `formatPrice` or `formatDuration`. */
  format: (value: number) => string;
  /** What the axis measures, used in the aria-label and the table header. */
  label: string;
  /** The median line. Explicitly null below two samples, when there is no
   *  median to draw -- not optional, so a caller cannot forget it. */
  median: number | null;
};

export default function DistroChart({
  samples,
  sampleSize,
  buckets,
  format,
  label,
  median,
}: DistroChartProps) {
  const geo = kdeGeometry(samples, CURVE_BOX);
  // `samples` arrives sorted ascending (backend contract, downsampling keeps
  // first and last), so the ends ARE the extremes.
  const min = samples[0];
  const max = samples[samples.length - 1];
  const total = buckets.reduce((sum, b) => sum + b.count, 0);

  // A dense 120-point polyline, not smoothPath: at ~6px per segment the
  // Bézier version is visually identical and three times the path string.
  const pts = geo ? geo.curve.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" L ") : null;
  const first = geo?.curve[0];
  const last = geo?.curve[geo.curve.length - 1];
  const area =
    pts && first && last
      ? `M ${first.x.toFixed(1)} ${RUG_TOP} L ${pts} L ${last.x.toFixed(1)} ${RUG_TOP} Z`
      : null;
  const line = pts ? `M ${pts}` : null;

  const medianX = geo && median !== null ? geo.x(median) : null;
  // The label is clamped so a median near an edge does not push its text out
  // of the viewBox; the line itself stays where the value is.
  const medianLabelX =
    medianX === null ? null : Math.max(CURVE_BOX.left + 40, Math.min(CURVE_BOX.right - 40, medianX));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      {geo && min !== undefined && max !== undefined ? (
        <div className="table-scroll" style={{ padding: "var(--space-2)" }}>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            height={H}
            role="img"
            aria-label={`${sampleSize} 件商品的${label}分布密度曲线，从 ${format(min)} 到 ${format(max)}${median === null ? "" : `，中位数 ${format(median)}`}，精确数值见下方表格`}
            // Below this the curve and the rug merge into a smear; the wrapper
            // scrolls instead of squeezing, and the page body never does.
            style={{ display: "block", minWidth: 480 }}
          >
            {area === null ? null : <path d={area} fill="var(--chart-band)" stroke="none" />}
            {line === null ? null : (
              <path d={line} fill="none" stroke="var(--acc)" strokeWidth="1.6" />
            )}

            {/* Beeswarm rug: one dot per sample, stacking upward where values
                crowd. This is what makes 3 samples and 300 look different even
                when their curves have the same shape. */}
            {geo.rug.map((dot, i) => (
              <circle
                key={i}
                cx={dot.x.toFixed(1)}
                cy={AXIS_Y - 6 - dot.lane * 6}
                r="2.4"
                fill="var(--acc)"
                opacity="0.6"
              />
            ))}

            <line
              x1={CURVE_BOX.left}
              y1={AXIS_Y}
              x2={CURVE_BOX.right}
              y2={AXIS_Y}
              stroke="var(--line2)"
            />

            {medianX !== null && medianLabelX !== null && median !== null ? (
              <>
                <line
                  x1={medianX}
                  y1={CURVE_BOX.top - 2}
                  x2={medianX}
                  y2={AXIS_Y}
                  stroke="var(--text3)"
                  strokeWidth="1"
                  strokeDasharray="5 4"
                />
                <text
                  x={medianLabelX}
                  y={CURVE_BOX.top - 8}
                  textAnchor="middle"
                  fontSize="11"
                  fill="var(--text2)"
                  fontFamily="var(--font-mono)"
                >
                  中位数 {format(median)}
                </text>
              </>
            ) : null}

            {Array.from({ length: TICKS }, (_, t) => {
              const value = min + ((max - min) * t) / (TICKS - 1);
              return (
                <text
                  key={t}
                  x={geo.x(value)}
                  y={H - 8}
                  textAnchor={t === 0 ? "start" : t === TICKS - 1 ? "end" : "middle"}
                  fontSize="11"
                  fill="var(--text3)"
                  fontFamily="var(--font-mono)"
                >
                  {format(value)}
                </text>
              );
            })}
          </svg>
        </div>
      ) : null}

      {/* The cap is the backend's (uniform downsampling over the sorted
          values); saying so keeps the dot count from contradicting the
          headline sample count. */}
      {samples.length < sampleSize ? (
        <p className="dim" style={{ fontSize: 11.5, margin: 0 }}>
          曲线与散点由均匀降采样的 {samples.length} 个样本绘制（保留首尾）；分位数与下表按全部{" "}
          {sampleSize} 件计算。
        </p>
      ) : null}

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
            每档的商品件数。曲线是密度估计（KDE），形状是插值；精确数值以本表为准。
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
