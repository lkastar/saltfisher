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
