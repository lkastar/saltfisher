/** The two seller-rule decisions that must not be wrong: what to call a new
 *  rule, and whether one already exists.
 *
 *  Out of the pages for the same reason `itemFilters.ts` is: there is no jsdom
 *  and no component test in this project (`spec/frontend/quality-guidelines.md`),
 *  so a `lib` function is the only shape a check can reach.
 *
 *  Which kind a rule is stays `keyword === null` — `models.Monitor` has the
 *  xor as a CHECK constraint and says in a comment why a third `kind` column
 *  would be a second truth that can disagree with the first two.
 */

import type { Monitor } from "../api/queries";
import { sellerLabel } from "./itemFilters";

/** Mirrors `MonitorBase.name`'s max_length. A `Seller.nick` has no length
 *  limit of its own, so a long shop name would otherwise come back as a 422
 *  on a name the user never typed.
 */
const NAME_MAX = 100;

/** The default name for a rule watching a seller.
 *
 *  `sellerLabel` already answers "what do we call this seller" — nick first,
 *  truncated id only when there is none (8 sellers in the real db have an
 *  empty nick, and the API returns `""`, not null, for them) — so the opaque
 *  base64 id is never the whole title.
 */
export function sellerRuleName(nick: string | null | undefined, sellerId: string): string {
  return `卖家：${sellerLabel(nick, sellerId)}`.slice(0, NAME_MAX);
}

/** The rule already watching this seller, if any.
 *
 *  `seller_id` is non-null only on seller rules (that is the xor), so matching
 *  on it needs no rule-type test. This is the only thing stopping a second
 *  rule for one seller: the backend constrains keyword-xor-seller, not
 *  one-rule-per-seller.
 */
export function findSellerRule(monitors: Monitor[], sellerId: string): Monitor | undefined {
  return monitors.find((m) => m.seller_id === sellerId);
}
