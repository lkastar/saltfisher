import { describe, expect, it } from "vitest";

import {
  bandPath,
  bucketRects,
  dayTicks,
  kdeGeometry,
  rollingBand,
  scale,
  smoothPath,
  sparkPaths,
  stateColumns,
  type Pt,
  type TrendState,
} from "./chart";

/** Every point a path visits: the M pair, then the endpoint of each C
 *  segment (its last coordinate pair). Control handles are skipped -- a
 *  Catmull-Rom curve does not pass through those.
 */
function pathEndpoints(path: string): Pt[] {
  const points: Pt[] = [];
  for (const match of path.matchAll(/[MLC]([^MLCZ]+)/g)) {
    const nums = match[1]!.trim().split(/[\s,]+/).map(Number);
    points.push({ x: nums[nums.length - 2]!, y: nums[nums.length - 1]! });
  }
  return points;
}

describe("scale", () => {
  it("maps the domain onto the range", () => {
    expect(scale(0, 0, 10, 0, 100)).toBe(0);
    expect(scale(10, 0, 10, 0, 100)).toBe(100);
    expect(scale(5, 0, 10, 0, 100)).toBe(50);
  });

  it("centres instead of dividing by zero", () => {
    // One observation, or several at an unchanged price.
    expect(scale(42, 42, 42, 0, 200)).toBe(100);
  });

  it("handles an inverted range, which is how svg y axes work", () => {
    expect(scale(0, 0, 10, 200, 0)).toBe(200);
    expect(scale(10, 0, 10, 200, 0)).toBe(0);
  });
});

describe("bucketRects", () => {
  const box = { left: 0, right: 100, top: 0, bottom: 50 };

  it("tiles contiguous buckets with no gap and no overlap", () => {
    const rects = bucketRects(
      [
        { lo: 0, hi: 10, count: 1 },
        { lo: 10, hi: 20, count: 2 },
        { lo: 20, hi: 30, count: 4 },
      ],
      box,
    );
    rects.forEach((rect, i) => {
      expect(rect.x).toBeCloseTo((i * 100) / 3);
      expect(rect.width).toBeCloseTo(100 / 3);
      // The next bar starts exactly where this one ends: a histogram with
      // gaps between adjacent buckets claims prices nothing landed on.
      if (i > 0) expect(rects[i - 1]!.x + rects[i - 1]!.width).toBeCloseTo(rect.x);
    });
  });

  it("scales height by count and stands every bar on the baseline", () => {
    const rects = bucketRects(
      [
        { lo: 0, hi: 1, count: 1 },
        { lo: 1, hi: 2, count: 2 },
      ],
      box,
    );
    expect(rects.map((r) => r.height)).toEqual([25, 50]);
    expect(rects.every((r) => r.y + r.height === box.bottom)).toBe(true);
  });

  it("gives an all-empty series flat bars, not centred ones", () => {
    // scale() centres a zero-width domain, so without the guard a day with
    // nothing collected would draw a half-height bar out of no data at all.
    const rects = bucketRects(
      [
        { lo: 0, hi: 1, count: 0 },
        { lo: 1, hi: 2, count: 0 },
      ],
      box,
    );
    expect(rects.map((r) => r.height)).toEqual([0, 0]);
    // The columns still have width: "we never collected that day" is drawn
    // there, and it needs somewhere to be drawn.
    expect(rects.map((r) => r.width)).toEqual([50, 50]);
  });

  it("draws nothing for no buckets", () => {
    expect(bucketRects([], box)).toEqual([]);
  });
});

describe("smoothPath", () => {
  it("passes exactly through every control point", () => {
    // Catmull-Rom interpolates BETWEEN observations; the observations
    // themselves must land exactly, or the dots drawn on top of the line
    // would sit beside it.
    const points: Pt[] = [
      { x: 0, y: 100 },
      { x: 10, y: 80 },
      { x: 25, y: 95 },
      { x: 40, y: 60 },
    ];
    const visited = pathEndpoints(smoothPath(points));
    expect(visited).toHaveLength(points.length);
    points.forEach((p, i) => {
      expect(visited[i]!.x).toBeCloseTo(p.x, 2);
      expect(visited[i]!.y).toBeCloseTo(p.y, 2);
    });
  });

  it("draws nothing for no data and a bare move for one point", () => {
    expect(smoothPath([])).toBe("");
    expect(smoothPath([{ x: 3, y: 7 }])).toBe("M 3 7");
  });

  it("two points reduce to a straight segment, endpoints exact", () => {
    const visited = pathEndpoints(smoothPath([{ x: 0, y: 10 }, { x: 20, y: 30 }]));
    expect(visited).toEqual([
      { x: 0, y: 10 },
      { x: 20, y: 30 },
    ]);
  });
});

