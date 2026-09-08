/** Chart geometry, kept pure (no JSX, no DOM) so the semantics carry unit
 *  tests and the charts cannot disagree about where things land.
 *
 *  The price trend renders as a Catmull-Rom smoothed line (decision hycai
 *  2026-09-08, superseding the step-line rule). The smoothing interpolates,
 *  so every chart keeps a visible data table as the exact-values source, and
 *  the band drawn behind the line never leaves the observed values.
 */

export type Pt = { x: number; y: number };

/** Smooth paths emit four coordinate pairs per segment; full doubles bloat
 *  the DOM for sub-pixel precision nobody can see.
 */
function round2(v: number): number {
  return Math.round(v * 100) / 100;
}

/** Catmull-Rom through every point, emitted as cubic Béziers.
 *
 *  Ported from the SIGNAL DECK prototype's `smoothPath` (decision hycai
 *  2026-09-08: price history uses the smoothed trend look, superseding the
 *  step-line rule). The curve passes EXACTLY through each observation -- the
 *  tangents (neighbour deltas / 6) only shape the segment between them -- and
 *  the visible data table below every chart remains the exact-values source.
 */
export function smoothPath(points: readonly Pt[]): string {
  const first = points[0];
  if (!first) return "";

  const parts = [`M ${round2(first.x)} ${round2(first.y)}`];
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i - 1] ?? points[i]!;
    const p1 = points[i]!;
    const p2 = points[i + 1]!;
    const p3 = points[i + 2] ?? p2;
    parts.push(
      `C ${round2(p1.x + (p2.x - p0.x) / 6)} ${round2(p1.y + (p2.y - p0.y) / 6)}, ` +
        `${round2(p2.x - (p3.x - p1.x) / 6)} ${round2(p2.y - (p3.y - p1.y) / 6)}, ` +
        `${round2(p2.x)} ${round2(p2.y)}`,
    );
  }
  return parts.join(" ");
}

/** Rolling min/max envelope around a series -- the prototype trend's band.
 *
 *  At each index the window is the `radius` neighbours on either side,
 *  clipped at the ends. The band is honest where the smoothing is not: the
 *  curve interpolates, the band never leaves the observed values.
 */
export function rollingBand(
  values: readonly number[],
  radius = 2,
): { upper: number[]; lower: number[] } {
  const upper: number[] = [];
  const lower: number[] = [];
  for (let i = 0; i < values.length; i += 1) {
    const window = values.slice(Math.max(0, i - radius), Math.min(values.length - 1, i + radius) + 1);
    upper.push(Math.max(...window));
    lower.push(Math.min(...window));
  }
  return { upper, lower };
}

/** A closed area between two smoothed edges: along `upper`, back along
 *  `lower` reversed. Both arrays are in x order; this function does the
 *  reversing so no caller draws the band inside-out.
 */
export function bandPath(upper: readonly Pt[], lower: readonly Pt[]): string {
  if (upper.length === 0 || lower.length === 0) return "";
  const back = [...lower].reverse();
  return `${smoothPath(upper)} ${smoothPath(back).replace(/^M/, "L")} Z`;
}

/** Per-point collection state on a trend chart. `idle` = we were not
 *  collecting, `fail` = we tried and failed. Two different facts, two
 *  different hatches -- same distinction DailyBars draws per day.
 */
export type TrendState = "ok" | "idle" | "fail";

/** Full-height columns marking the idle/fail points of an index-spaced trend
 *  (the prototype hatches them). Points sit at `scale(i, 0, n-1, ...)`, the
 *  column is `width` px centred on the point; a lone point centres in the box.
 */
export function stateColumns(
  states: readonly TrendState[],
  box: Box,
  width = 10,
): (Rect & { state: "idle" | "fail" })[] {
  const columns: (Rect & { state: "idle" | "fail" })[] = [];
  states.forEach((state, i) => {
    if (state === "ok") return;
    const centre = scale(i, 0, states.length - 1, box.left, box.right);
    columns.push({
      x: centre - width / 2,
      y: box.top,
      width,
      height: box.bottom - box.top,
      state,
    });
  });
  return columns;
}

