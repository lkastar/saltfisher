import { describe, expect, it } from "vitest";

import { bucketRects, dayTicks, scale, stepPath, type Pt } from "./chart";

describe("stepPath", () => {
  it("moves horizontally at the old value, then vertically", () => {
    // The price was 100 until the moment it became 80. A diagonal here would
    // claim it passed through 90, which never happened.
    const path = stepPath([
      { x: 0, y: 100 },
      { x: 10, y: 80 },
    ]);
    expect(path).toBe("M 0 100 L 10 100 L 10 80");
  });

  it("keeps every corner a right angle across many points", () => {
    const points: Pt[] = [
      { x: 0, y: 50 },
      { x: 5, y: 50 },
      { x: 9, y: 20 },
      { x: 14, y: 30 },
    ];
    const commands = stepPath(points)
      .split("L ")
      .slice(1)
      .map((c) => c.trim().split(" ").map(Number) as [number, number]);

    // Each pair of commands shares first an x, then a y, with its neighbour:
    // no segment ever changes both at once.
    let [cx, cy] = [points[0]!.x, points[0]!.y];
    for (const [nx, ny] of commands) {
      expect(nx === cx || ny === cy).toBe(true);
      [cx, cy] = [nx, ny];
    }
  });

  it("draws nothing for no data and a bare move for one point", () => {
    expect(stepPath([])).toBe("");
    expect(stepPath([{ x: 3, y: 7 }])).toBe("M 3 7");
  });
});

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