describe("rollingBand", () => {
  const values = [100, 80, 95, 60, 60, 120, 110];

  it("envelops the series at every index", () => {
    const { upper, lower } = rollingBand(values);
    values.forEach((v, i) => {
      expect(lower[i]!).toBeLessThanOrEqual(v);
      expect(upper[i]!).toBeGreaterThanOrEqual(v);
    });
  });

  it("never leaves the observed min/max", () => {
    // The band is the honest layer behind the smoothed line: unlike the
    // curve, it must not contain a price that never existed.
    const { upper, lower } = rollingBand(values);
    const min = Math.min(...values);
    const max = Math.max(...values);
    for (const v of [...upper, ...lower]) {
      expect(v).toBeGreaterThanOrEqual(min);
      expect(v).toBeLessThanOrEqual(max);
    }
  });

  it("clips the window at the ends instead of reading outside", () => {
    const { upper, lower } = rollingBand([1, 2, 3, 4, 5], 2);
    expect(upper[0]).toBe(3); // indices 0..2 only
    expect(lower[4]).toBe(3); // indices 2..4 only
  });

  it("is empty for an empty series", () => {
    expect(rollingBand([])).toEqual({ upper: [], lower: [] });
  });
});

describe("bandPath", () => {
  it("goes out along the upper edge and back along the lower, closed", () => {
    const upper: Pt[] = [
      { x: 0, y: 10 },
      { x: 50, y: 5 },
      { x: 100, y: 12 },
    ];
    const lower: Pt[] = [
      { x: 0, y: 40 },
      { x: 50, y: 45 },
      { x: 100, y: 38 },
    ];
    const path = bandPath(upper, lower);
    expect(path.startsWith("M 0 10")).toBe(true);
    expect(path.endsWith("Z")).toBe(true);
    // The return leg starts at the LAST lower point: the loop must not cross
    // itself, which it would if lower were traced in x order.
    expect(path).toContain("L 100 38");
    const visited = pathEndpoints(path);
    expect(visited).toEqual([...upper, ...[...lower].reverse()]);
  });

  it("draws nothing when either edge is missing", () => {
    expect(bandPath([], [{ x: 0, y: 0 }])).toBe("");
    expect(bandPath([{ x: 0, y: 0 }], [])).toBe("");
  });
});

describe("stateColumns", () => {
  const box = { left: 100, right: 200, top: 10, bottom: 90 };

  it("marks idle and fail points with full-height columns centred on them", () => {
    const states: TrendState[] = ["ok", "idle", "ok", "ok", "fail", "ok"];
    const columns = stateColumns(states, box, 10);
    expect(columns).toEqual([
      // index 1 of 0..5 -> x = 100 + 100 * 1/5 = 120, centred: 115
      { x: 115, y: 10, width: 10, height: 80, state: "idle" },
      // index 4 -> x = 180, centred: 175
      { x: 175, y: 10, width: 10, height: 80, state: "fail" },
    ]);
  });

  it("draws nothing when every point is ok", () => {
    expect(stateColumns(["ok", "ok"], box)).toEqual([]);
  });

  it("centres a lone point instead of dividing by zero", () => {
    const columns = stateColumns(["fail"], box, 10);
    expect(columns[0]!.x).toBe(145); // centre 150, half-width 5
  });
});

describe("sparkPaths", () => {
  it("handles 0, 1 and 2 points without NaN", () => {
    for (const values of [[], [7], [7, 9]]) {
      const { line, area } = sparkPaths(values, 160, 26);
      expect(line).not.toMatch(/NaN/);
      expect(area).not.toMatch(/NaN/);
    }
    expect(sparkPaths([], 160, 26)).toEqual({ line: "", area: "", end: null });
    // A lone value is a centred point with no area under it.
    const lone = sparkPaths([7], 160, 26);
    expect(lone.end?.x).toBe(80);
    expect(lone.area).toBe("");
  });

  it("survives a flat series instead of dividing by zero", () => {
    const { line } = sparkPaths([5, 5, 5], 160, 26);
    expect(line).not.toMatch(/NaN/);
    // Flat means one y for every point.
    const ys = new Set(pathEndpoints(line).map((p) => p.y));
    expect(ys.size).toBe(1);
  });

  it("closes the area to the bottom edge under the line's endpoints", () => {
    const { line, area, end } = sparkPaths([3, 8, 5], 160, 26);
    expect(area.startsWith(line)).toBe(true);
    expect(area).toContain(`L ${end!.x} 26`);
    expect(area.endsWith("L 2 26 Z")).toBe(true);
  });
});

