import { describe, expect, it } from "vitest";

import type { SupplyDay } from "../api/queries";

import { firstWatchedIndex } from "./supplyTrend";

function day(runs_ok = 0, runs_failed = 0): SupplyDay {
  return {
    date: "",
    new_count: 0,
    collected: runs_ok > 0,
    runs_ok,
    runs_failed,
  };
}

describe("firstWatchedIndex", () => {
  it("starts at the first day with a run", () => {
    // The case the user hit: a rule created today, on a 30-day window. The
    // days before it existed are not an outage.
    const days = [day(), day(), day(1), day(1)];
    expect(firstWatchedIndex(days, 1)).toBe(2);
  });

  it("counts a failed-only day as watched", () => {
    // We were looking and it went wrong -- that is an outage worth showing,
    // not pre-history.
    expect(firstWatchedIndex([day(), day(0, 2), day(1)], 0)).toBe(1);
  });

  it("covers a rule that ran but never matched anything", () => {
    // data_days counts from the first SIGHTING, so it is 0 here even though
    // the rule has been running all along.
    expect(firstWatchedIndex([day(1), day(1), day(1)], 0)).toBe(0);
  });

  it("falls back to data_days when the run log predates nothing", () => {
    // Databases older than CollectRun have hits with no runs behind them;
    // going by runs alone would call their whole history pre-history.
    expect(firstWatchedIndex([day(), day(), day(), day()], 2)).toBe(2);
  });

  it("takes whichever signal starts earlier", () => {
    const days = [day(), day(), day(1), day(1)];
    expect(firstWatchedIndex(days, 4)).toBe(0);
  });

  it("treats a rule that has done nothing as all pre-history", () => {
    expect(firstWatchedIndex([day(), day()], 0)).toBe(2);
  });
});
