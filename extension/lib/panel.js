/** Where the extension is allowed to send anything: the panel the user typed.
 *
 *  There is exactly one destination in this extension and it is computed here
 *  from `chrome.storage.local`. No telemetry, no update check, no third party
 *  -- `web/src/lib/extension.test.ts` asserts that by enumerating every URL
 *  literal in `extension/`, not by grepping for a known name.
 */

/** The endpoint the devtools paste and the bookmarklet already use. A path,
 *  never a URL: the host half only ever comes from the stored origin. */
export const IMPORT_PATH = "/api/session/cookies";

/**
 * Turn what a human typed into an origin, or "" if it is not one.
 *
 * A panel address is typed without a scheme more often than not, and
 * `new URL("192.168.1.10:8000")` does not fail on that -- it parses
 * `192.168.1.10:` as the scheme and `8000` as the path. Defaulting to http is
 * the honest guess: a self-hosted panel on a LAN address is plain http, and
 * the alternative (assuming https) fails at connect time with an error that
 * points nowhere near the cause.
 *
 * @param {unknown} input
 * @returns {string} an origin like `http://192.168.1.10:8000`, or ""
 */
export function normalisePanelOrigin(input) {
  const raw = String(input ?? "").trim();
  if (!raw) return "";
  const withScheme = /^[a-z][a-z\d+.-]*:\/\//i.test(raw) ? raw : `http://${raw}`;
  try {
    const url = new URL(withScheme);
    // `new URL("http://")` throws, but `new URL("http://?x")` does not and
    // yields an opaque origin -- reject anything without a host rather than
    // fetch "null/api/session/cookies".
    return url.hostname ? url.origin : "";
  } catch {
    return "";
  }
}

// Chrome's Local Network Access has THREE address spaces, not two:
// `public`, `local` (the RFC 1918 blocks and IPv6 unique-local) and
// `loopback` (127.0.0.0/8, ::1, and the names that resolve to them).
//
// Getting that wrong is not a no-op, which is the whole reason this is a
// classifier and not a boolean. `targetAddressSpace` is an ASSERTION about
// the target, so declaring `local` for a loopback panel is a false claim and
// Chrome refuses the request outright:
//
//   blocked by CORS policy: Request had a target IP address space of `local`
//   yet the resource is in address space `loopback`
//
// Measured 2026-09-08 against http://127.0.0.1:8000, which is exactly the
// address a self-hosted panel runs on.
const LOOPBACK_PATTERNS = [/^localhost$/, /\.localhost$/, /^127\./, /^::1$/];
const PRIVATE_PATTERNS = [
  /^10\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^f[cd][0-9a-f]{2}:/,
];

/**
 * Which address space a host is in, as `targetAddressSpace` names them.
 *
 * Returns null for anything else, and the caller then sends no assertion at
 * all -- claiming `local` for a public host would be false in the other
 * direction. Chrome builds that do not know the option ignore it either way.
 *
 * @param {string} hostname as `URL.hostname` gives it, IPv6 still bracketed
 * @returns {"loopback" | "local" | null}
 */
export function addressSpace(hostname) {
  const host = String(hostname || "")
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  if (LOOPBACK_PATTERNS.some((p) => p.test(host))) return "loopback";
  if (PRIVATE_PATTERNS.some((p) => p.test(host))) return "local";
  return null;
}
