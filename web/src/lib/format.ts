/** The only place a price becomes a string, and the only place a backend
 *  timestamp becomes a local one.
 *
 *  Both exist because the alternative is three call sites each rounding
 *  differently, which is how a monitoring tool starts lying about prices.
 */

/** Prices cross the wire as integer cents. Exactly one function divides. */
export function formatPrice(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) {
    return "—";
  }
  const yuan = cents / 100;
  // Whole yuan is the overwhelming case on this marketplace; showing ".00"
  // on every row is noise in a dense table.
  const digits = cents % 100 === 0 ? 0 : 2;
  return `¥${yuan.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Signed percentage for a price change. Negative means cheaper. */
export function formatChangeRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) {
    return "—";
  }
  if (ratio === 0) return "持平";
  // The arrow carries the meaning; color only accelerates reading it. Green
  // means cheaper here, the opposite of the A-share convention, so the glyph
  // and the number must stand on their own.
  const arrow = ratio < 0 ? "▼" : "▲";
  return `${arrow} ${Math.abs(ratio * 100).toFixed(1)}%`;
}

/** Backend timestamps are UTC. A bare ISO string with no offset is parsed as
 *  LOCAL time by Date, which silently shifts every timestamp by the machine's
 *  offset -- so pin it to UTC when the offset is missing.
 */
export function parseUtc(iso: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

const UNITS: ReadonlyArray<readonly [number, string]> = [
  [60, "秒"],
  [60, "分钟"],
  [24, "小时"],
  [Infinity, "天"],
];

export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "从未";
  const then = parseUtc(iso);
  if (Number.isNaN(then.getTime())) return "—";

  let delta = (Date.now() - then.getTime()) / 1000;
  const future = delta < 0;
  delta = Math.abs(delta);

  let unit = "秒";
  for (const [size, name] of UNITS) {
    unit = name;
    if (delta < size) break;
    delta /= size;
  }
  const n = Math.floor(delta);
  if (unit === "秒" && n < 5) return "刚刚";
  return future ? `${n} ${unit}后` : `${n} ${unit}前`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseUtc(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

/** The inverse of formatPrice, for money the user types.
 *
 *  Lives here for the same reason formatPrice does: one place converts between
 *  yuan and cents. Returns null for anything that is not a number, so a caller
 *  can tell "left blank" from "typed 0".
 */
export function parseYuanToCents(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const yuan = Number(trimmed);
  if (!Number.isFinite(yuan) || yuan < 0) return null;
  // Round rather than truncate: 12.345 typed by hand should not silently
  // become 12.34, and floating point makes 1234.5 out of 12.345 * 100.
  return Math.round(yuan * 100);
}
