import { describe, expect, it } from "vitest";

import { buildBookmarklet } from "./bookmarklet";

const SCRIPT = buildBookmarklet("https://panel.example:8000", "tk_abc-123_XYZ");
/** What the browser evaluates: the URL, percent-decoded. */
const SOURCE = decodeURIComponent(SCRIPT.slice("javascript:".length));

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

  it("quotes a ticket that contains a quote", () => {
    const nasty = buildBookmarklet("https://p", 'a"b\\c');
    expect(decodeURIComponent(nasty.slice("javascript:".length))).toContain('T="a\\"b\\\\c"');
  });
});
