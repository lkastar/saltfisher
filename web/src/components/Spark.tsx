import { sparkPaths } from "../lib/chart";

/** KPI-card mini trend line: smoothed line, soft area fill, end dot.
 *
 *  Presentational port of the prototype's `SFD.spark`; the geometry lives in
 *  `lib/chart.ts` where it is unit-tested. Decorative by design -- it carries
 *  no axis and no exact values, so the KPI number next to it is the data and
 *  the spark is `aria-hidden`.
 */

type SparkProps = {
  values: readonly number[];
  /** A colour TOKEN, e.g. "var(--green)". Defaults to the accent. */
  color?: string;
  width?: number;
  height?: number;
};

export default function Spark({ values, color = "var(--acc)", width = 160, height = 26 }: SparkProps) {
  const { line, area, end } = sparkPaths(values, width, height);
  if (end === null) return null;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      aria-hidden="true"
      style={{ display: "block", overflow: "visible" }}
    >
      {area === "" ? null : <path d={area} fill={color} opacity="0.12" stroke="none" />}
      <path d={line} fill="none" stroke={color} strokeWidth="1.4" opacity="0.85" />
      <circle cx={end.x} cy={end.y} r="2.2" fill={color} />
    </svg>
  );
}
