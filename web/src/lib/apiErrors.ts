import { ApiError } from "../api/client";

export type Remediation = { to: string; label: string };

/** The page that can actually clear this error, when one exists.
 *
 *  An error that names its own fix ("import a fresh cookie session") and then
 *  makes the user go find that screen is only half an error message. This maps
 *  the fixable ones to the route that fixes them.
 *
 *  Matched on status AND on the detail text. The status alone is too broad —
 *  503 is a general "upstream is unhappy" and will pick up other causes — and
 *  the text alone would match a rule's stored `last_error` echoing something
 *  similar. The detail string is the backend's own wording
 *  (`api/monitors.run_monitor`), so it moves only when that line moves.
 */
export function remediationFor(error: unknown): Remediation | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 503 && error.message.toLowerCase().includes("requires verification")) {
    return { to: "/settings#session", label: "去导入凭证" };
  }
  return null;
}
