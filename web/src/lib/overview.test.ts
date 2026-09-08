import { describe, expect, it } from "vitest";

import { countSince, dailyCounts, daysAgoStart, windowCovers } from "./overview";

/** Fixed local "now": 2026-09-08 15:00 local time. */
const NOW = new Date(2026, 8, 8, 15, 0, 0);

/** ISO string `hoursAgo` hours before NOW, in UTC with a Z (what the API sends). */
function ago(hoursAgo: number): string {
  return new Date(NOW.getTime() - hoursAgo * 3_600_000).toISOString();
}

describe("daysAgoStart", () => {
  it("days=1 is local midnight today", () => {
    const start = daysAgoStart(1, NOW);
    expect([start.getFullYear(), start.getMonth(), start.getDate()]).toEqual([2026, 8, 8]);
    expect([start.getHours(), start.getMinutes()]).toEqual([0, 0]);
  });

  it("crosses month boundaries", () => {
    const start = daysAgoStart(14, NOW); // 13 days back from Sep 8 = Aug 26
    expect([start.getMonth(), start.getDate()]).toEqual([7, 26]);
  });
});

describe("windowCovers", () => {
  const since = new Date(NOW.getTime() - 24 * 3_600_000);

  it("covers when the backend returned fewer rows than its cap", () => {
    expect(windowCovers([ago(1), ago(2)], since, 200)).toBe(true);
    expect(windowCovers([], since, 200)).toBe(true);
  });

  it("covers a full window whose oldest row predates the period", () => {
    expect(windowCovers([ago(1), ago(30)], since, 2)).toBe(true);
  });

  it("does NOT cover a full window that ends inside the period", () => {
    // Cap hit and the oldest row is only 2h old: anything older was cut off,
    // so a 24h count over this window is a lower bound, not a count.
    expect(windowCovers([ago(1), ago(2)], since, 2)).toBe(false);
  });

  it("boundary: oldest exactly at `since` does not prove coverage", () => {
    expect(windowCovers([ago(1), ago(24)], since, 2)).toBe(false);
  });
});

describe("countSince", () => {
  it("counts timestamps at or after the cut", () => {
    const since = new Date(NOW.getTime() - 24 * 3_600_000);
    expect(countSince([ago(1), ago(23), ago(24), ago(25)], since)).toBe(3);
  });

  it("pins zoneless ISO strings to UTC (via parseUtc)", () => {
    // 06:00Z on Sep 8 written without a zone; must not be read as local.
    const zoneless = "2026-09-08T06:00:00";
    const withZone = "2026-09-08T06:00:00Z";
    const since = new Date(Date.parse(withZone) - 1);
    expect(countSince([zoneless], since)).toBe(1);
  });
});

describe("dailyCounts", () => {
  it("buckets by local day, oldest first, today last", () => {
    const counts = dailyCounts(
      [
        ago(1), // today 14:00 local
        ago(16), // yesterday 23:00 local
        ago(20), // yesterday 19:00 local
        ago(6 * 24 + 10), // six days ago
      ],
      7,
      NOW,
    );
    expect(counts).toHaveLength(7);
    expect(counts[6]).toBe(1); // today
    expect(counts[5]).toBe(2); // yesterday
    expect(counts[0]).toBe(1); // oldest slot
    expect(counts.reduce((a, b) => a + b, 0)).toBe(4);
  });

  it("ignores timestamps outside the range", () => {
    const counts = dailyCounts([ago(8 * 24)], 7, NOW);
    expect(counts.every((c) => c === 0)).toBe(true);
  });

  it("empty input yields all-zero series of the right length", () => {
    expect(dailyCounts([], 7, NOW)).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });
});
