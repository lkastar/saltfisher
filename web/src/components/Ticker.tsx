import type { Item } from "../api/queries";
import { formatPrice, formatRelativeTime } from "../lib/format";
import { Icon } from "./Icon";

type TickerProps = {
  /** Newest first; the overview feeds it from its recent-items query. */
  items: readonly Item[];
};

/** SIGNAL DECK marquee of the newest hits. Purely presentational -- no
 *  fetching here -- and it renders nothing when there is nothing to announce:
 *  an empty marquee is chrome pretending to be signal, and the 最近命中 card
 *  below carries the real three states.
 *
 *  The cells render twice because the CSS loop translates the track by -50%;
 *  the duplicate is aria-hidden so nothing is read twice. Cells are spans,
 *  not links: a moving focus target is a keyboard trap, and the same items
 *  are one card away as ordinary links.
 */
export function Ticker({ items }: TickerProps) {
  if (items.length === 0) return null;

  const cells = items.map((item) => (
    <span className="tk" key={item.id} title={item.title}>
      <b>{item.title}</b>
      <span className="mono">{formatPrice(item.price_cents)}</span>
      <span className="dim">{formatRelativeTime(item.first_seen_at)}</span>
    </span>
  ));

  return (
    <div className="ticker-wrap">
      <span className="ticker-label">
        <Icon name="radio" size={13} />
        实时命中
      </span>
      <div className="ticker">
        <div className="ticker-track">
          {cells}
          <span style={{ display: "contents" }} aria-hidden="true">
            {cells}
          </span>
        </div>
      </div>
    </div>
  );
}