describe("kdeGeometry", () => {
  const box = { left: 10, right: 210, top: 20, bottom: 120 };

  it("is null for no samples", () => {
    expect(kdeGeometry([], box)).toBeNull();
  });

  it("spans the box and touches the top exactly at its peak", () => {
    const geo = kdeGeometry([100, 120, 125, 130, 200], box)!;
    // The curve covers the whole box width...
    expect(geo.curve[0]!.x).toBeCloseTo(box.left, 2);
    expect(geo.curve[geo.curve.length - 1]!.x).toBeCloseTo(box.right, 2);
    // ...stays inside it vertically, and its peak-normalisation puts the
    // densest point on box.top -- otherwise every distribution would render
    // at a different height and the card would look broken on sparse data.
    const ys = geo.curve.map((p) => p.y);
    expect(Math.min(...ys)).toBeCloseTo(box.top, 6);
    for (const y of ys) {
      expect(y).toBeGreaterThanOrEqual(box.top - 1e-6);
      expect(y).toBeLessThanOrEqual(box.bottom + 1e-6);
    }
    expect(geo.curve.some((p) => Number.isNaN(p.x) || Number.isNaN(p.y))).toBe(false);
  });

  it("tails toward the baseline at both domain edges", () => {
    // The domain extends two bandwidths past min/max so the curve dies down
    // inside the box instead of being chopped mid-slope.
    const geo = kdeGeometry([50, 55, 60], box)!;
    const height = box.bottom - box.top;
    expect(box.bottom - geo.curve[0]!.y).toBeLessThan(height * 0.25);
    expect(box.bottom - geo.curve[geo.curve.length - 1]!.y).toBeLessThan(height * 0.25);
  });

  it("keeps rug dots inset from the edges and in value order", () => {
    const geo = kdeGeometry([50, 100, 150], box)!;
    const xs = geo.rug.map((r) => r.x);
    // min/max sit two bandwidths inside the box, never on its edge.
    expect(xs[0]!).toBeGreaterThan(box.left);
    expect(xs[xs.length - 1]!).toBeLessThan(box.right);
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
    // The median line must land on the same mapping the rug used.
    expect(geo.x(100)).toBeCloseTo(xs[1]!, 6);
  });

  it("stacks overlapping samples into lanes instead of hiding them", () => {
    const geo = kdeGeometry([100, 100, 100], box)!;
    expect(geo.rug.map((r) => r.lane)).toEqual([0, 1, 2]);
    // All three are the same value, so the same x.
    expect(new Set(geo.rug.map((r) => r.x)).size).toBe(1);
  });

  it("survives one sample and an all-equal set without NaN", () => {
    for (const samples of [[42], [7, 7, 7, 7]]) {
      const geo = kdeGeometry(samples, box)!;
      expect(geo.curve.some((p) => Number.isNaN(p.x) || Number.isNaN(p.y))).toBe(false);
      expect(Math.min(...geo.curve.map((p) => p.y))).toBeCloseTo(box.top, 6);
    }
  });
});

describe("dayTicks", () => {
  it("labels every day when they all fit", () => {
    expect(dayTicks(4, 6)).toEqual([0, 1, 2, 3]);
  });

  it("always labels both ends", () => {
    for (const count of [7, 8, 9, 30, 31, 90]) {
      const ticks = dayTicks(count, 6);
      expect(ticks[0]).toBe(0);
      expect(ticks[ticks.length - 1]).toBe(count - 1);
      expect(ticks.length).toBeLessThanOrEqual(6);
    }
  });

  it("never puts a tick right next to the last one", () => {
    // count 8 with step 2 would otherwise emit 6 and then 7, and two dates
    // one pixel apart read as a rendering bug.
    for (const count of [7, 8, 9, 13, 30, 31, 90]) {
      const ticks = dayTicks(count, 6);
      const gaps = ticks.slice(1).map((t, i) => t - ticks[i]!);
      expect(Math.min(...gaps)).toBeGreaterThan(1);
    }
  });

  it("has nothing to label with no days", () => {
    expect(dayTicks(0)).toEqual([]);
  });
});
