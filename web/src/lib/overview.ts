/** Pure derivations behind the overview KPIs.
 *
 *  There is no global stats endpoint; the KPIs are counted client-side from
 *  list endpoints whose responses the backend CAPS (`/api/items` and
 *  `/api/notify-logs` both stop at 200 rows). A capped window silently
 *  under-counts, so every count carries its own honesty check: `windowCovers`
 *  says whether the window reaches back far enough for a count over a period
 *  to be exact, and the page renders "N+" or drops a sparkline when it is not.
 *
 *  In `lib/`, not the page, for the same reason `itemFilters.ts` is: this
 *  project has no jsdom and no component tests, so a lib function is the only
 *  shape a check can reach.
 */

import { parseUtc } from "./format";

/** True when a newest-first window is complete over [since, now]: either the
 *  backend returned fewer rows than its cap (we hold everything it has), or
 *  the oldest row predates `since` (everything newer is inside the window).
 */
export function windowCovers(
  newestFirstIso: readonly string[],
  since: Date,
  cap: number,
): boolean {
  if (newestFirstIso.length < cap) return true;
  const oldest = newestFirstIso[newestFirstIso.length - 1];
  return oldest !== undefined && parseUtc(oldest).getTime() < since.getTime();
}

/** How many timestamps are at or after `since`. */
export function countSince(iso: readonly string[], since: Date): number {
  const cut = since.getTime();
  return iso.filter((t) => parseUtc(t).getTime() >= cut).length;
}

/** Local midnight `days - 1` days ago: the left edge of a daily series that
 *  ends today. Local, not UTC, because "today" on a dashboard means the
 *  user's day; the backend's UTC timestamps are converted by parseUtc.
 */
export function daysAgoStart(days: number, now = new Date()): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1));
}

/** A local-calendar-day key. Component-based rather than dividing epoch time
 *  by 86_400_000 so a DST day (23h or 25h long) cannot shift a count into the
 *  neighbouring bucket.
 */
function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** Per-local-day counts over the last `days` days, oldest day first (a spark
 *  reads left to right, ending at today). Timestamps outside the range are
 *  ignored; the caller decides with `windowCovers` whether the series is
 *  honest enough to draw.
 */
export function dailyCounts(
  iso: readonly string[],
  days: number,
  now = new Date(),
): number[] {
  const counts = new Array<number>(days).fill(0);
  const slotByDay = new Map<string, number>();
  for (let i = 0; i < days; i += 1) {
    slotByDay.set(
      dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1 - i))),
      i,
    );
  }
  for (const t of iso) {
    const slot = slotByDay.get(dayKey(parseUtc(t)));
    if (slot !== undefined) counts[slot] = (counts[slot] ?? 0) + 1;
  }
  return counts;
}