/** Sparkline geometry for a KPI card: smoothed line plus the area under it,
 *  closed to the bottom edge. Ported from the prototype's `SFD.spark`.
 *
 *  A flat series still draws (span falls back to 1 instead of dividing by
 *  zero), a single value centres as a bare point with no area, and an empty
 *  series draws nothing.
 */
export function sparkPaths(
  values: readonly number[],
  width: number,
  height: number,
): { line: string; area: string; end: Pt | null } {
  const count = values.length;
  if (count === 0) return { line: "", area: "", end: null };

  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const points = values.map((v, i) => ({
    x: count === 1 ? width / 2 : 2 + ((width - 4) * i) / (count - 1),
    y: 2 + (height - 5) * (1 - (v - min) / span),
  }));
  const line = smoothPath(points);
  const end = points[count - 1]!;
  if (count === 1) return { line, area: "", end };

  const firstX = round2(points[0]!.x);
  return { line, area: `${line} L ${round2(end.x)} ${height} L ${firstX} ${height} Z`, end };
}

/** An SVG path that holds each value until the next observation.
 *
 *  Right angles only: travel horizontally at the OLD y to the new x, then
 *  vertically to the new y. That is `step: 'end'` -- the price was the old
 *  price right up to the moment it changed.
 */
export function stepPath(points: Pt[]): string {
  const first = points[0];
  if (!first) return "";

  const parts = [`M ${first.x} ${first.y}`];
  for (let i = 1; i < points.length; i += 1) {
    const point = points[i]!;
    const previous = points[i - 1]!;
    parts.push(`L ${point.x} ${previous.y}`, `L ${point.x} ${point.y}`);
  }
  return parts.join(" ");
}

/** Map a value onto a pixel range, tolerating a zero-width domain (one
 *  observation, or several at the same price) by centring instead of dividing
 *  by zero.
 */
export function scale(value: number, lo: number, hi: number, from: number, to: number): number {
  if (hi === lo) return (from + to) / 2;
  return from + ((value - lo) / (hi - lo)) * (to - from);
}

export type Bucket = { lo: number; hi: number; count: number };
export type Box = { left: number; right: number; top: number; bottom: number };
export type Rect = { x: number; y: number; width: number; height: number };

/** Contiguous buckets to bars on a linear value axis.
 *
 *  One function for both bar charts on purpose: the price histogram's domain
 *  is cents and the supply trend's is the day index, but both are rectangles
 *  standing on a baseline with a shared `scale()`. A second copy is how two
 *  charts start disagreeing about where a bar ends.
 *
 *  `x` and `width` come back for EVERY bucket, including empty ones: a day we
 *  never collected has no bar but still needs its column marked, and the
 *  caller cannot recompute the column without redoing this arithmetic.
 */
export function bucketRects(buckets: readonly Bucket[], box: Box): Rect[] {
  const first = buckets[0];
  const last = buckets[buckets.length - 1];
  if (!first || !last) return [];

  const peak = Math.max(...buckets.map((b) => b.count));
  const plotHeight = box.bottom - box.top;
  return buckets.map((bucket) => {
    const x = scale(bucket.lo, first.lo, last.hi, box.left, box.right);
    // peak 0 means every bucket is empty. scale() would centre a zero-width
    // domain and draw half-height bars for nothing at all.
    const height = peak === 0 ? 0 : scale(bucket.count, 0, peak, 0, plotHeight);
    return {
      x,
      y: box.bottom - height,
      width: scale(bucket.hi, first.lo, last.hi, box.left, box.right) - x,
      height,
    };
  });
}

/** Which indices of a daily series to label.
 *
 *  Both ends always get a label -- an axis whose first and last day are
 *  unnamed does not say what range it covers -- and a generated tick is
 *  dropped rather than allowed to land next to the final one, because two
 *  overlapping dates read as a rendering bug.
 */
export function dayTicks(count: number, max = 6): number[] {
  if (count <= 0) return [];
  if (count <= max) return Array.from({ length: count }, (_, i) => i);

  const step = Math.ceil((count - 1) / (max - 1));
  const ticks: number[] = [];
  for (let i = 0; i + step <= count - 1; i += step) ticks.push(i);
  ticks.push(count - 1);
  return ticks;
}
