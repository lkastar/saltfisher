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

/** A monitor's price window in one string. Null on both ends is "不限" --
 *  an unbounded rule filters nothing, and printing "— – —" would read as
 *  missing data rather than a deliberate open window.
 */
export function formatPriceRange(
  lo: number | null | undefined,
  hi: number | null | undefined,
): string {
  if (lo == null && hi == null) return "不限";
  if (lo == null) return `≤ ${formatPrice(hi)}`;
  if (hi == null) return `≥ ${formatPrice(lo)}`;
  return `${formatPrice(lo)} – ${formatPrice(hi)}`;
}

/** Durations cross the wire as integer minutes, for the same reason prices
 *  cross as integer cents. Exactly one function turns them into words.
 *
 *  Hours are the reading unit — a listing lasts hours or days here, and
 *  "3720 分钟" is a number nobody converts in their head. Below an hour stays
 *  in minutes rather than rounding to "0 小时": at a 300s poll interval a
 *  listing seen in one cycle and gone by the next is an ordinary sample, and
 *  it is the shortest one the tool can measure.
 */
export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) {
    return "—";
  }
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  // Whole hours first, so the day split cannot round its way to "1 天 24 小时".
  const hours = Math.floor(minutes / 60);
  // Compare on the value that will actually be PRINTED, not on the floor of
  // it: 2879 minutes floors to 47 hours but prints as (2879/60).toFixed(1) =
  // "48.0 小时", which is the same boundary this branch exists to stay below
  // and reads as more than the "2 天" that 2880 gets.
  const tenths = Math.round(minutes / 6) / 10;
  if (tenths < 48) {
    const exact = minutes % 60 === 0;
    return `${exact ? hours : tenths.toFixed(1)} 小时`;
  }
  const rest = hours % 24;
  const days = (hours - rest) / 24;
  return rest === 0 ? `${days} 天` : `${days} 天 ${rest} 小时`;
}

/** How many samples are still timed by the old global clock.
 *
 *  `MonitorHit.last_hit_at` cannot be backfilled, so listings recorded before
 *  it existed end their clock at `Item.last_seen_at` — which another keyword's
 *  rule keeps winding. Those rows under-report, and a mixed sample must say so
 *  rather than pass itself off as one clean measurement. Returns null once
 *  none are left, which happens on its own as new cycles stamp the column.
 */
export function legacyClockNote(legacy: number, total: number): string | null {
  if (legacy <= 0 || total <= 0) return null;
  if (legacy >= total) {
    return `这 ${total} 个样本全部还在旧的全局时钟上：它们的「消失时刻」取自商品的全局最后一次观测，而不是这个关键词自己最后一次看到它。被多个关键词共享的商品会因此偏短。新采集的周期会自己修正，这个数会降下去。`;
  }
  return `${total} 个样本里有 ${legacy} 个还在旧的全局时钟上（早于按关键词记录最后观测时刻的那次改动），它们偏短。剩下 ${total - legacy} 个是按本关键词计的。`;
}

/** The observation aperture behind a duration distribution, in words.
 *
 *  Lives here, with a test, rather than as a ternary inside the page: getting
 *  the condition backwards would hide exactly the caveat the field exists to
 *  surface, and it would hide it silently.
 *
 *  `caveat` is non-null when the distribution is not safe to read as a market
 *  measurement — the aperture changed inside the window, or we cannot say
 *  what it was. A wider aperture makes a listing "disappear" later, so those
 *  numbers are partly a measurement of how many pages we happened to read.
 */
export function apertureNote(
  pagesMin: number | null | undefined,
  pagesMax: number | null | undefined,
  rows: number,
): { aperture: string; caveat: string | null } {
  const lo = pagesMin ?? null;
  const hi = pagesMax ?? null;
  if (lo === null || hi === null) {
    return {
      aperture:
        "商品从我们的搜索结果里消失，不等于它被买走了——也可能是下架，也可能只是排名掉出了我们读的那几页。",
      caveat:
        "这个窗口里没有采集记录，说不出当时的观测口径（每轮读了几页）。下面的分布只能当参考：口径越宽，商品越晚「消失」。",
    };
  }
  const aperture = `观测口径：每轮只读搜索结果的前 ${hi} 页 × ${rows} 条 = ${hi * rows} 件。商品从这个范围里消失，不等于它被买走了——也可能是下架，也可能只是排名掉出了我们读的这几页。`;
  if (lo === hi) return { aperture, caveat: null };
  return {
    aperture,
    caveat: `这个窗口里观测口径变过（${lo} 页 → ${hi} 页，每页 ${rows} 条），所以分布内部不可比：口径越宽，商品越晚「消失」。等口径稳定满一个窗口再横向比较。`,
  };
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

/** Real merchant titles run past 250 characters. Putting one in an aria-label
 *  makes a screen reader read the entire listing before it says which control
 *  this is, so labels built from titles identify the row with this short
 *  prefix instead.
 */
export function shortTitle(title: string): string {
  return title.length > 18 ? `${title.slice(0, 18)}…` : title;
}

/** A one-line display title derived from a long stored one.
 *
 *  Seller-card-collected listings carry the full description AS the title —
 *  no short title exists upstream — so the headline is derived here,
 *  deterministically: first clause up to a strong delimiter, capped at 24
 *  chars (plain slice is CJK-safe; every char is one code unit in the BMP
 *  text this marketplace produces). Titles that already fit (≤ 32 chars)
 *  pass through untouched. Not for list rows or aria-labels — that is
 *  shortTitle's job.
 */
export function displayTitle(title: string): string {
  if (title.length <= 32) return title;
  const clause = (title.split(/[，,。、！!？?\n]/, 1)[0] ?? "").trim();
  // A title that OPENS with a delimiter yields an empty clause; fall back to
  // a plain prefix rather than an empty headline.
  const head = clause || title.slice(0, 24).trim();
  return head.length > 24 ? head.slice(0, 24) : head;
}
