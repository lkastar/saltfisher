import type { Monitor, MonitorTrendDay } from "../api/queries";

/** Most hits among the enabled rules, else most hits overall.
 *
 *  Hit count, not recency: the default should be the rule with something to
 *  plot. A rule created a minute ago has the newest id and an empty chart.
 */
export function defaultMonitorId(monitors: readonly Monitor[]): number | null {
  const best = (pool: readonly Monitor[]) =>
    pool.reduce<Monitor | null>(
      (top, m) => (top === null || m.hit_count > top.hit_count ? m : top),
      null,
    );
  const pick = best(monitors.filter((m) => m.enabled)) ?? best(monitors);
  return pick?.id ?? null;
}

/** Is this day a measurement, rather than a carry-forward or a blank?
 *
 *  TWO conditions, and the second is the one that is easy to miss. A day the
 *  rule never successfully ran still comes back with a `mean_cents`: the
 *  aggregate carries the last observed price forward, which is a real fact
 *  ("this is the newest price we know") and worth keeping in the payload and
 *  the tooltip. It is NOT a measurement of that day, so it must not get a dot
 *  and the line must not run through it -- drawing a smooth curve across an
 *  outage reports a market nobody looked at.
 *
 *  The backend deliberately keeps the carried value instead of nulling it, so
 *  this predicate is where the distinction is enforced.
 */
export type PlottedDay = MonitorTrendDay & { mean_cents: number };

export function isPlotted(day: MonitorTrendDay): day is PlottedDay {
  return (
    day.mean_cents !== null && day.mean_cents !== undefined && day.collected
  );
}

/** Runs of consecutive plotted days, as index ranges.
 *
 *  Each run is drawn as its own path. A day that is not plotted -- no price at
 *  all, or a price we carried across an outage -- breaks the line rather than
 *  being smoothed over.
 */
export function meanSegments(days: readonly MonitorTrendDay[]): number[][] {
  const runs: number[][] = [];
  let current: number[] = [];
  days.forEach((day, i) => {
    if (!isPlotted(day)) {
      if (current.length > 0) runs.push(current);
      current = [];
      return;
    }
    current.push(i);
  });
  if (current.length > 0) runs.push(current);
  return runs;
}

/** Drop the leading and trailing days that have no observation at all.
 *
 *  A 30-day window over a rule that started collecting five days ago spends
 *  25 of its width on days that predate the rule's first hit, squeezing every
 *  real point into the last eighth of the chart -- where a smoothed curve
 *  reads as a polyline because each segment is a couple of dozen pixels wide.
 *  Those days also carry no information: the tool was not watching yet, which
 *  is the same reason `analytics.data_days` counts history from the first
 *  sighting rather than from the rule's creation.
 *
 *  INNER gaps are kept. A day with no price between two days that have one is
 *  a real hole in the record and the chart hatches it; trimming those would
 *  be closing a gap rather than cropping an empty margin.
 */
export function trimToObserved(
  days: readonly MonitorTrendDay[],
): MonitorTrendDay[] {
  const seen = (d: MonitorTrendDay) =>
    d.mean_cents !== null && d.mean_cents !== undefined;
  const first = days.findIndex(seen);
  if (first === -1) return [];
  let last = days.length - 1;
  while (!seen(days[last]!)) last -= 1;
  return days.slice(first, last + 1);
}
