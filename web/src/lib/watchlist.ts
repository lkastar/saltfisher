import type { WatchEntry } from "../api/queries";

import { parseUtc } from "./format";

/** The N most recently pinned watchlist entries, newest first.
 *
 *  `parseUtc`, not a raw string compare: `added_at` arrives without an offset,
 *  and while ISO-8601 strings of identical shape do sort lexicographically,
 *  one row serialised with a `Z` or with microseconds would silently sort into
 *  the wrong place. Comparing instants costs nothing here and cannot drift.
 *
 *  Returns a new array — `/api/watchlist` data is TanStack Query cache state
 *  shared with the watchlist page, and sorting it in place would reorder that
 *  page's table as a side effect.
 */
export function recentWatch(entries: readonly WatchEntry[], limit = 5): WatchEntry[] {
  return [...entries]
    .sort((a, b) => parseUtc(b.added_at).getTime() - parseUtc(a.added_at).getTime())
    .slice(0, limit);
}
