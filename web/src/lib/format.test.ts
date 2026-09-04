import { describe, expect, it } from "vitest";

import {
  formatChangeRatio,
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
