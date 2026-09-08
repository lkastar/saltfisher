import { describe, expect, it } from "vitest";

import { nextParams, readFilters, sellerLabel } from "./itemFilters";

/** Real seller ids, copied out of `tests/fixtures/search_real.json`. The first
 *  one starts with `+`, which is exactly the character a hand-built query
 *  string turns into a space.
 */
const REAL_IDS = [
  "+5xYqEw3f9sETQuuxt2Emw==",
  "6C9UyAl5OsXgten6tYo/sQ==",
  "a2WguXOv/jPBahwtlsZkGQ==",
];

describe("seller_id round trip", () => {
  it.each(REAL_IDS)("survives write -> URL text -> read: %s", (id) => {
    const written = nextParams(new URLSearchParams("sort=price"), "seller_id", id);
    // Through the actual URL string, because that is what the browser stores
    // and what a shared link carries -- not through the object.
    const reread = readFilters(new URLSearchParams(written.toString()));

    expect(reread.seller_id).toBe(id);
    expect(reread.sort).toBe("price");
  });

  it("escapes the id instead of shipping it raw", () => {
    const query = nextParams(new URLSearchParams(), "seller_id", REAL_IDS[0]!).toString();
    // `+` raw in a query string reads back as a space; `/` and `=` are
    // likewise structural. If any of them appear unescaped, the round trip
    // above is passing by luck.
    expect(query).toBe("seller_id=%2B5xYqEw3f9sETQuuxt2Emw%3D%3D");
  });

  it("is dropped by the clear button, not turned into an empty filter", () => {
    const cleared = nextParams(
      new URLSearchParams(`seller_id=${encodeURIComponent(REAL_IDS[0]!)}`),
      "seller_id",
      undefined,
    );
    expect(cleared.toString()).toBe("");
    expect(readFilters(cleared).seller_id).toBeUndefined();
  });

  it("resets the page, so a new seller does not open on page 3", () => {
    const written = nextParams(
      new URLSearchParams("offset=100"),
      "seller_id",
      REAL_IDS[1]!,
    );
    expect(readFilters(written).offset).toBe(0);
  });

  it("keeps paging within one seller", () => {
    const paged = nextParams(
      new URLSearchParams(`seller_id=${encodeURIComponent(REAL_IDS[1]!)}`),
      "offset",
      "50",
      false,
    );
    const reread = readFilters(new URLSearchParams(paged.toString()));
    expect(reread).toMatchObject({ seller_id: REAL_IDS[1], offset: 50 });
  });

  it("reads an absent or blank id as no filter at all", () => {
    expect(readFilters(new URLSearchParams("")).seller_id).toBeUndefined();
    // `?seller_id=` would otherwise ask the API for the seller called "".
    expect(readFilters(new URLSearchParams("seller_id=")).seller_id).toBeUndefined();
  });
});

describe("sellerLabel", () => {
  it("prefers the nick", () => {
    expect(sellerLabel("老王", REAL_IDS[0]!)).toBe("老王");
  });

  it("falls back to a short id rather than showing base64 as a name", () => {
    // Empty nick: 8 of the 253 sellers in the real db. Undefined: the list
    // came back empty, so there is no row to read a nick off.
    expect(sellerLabel("", REAL_IDS[0]!)).toBe("+5xYqEw3…");
    expect(sellerLabel(undefined, REAL_IDS[0]!)).toBe("+5xYqEw3…");
    expect(sellerLabel("   ", REAL_IDS[0]!)).toBe("+5xYqEw3…");
  });
});
