import { describe, expect, it } from "vitest";

import type { Monitor, MonitorTrendDay } from "../api/queries";

import {
  defaultMonitorId,
  isPlotted,
  meanSegments,
  trimToObserved,
} from "./monitorTrend";

function rule(id: number, hit_count: number, enabled = true): Monitor {
  return { id, hit_count, enabled } as unknown as Monitor;
}

function day(
  date: string,
  mean_cents: number | null,
  collected = true,
): MonitorTrendDay {
  return {
    date,
    mean_cents,
    p25_cents: null,
    p75_cents: null,
    listing_count: 0,
    collected,
  };
}

describe("defaultMonitorId", () => {
  it("prefers the enabled rule with the most hits", () => {
    expect(defaultMonitorId([rule(1, 5), rule(2, 40), rule(3, 12)])).toBe(2);
  });

  it("ignores a disabled rule even when it has the most hits", () => {
    // A stopped rule's chart ends the day it stopped. Defaulting to it opens
    // the dashboard on stale history and hides the rule actually running.
    expect(defaultMonitorId([rule(1, 500, false), rule(2, 3)])).toBe(2);
  });

  it("falls back to the most-hit rule when every rule is stopped", () => {
    expect(defaultMonitorId([rule(1, 9, false), rule(2, 2, false)])).toBe(1);
  });

  it("returns null with no rules at all", () => {
    expect(defaultMonitorId([])).toBeNull();
  });
});

describe("meanSegments", () => {
  it("splits the series at days with no mean", () => {
    const days = [
      day("2026-09-01", 100),
      day("2026-09-02", 110),
      day("2026-09-03", null),
      day("2026-09-04", 120),
    ];
    expect(meanSegments(days)).toEqual([[0, 1], [3]]);
  });

  it("keeps an unbroken series as one segment", () => {
    const days = [day("2026-09-01", 100), day("2026-09-02", 110)];
    expect(meanSegments(days)).toEqual([[0, 1]]);
  });

  it("drops leading and trailing gaps instead of emitting empty runs", () => {
    // An empty run would render as a zero-length path -- invisible, but it
    // also means `run[0]` is undefined and the React key collapses.
    const days = [
      day("2026-09-01", null),
      day("2026-09-02", 110),
      day("2026-09-03", null),
    ];
    expect(meanSegments(days)).toEqual([[1]]);
  });

  it("returns nothing for a window with no observations", () => {
    expect(
      meanSegments([day("2026-09-01", null), day("2026-09-02", null)]),
    ).toEqual([]);
  });
});

describe("trimToObserved", () => {
  const d = (date: string, mean: number | null) => day(date, mean);

  it("crops the empty margin before the first observation", () => {
    // The case this exists for: a 30-day window over a rule that has been
    // collecting for three days.
    const days = [
      d("09-01", null),
      d("09-02", null),
      d("09-03", 100),
      d("09-04", 110),
    ];
    expect(trimToObserved(days).map((x) => x.date)).toEqual(["09-03", "09-04"]);
  });

  it("crops a trailing margin too", () => {
    const days = [d("09-01", 100), d("09-02", null)];
    expect(trimToObserved(days).map((x) => x.date)).toEqual(["09-01"]);
  });

  it("keeps inner gaps, which are real holes in the record", () => {
    const days = [d("09-01", 100), d("09-02", null), d("09-03", 120)];
    expect(trimToObserved(days).map((x) => x.date)).toEqual([
      "09-01",
      "09-02",
      "09-03",
    ]);
  });

  it("returns nothing when the window has no observation at all", () => {
    expect(trimToObserved([d("09-01", null), d("09-02", null)])).toEqual([]);
  });
});

describe("isPlotted", () => {
  it("accepts a day that was measured", () => {
    expect(isPlotted(day("09-01", 100))).toBe(true);
  });

  it("rejects a day with no price", () => {
    expect(isPlotted(day("09-01", null))).toBe(false);
  });

  it("rejects a carried price on an uncollected day", () => {
    // The case that shipped broken: the aggregate carries the last observed
    // price across an outage, so `mean_cents` is NOT null on a day the rule
    // never ran. Plotting it draws a measurement nobody took.
    expect(isPlotted(day("09-01", 100, false))).toBe(false);
  });
});

describe("meanSegments breaks on outages, not just on missing prices", () => {
  it("splits at an uncollected day even though it carries a price", () => {
    const days = [
      day("09-01", 100),
      day("09-02", 100, false),
      day("09-03", 120),
    ];
    expect(meanSegments(days)).toEqual([[0], [2]]);
  });

  it("keeps a run whose days were all collected", () => {
    const days = [day("09-01", 100), day("09-02", 110), day("09-03", 120)];
    expect(meanSegments(days)).toEqual([[0, 1, 2]]);
  });
});
