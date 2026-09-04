import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, verifyToken } from "./client";

/** verifyToken is the one entry point that touches neither localStorage nor
 *  window, so the error-message mapping can be checked without a DOM.
 */
function respondWith(body: string, status: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("error messages", () => {
  it("passes the backend's own detail through", async () => {
    respondWith(JSON.stringify({ detail: "invalid or missing bearer token" }), 401);
    await expect(verifyToken("nope")).rejects.toMatchObject({
      status: 401,
      message: "invalid or missing bearer token",
    });
  });

  it("flattens a 422 into field-level reasons", async () => {
    respondWith(
      JSON.stringify({
        detail: [
          { loc: ["body", "interval_seconds"], msg: "should be >= 60" },
          { loc: ["body", "keyword"], msg: "should have at least 1 character" },
        ],
      }),
      422,
    );
    await expect(verifyToken("t")).rejects.toMatchObject({
      status: 422,
      message:
        "interval_seconds: should be >= 60；keyword: should have at least 1 character",
    });
  });

  it("names the cause when a gateway answers with no body", async () => {
    // The dev proxy and the container both do this when the app is down.
    // "HTTP 502" is not something a user can act on.
    respondWith("", 502);
    await expect(verifyToken("t")).rejects.toMatchObject({
      status: 502,
      message: "后端无响应，确认服务是否在运行",
    });
  });

  it("falls back without pretending to know more", async () => {
    respondWith("", 418);
    await expect(verifyToken("t")).rejects.toMatchObject({
      status: 418,
      message: "服务器未返回错误详情",
    });
  });

  it("resolves quietly when the token is accepted", async () => {
    respondWith("[]", 200);
    await expect(verifyToken("dev-token")).resolves.toBeUndefined();
  });

  it("throws ApiError, so callers can read the status", async () => {
    respondWith("", 502);
    await expect(verifyToken("t")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("field-level 422 detail", () => {
  it("keys each reason by its field so a form can attach it", async () => {
    respondWith(
      JSON.stringify({
        detail: [
          { loc: ["body", "interval_seconds"], msg: "should be >= 60" },
          { loc: ["body", "name"], msg: "should have at least 1 character" },
        ],
      }),
      422,
    );
    await expect(verifyToken("t")).rejects.toMatchObject({
      fields: {
        interval_seconds: "should be >= 60",
        name: "should have at least 1 character",
      },
    });
  });

  it("leaves fields empty for a plain string detail", async () => {
    respondWith(JSON.stringify({ detail: "item not found" }), 404);
    await expect(verifyToken("t")).rejects.toMatchObject({ fields: {} });
  });
});
