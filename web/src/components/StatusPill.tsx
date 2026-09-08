import { Icon } from "./Icon";

/** Listing status as a pill. The word always carries the meaning; icon and
 *  tone only accelerate it. Sold/removed render as the prototype's neutral
 *  pill: they are terminal facts about the listing, not failures of ours --
 *  red stays reserved for errors and price rises.
 */
export function StatusPill({ status }: { status: string }) {
  if (status === "on_sale") {
    return (
      <span className="pill" data-tone="success">
        <Icon name="check" size={11} />
        在售
      </span>
    );
  }
  if (status === "sold") {
    return (
      <span className="pill">
        <Icon name="check-check" size={11} />
        已售
      </span>
    );
  }
  return (
    <span className="pill">
      <Icon name="x" size={11} />
      已下架
    </span>
  );
}
