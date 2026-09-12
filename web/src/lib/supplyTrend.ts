import type { SupplyDay } from "../api/queries";

/** Index of the first day this keyword was actually being watched.
 *
 *  Days before it are not an outage -- the rule did not exist yet -- and
 *  labelling them 无采集记录 tells someone who created a rule today that the
 *  tool has been failing for two weeks.
 *
 *  Two signals, whichever starts earlier, because neither alone is right:
 *
 *  - The first day with ANY run, succeeded or failed. This is the direct
 *    evidence, and it is the only one that covers a rule which ran but never
 *    matched anything.
 *  - `data_days`, which counts from the first SIGHTING. Databases older than
 *    the run log have hits with no CollectRun rows behind them, and going by
 *    runs alone would call their entire history pre-history.
 *
 *  Neither present means the rule has done nothing yet, so the whole window is
 *  pre-history.
 */
export function firstWatchedIndex(
  days: readonly SupplyDay[],
  dataDays: number,
): number {
  const firstRun = days.findIndex((d) => d.runs_ok + d.runs_failed > 0);
  const fromHits =
    dataDays > 0 ? Math.max(0, days.length - dataDays) : days.length;
  return Math.min(firstRun === -1 ? days.length : firstRun, fromHits);
}
