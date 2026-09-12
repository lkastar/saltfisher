import { describe, expect, it } from "vitest";

import type { WatchEntry } from "../api/queries";

import { recentWatch } from "./watchlist";

/** Only the two fields `recentWatch` reads. The rest of WatchlistPublic is
 *  irrelevant to ordering, and spelling it out would make the test a schema
 *  mirror that breaks on every unrelated field addition.
 */
function entry(item_id: string, added_at: string): WatchEntry {
  return { item_id, added_at } as unknown as WatchEntry;
}

const ENTRIES = [
  entry("b", "2026-09-10T08:00:00"),
  entry("d", "2026-09-12T09:30:00"),
  entry("a", "2026-09-01T00:00:00"),
  entry("c", "2026-09-11T23:59:59"),
];

describe("recentWatch", () => {
  it("orders newest first", () => {
    expect(recentWatch(ENTRIES, 10).map((e) => e.item_id)).toEqual(["d", "c", "b", "a"]);
  });

  it("keeps the newest `limit`, not the first `limit` of the input", () => {
    expect(recentWatch(ENTRIES, 2).map((e) => e.item_id)).toEqual(["d", "c"]);
  });

  it("does not mutate the input", () => {
    const before = ENTRIES.map((e) => e.item_id);
    recentWatch(ENTRIES, 2);
    expect(ENTRIES.map((e) => e.item_id)).toEqual(before);
  });

  it("handles an empty watchlist", () => {
    expect(recentWatch([], 5)).toEqual([]);
  });

  it("treats a bare timestamp as UTC, not local time", () => {
    // Two entries one minute apart across the UTC day boundary. Parsed as
    // local time in a UTC+8 zone these land on different calendar days, but
    // their ORDER must be the same either way -- this guards the comparison,
    // not the formatting.
    const pair = [entry("late", "2026-09-11T23:59:00"), entry("later", "2026-09-12T00:00:00")];
    expect(recentWatch(pair, 2).map((e) => e.item_id)).toEqual(["later", "late"]);
  });
});
