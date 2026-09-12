import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";

import { remediationFor } from "./apiErrors";

function apiError(status: number, message: string): ApiError {
  return new ApiError(status, message, {});
}

describe("remediationFor", () => {
  it("sends a risk-control 503 to the credential import", () => {
    const r = remediationFor(
      apiError(503, "upstream requires verification: import a fresh cookie session"),
    );
    expect(r).toEqual({ to: "/settings#session", label: "去导入凭证" });
  });

  it("ignores a 503 that is not about verification", () => {
    // 503 is a general "upstream is unhappy"; only the challenge case has a
    // screen that fixes it.
    expect(remediationFor(apiError(503, "collector is busy"))).toBeNull();
  });

  it("ignores the right text at the wrong status", () => {
    // A rule's stored last_error can echo this wording without being the
    // live challenge that a fresh import would clear.
    expect(remediationFor(apiError(502, "upstream requires verification"))).toBeNull();
  });

  it("returns nothing for a plain Error or a non-error", () => {
    expect(remediationFor(new Error("boom"))).toBeNull();
    expect(remediationFor("boom")).toBeNull();
    expect(remediationFor(null)).toBeNull();
  });
});
