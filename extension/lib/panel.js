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

// Loopback and the three RFC 1918 blocks, plus IPv6 loopback and unique-local.
// This is the set Chrome's Local Network Access proposal calls "local"; the
// docs do not say whether extensions are subject to it, so the fetch states an
// expectation for these hosts and nothing more.
const LOCAL_PATTERNS = [
  /^localhost$/,
  /\.localhost$/,
  /^127\./,
  /^10\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^::1$/,
  /^f[cd][0-9a-f]{2}:/,
];

/**
 * Whether an address is one Local Network Access would care about.
 *
 * Gated rather than always-on: `targetAddressSpace: "local"` is an assertion
 * about the target, and asserting it for a panel on a public https host would
 * be a claim that is false. Chrome builds that do not know the option ignore
 * it either way.
 *
 * @param {string} hostname as `URL.hostname` gives it, IPv6 still bracketed
 */
export function isLocalAddress(hostname) {
  const host = String(hostname || "")
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  return LOCAL_PATTERNS.some((pattern) => pattern.test(host));
}
