/** The item list's URL <-> filters mapping.
 *
 *  Out of the page because this is the part that must not break, and there is
 *  no jsdom in this project, so a `lib` function is the only thing that can
 *  carry a check.
 *
 *  It earns its keep on `seller_id`: real seller ids are opaque base64 and
 *  contain `+`, `/` and `=` (`+5xYqEw3f9sETQuuxt2Emw==` is one from the
 *  fixtures). Both directions go through URLSearchParams, which escapes and
 *  unescapes them; a hand-built query string would ship `+` as a space and
 *  filter to a seller that does not exist -- an empty list, no error.
 */

import type { ItemFilters } from "../api/queries";

export const SORTS: ReadonlyArray<[NonNullable<ItemFilters["sort"]>, string]> = [
  ["-first_seen", "最新入库"],
  ["first_seen", "最早入库"],
  ["-last_seen", "最近还在"],
  ["price", "价格从低到高"],
  ["-price", "价格从高到低"],
];

export const STATUSES: ReadonlyArray<[NonNullable<ItemFilters["status"]>, string]> = [
  ["on_sale", "在售"],
  ["sold", "已售"],
  ["removed", "已下架"],
];

/** The URL is the only place filter state lives. A reloaded or shared link has
 *  to reproduce the view, and TanStack Query keys off the same object, so
 *  "same URL, same cache" comes out for free.
 */
export function readFilters(params: URLSearchParams): ItemFilters {
  const num = (key: string): number | undefined => {
    const raw = params.get(key);
    if (raw === null || raw === "") return undefined;
    const value = Number(raw);
    return Number.isFinite(value) ? value : undefined;
  };
  const status = params.get("status");
  const sort = params.get("sort");
  return {
    monitor_id: num("monitor_id"),
    // Opaque id: no parsing, no validation. The only wrong answer is an
    // empty string, which would ask the API for "the seller called nothing".
    seller_id: params.get("seller_id") || undefined,
    min_price_cents: num("min_price_cents"),
    max_price_cents: num("max_price_cents"),
    status: STATUSES.some(([v]) => v === status)
      ? (status as ItemFilters["status"])
      : undefined,
    sort: SORTS.some(([v]) => v === sort)
      ? (sort as ItemFilters["sort"])
      : "-first_seen",
    offset: num("offset") ?? 0,
  };
}

/** One filter change -> the next URL.
 *
 *  Any filter change resets the page. Keeping the old offset lands the user on
 *  an empty page, which reads as "no results" rather than "you are on page 3
 *  of a shorter list".
 */
export function nextParams(
  params: URLSearchParams,
  key: string,
  value: string | undefined,
  resetOffset = true,
): URLSearchParams {
  const next = new URLSearchParams(params);
  if (value === undefined || value === "") next.delete(key);
  else next.set(key, value);
  if (resetOffset) next.delete("offset");
  return next;
}

/** What to call the seller the list is pinned to.
 *
 *  The id is base64, not a name, so it is never the headline -- but a nick can
 *  be empty (8 of the 253 sellers in the real db) and a list filtered down to nothing
 *  has no row to read a nick off at all. A truncated id is the honest answer
 *  there: it says "this is who the URL points at" without inventing a name.
 */
export function sellerLabel(nick: string | undefined, sellerId: string): string {
  return nick?.trim() ? nick : `${sellerId.slice(0, 8)}…`;
}
