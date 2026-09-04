/** Chart geometry, kept pure so the step semantics can be tested.
 *
 *  Separate from the component because this is the part that must not break:
 *  a smoothed or diagonal line between two observations draws a price that
 *  never existed.
 */

export type Pt = { x: number; y: number };

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
