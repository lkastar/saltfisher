import { describe, expect, it } from "vitest";

import { scale, stepPath, type Pt } from "./chart";

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
