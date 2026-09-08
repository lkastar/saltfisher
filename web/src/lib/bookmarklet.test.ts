import { describe, expect, it } from "vitest";

import { buildBookmarklet } from "./bookmarklet";

const SCRIPT = buildBookmarklet("https://panel.example:8000", "tk_abc-123_XYZ");
/** What the browser evaluates: the URL, percent-decoded. */
const SOURCE = decodeURIComponent(SCRIPT.slice("javascript:".length));

/** Run the snapshot-collecting half of the real bookmarklet against a fake
 *  browser.
 *
 *  Cut out of `SOURCE` rather than reimplemented: a copy of the collector in
 *  the test would keep passing after the bookmarklet stopped reading a field.
 *  The slice is delimited by the two literals that bracket it in the source,
 *  so a rename breaks the extraction loudly instead of testing nothing.
 */
function runCollector(nav: Record<string, unknown>): Record<string, unknown> {
  const start = SOURCE.indexOf("var z={};try{");
  const end = SOURCE.indexOf("}catch(e){z={}}") + "}catch(e){z={}}".length;
  expect(start).toBeGreaterThan(-1);
  const body = SOURCE.slice(start, end);

  const screen = { width: 1920, height: 1080, colorDepth: 24 };
  const Intl = {
    DateTimeFormat: () => ({
      resolvedOptions: () => ({ timeZone: "Europe/Berlin", locale: "de-DE" }),
    }),
  };
  return new Function(
    "navigator",
    "screen",
    "window",
    "Intl",
    `${body}return z;`,
  )(nav, screen, { devicePixelRatio: 2 }, Intl) as Record<string, unknown>;
}

describe("buildBookmarklet", () => {
  it("reaches the evaluator whole", () => {
    // The failure mode this guards is silent: the bookmark is parsed as a URL
    // before it is ever evaluated, so a `#` opens the fragment and a `?` opens
    // the query, and everything past the first one is gone. A CSS hex colour
    // put a `#` in the first draft; the ternary in the message logic put a `?`
    // in the second. Both produce a bookmark that just does nothing.
    //
    // Asserted through `URL` rather than by banning characters: the point is
    // that nothing is lost, not that some blocklist stayed current.
    expect(SCRIPT.startsWith("javascript:")).toBe(true);
    const parsed = new URL(SCRIPT);
    expect(parsed.hash).toBe("");
    expect(parsed.search).toBe("");
    expect(SCRIPT).not.toContain("\n");
    // ...and the whole path is what the browser will run, after the decode it
    // does on the way in.
    expect(decodeURIComponent(parsed.pathname)).toBe(SOURCE);
  });

  it("decodes back to syntactically valid JavaScript", () => {
    // The stray-quote failure this file exists to catch produces a bookmark
    // that does nothing when clicked, with no error anywhere. Parsing what the
    // browser will actually evaluate is the only check that sees it.
    expect(SOURCE.startsWith("(function(){")).toBe(true);
    expect(() => new Function(SOURCE)).not.toThrow();
  });

  it("bakes in the panel's own origin and the ticket", () => {
    expect(SOURCE).toContain('"https://panel.example:8000/api/session/cookies"');
    expect(SOURCE).toContain('"tk_abc-123_XYZ"');
  });

  it("carries the ticket header and no bearer token", () => {
    expect(SOURCE).toContain("x-sfd-import-ticket");
    expect(SOURCE.toLowerCase()).not.toContain("authorization");
    expect(SOURCE.toLowerCase()).not.toContain("bearer");
  });

  it("never blocks the goofish page", () => {
    // alert/confirm freeze the tab the user is mid-login on.
    expect(SOURCE).not.toMatch(/\balert\(/);
    expect(SOURCE).not.toMatch(/\bconfirm\(/);
  });

  it("does not claim recovery on a successful import", () => {
    // 判定成功看行为，不看 cookie 名单: the import proves nothing on its own,
    // so the on-page message has to send the user to 立即运行.
    expect(SOURCE).toContain("立即运行");
    expect(SOURCE).not.toContain("已恢复");
  });

  it("collects the environment the collector has to match", () => {
    // The whole reason this exists: the cookies come from the user's browser
    // and the requests carrying them used to announce a different one. Every
    // field the backend's EnvSnapshot accepts has to actually be read here, or
    // the schema quietly grows a column nothing ever fills.
    for (const key of [
      "user_agent",
      "platform",
      "language",
      "languages",
      "hardware_concurrency",
      "device_memory",
      "max_touch_points",
      "ua_data",
      "screen_width",
      "screen_height",
      "device_pixel_ratio",
      "color_depth",
      "time_zone",
      "locale",
    ]) {
      expect(SOURCE).toContain(`'${key}'`);
    }
    expect(SOURCE).toContain("env:z");
  });

  it("omits what the browser does not offer instead of sending null", () => {
    // `userAgentData` does not exist in Firefox or Safari and `deviceMemory`
    // is Chromium-only. A null would reach the backend as "the browser says it
    // has none", which is a different claim.
    const snapshot = runCollector({
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Firefox/129.0",
      platform: "Linux x86_64",
      language: "en-US",
      languages: ["en-US", "en"],
      hardwareConcurrency: 8,
      maxTouchPoints: 0,
    });

    expect(snapshot).not.toHaveProperty("ua_data");
    expect(snapshot).not.toHaveProperty("device_memory");
    expect(Object.values(snapshot)).not.toContain(null);
    expect(snapshot.user_agent).toContain("Firefox/129.0");
    // ...and 0 is a value, not an absence.
    expect(snapshot.max_touch_points).toBe(0);
  });

  it("reads a Chromium environment whole", () => {
    const snapshot = runCollector({
      userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0",
      platform: "Win32",
      language: "zh-CN",
      languages: ["zh-CN", "zh"],
      hardwareConcurrency: 24,
      deviceMemory: 8,
      maxTouchPoints: 0,
      userAgentData: {
        toJSON: () => ({
          brands: [{ brand: "Google Chrome", version: "141" }],
          mobile: false,
          platform: "Windows",
        }),
      },
    });

    expect(snapshot).toEqual({
      user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0",
      platform: "Win32",
      language: "zh-CN",
      languages: ["zh-CN", "zh"],
      hardware_concurrency: 24,
      device_memory: 8,
      max_touch_points: 0,
      ua_data: {
        brands: [{ brand: "Google Chrome", version: "141" }],
        mobile: false,
        platform: "Windows",
      },
      screen_width: 1920,
      screen_height: 1080,
      device_pixel_ratio: 2,
      color_depth: 24,
      time_zone: "Europe/Berlin",
      locale: "de-DE",
    });
  });

  it("gives up the snapshot rather than the cookies", () => {
    // The credentials are why the user clicked. A browser that throws reading
    // one of these properties must cost the fingerprint, not the import.
    const snapshot = runCollector({
      get userAgent(): string {
        throw new TypeError("no");
      },
    });
    expect(snapshot).toEqual({});
  });

  it("quotes a ticket that contains a quote", () => {
    const nasty = buildBookmarklet("https://p", 'a"b\\c');
    expect(decodeURIComponent(nasty.slice("javascript:".length))).toContain('T="a\\"b\\\\c"');
  });
});
