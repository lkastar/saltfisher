import { describe, expect, it } from "vitest";

import {
  apertureNote,
  formatChangeRatio,
  formatDuration,
  formatPrice,
  formatRelativeTime,
  parseUtc,
  parseYuanToCents,
} from "./format";

describe("formatPrice", () => {
  it("divides cents by 100", () => {
    expect(formatPrice(261900)).toBe("¥2,619");
    expect(formatPrice(41000)).toBe("¥410");
  });

  it("shows decimals only when there are any", () => {
    expect(formatPrice(41050)).toBe("¥410.50");
    expect(formatPrice(1)).toBe("¥0.01");
  });

  it("renders zero as a price, not as missing", () => {
    // A free listing is real; "—" would misreport it as unknown.
    expect(formatPrice(0)).toBe("¥0");
  });

  it("does not crash on values that should never arrive", () => {
    expect(formatPrice(null)).toBe("—");
    expect(formatPrice(undefined)).toBe("—");
    expect(formatPrice(Number.NaN)).toBe("—");
    // A negative price cannot come off this API; the only requirement
    // here is that it renders something instead of throwing.
    expect(formatPrice(-100)).toBe("¥-1");
  });
});

describe("formatChangeRatio", () => {
  it("marks direction with a glyph, never colour alone", () => {
    expect(formatChangeRatio(-0.168)).toBe("▼ 16.8%");
    expect(formatChangeRatio(0.042)).toBe("▲ 4.2%");
    expect(formatChangeRatio(0)).toBe("持平");
  });
});

describe("parseUtc", () => {
  it("treats an offset-less timestamp as UTC, not local", () => {
    // Date would read the bare string as local time and shift it by the
    // machine's offset -- every "last run" would be wrong away from UTC.
    expect(parseUtc("2026-09-04T09:41:04").toISOString()).toBe(
      "2026-09-04T09:41:04.000Z",
    );
  });

  it("respects an explicit offset", () => {
    expect(parseUtc("2026-09-04T09:41:04+00:00").toISOString()).toBe(
      "2026-09-04T09:41:04.000Z",
    );
  });
});

describe("formatRelativeTime", () => {
  it("distinguishes never-ran from just-ran", () => {
    expect(formatRelativeTime(null)).toBe("从未");
    expect(formatRelativeTime(new Date().toISOString())).toBe("刚刚");
  });

  it("scales through the units", () => {
    const ago = (s: number) => new Date(Date.now() - s * 1000).toISOString();
    expect(formatRelativeTime(ago(90))).toBe("1 分钟前");
    expect(formatRelativeTime(ago(3 * 3600))).toBe("3 小时前");
    expect(formatRelativeTime(ago(50 * 3600))).toBe("2 天前");
  });
});

describe("parseYuanToCents", () => {
  it("converts what a user types into cents", () => {
    expect(parseYuanToCents("2180")).toBe(218000);
    expect(parseYuanToCents("410.5")).toBe(41050);
    expect(parseYuanToCents(" 99 ")).toBe(9900);
  });

  it("distinguishes blank from zero", () => {
    // Blank means "no bound"; 0 is a real lower bound.
    expect(parseYuanToCents("")).toBe(null);
    expect(parseYuanToCents("   ")).toBe(null);
    expect(parseYuanToCents("0")).toBe(0);
  });

  it("rejects what is not a non-negative number", () => {
    expect(parseYuanToCents("abc")).toBe(null);
    expect(parseYuanToCents("-5")).toBe(null);
    expect(parseYuanToCents("1e999")).toBe(null);
  });

  it("rounds instead of truncating", () => {
    // 12.345 * 100 is 1234.4999... in binary floating point.
    expect(parseYuanToCents("12.345")).toBe(1235);
  });
});

describe("formatDuration", () => {
  it("keeps sub-hour spans in minutes", () => {
    // The shortest measurable duration: seen in one cycle, gone by the next.
    expect(formatDuration(0)).toBe("0 分钟");
    expect(formatDuration(25)).toBe("25 分钟");
    expect(formatDuration(59)).toBe("59 分钟");
  });

  it("switches to hours, without a trailing .0", () => {
    expect(formatDuration(60)).toBe("1 小时");
    expect(formatDuration(150)).toBe("2.5 小时");
    expect(formatDuration(2820)).toBe("47 小时");
  });

  it("switches to days past two of them", () => {
    expect(formatDuration(2880)).toBe("2 天");
    expect(formatDuration(2940)).toBe("2 天 1 小时");
  });

  it("never rounds its way to 24 hours", () => {
    // 4319 minutes is 71.98 hours. Rounding the leftover hours instead of
    // flooring them first prints "2 天 24 小时", which is not a duration.
    expect(formatDuration(4319)).toBe("2 天 23 小时");
    expect(formatDuration(4320)).toBe("3 天");
  });

  it("says nothing rather than NaN", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("apertureNote", () => {
  it("states the aperture and raises nothing when it held still", () => {
    const { aperture, caveat } = apertureNote(2, 2, 30);
    expect(aperture).toContain("前 2 页 × 30 条 = 60 件");
    expect(caveat).toBe(null);
  });

  it("says so when the aperture changed inside the window", () => {
    // The whole reason the field exists: 1x30 and 2x30 are not comparable,
    // and the wider one makes a listing "disappear" later.
    const { caveat } = apertureNote(1, 2, 30);
    expect(caveat).toContain("口径变过");
    expect(caveat).toContain("1 页 → 2 页");
  });

  it("says so when there is no record of the aperture at all", () => {
    // Real case: `iPhone 15` has 264 ledger items and zero CollectRun rows,
    // because they were collected before the run log existed.
    expect(apertureNote(null, null, 30).caveat).toContain("没有采集记录");
    expect(apertureNote(undefined, undefined, 30).caveat).toContain("没有采集记录");
  });

  it("never claims a disappearance was a purchase", () => {
    // Acceptance item. A listing stops coming back because it was bought,
    // because it was delisted, or because its rank fell past our pages.
    for (const args of [[1, 1], [1, 2], [null, null]] as const) {
      const { aperture, caveat } = apertureNote(args[0], args[1], 30);
      for (const text of [aperture, caveat ?? ""]) {
        expect(text).not.toContain("成交");
        expect(text).not.toContain("售出");
      }
    }
  });
});
