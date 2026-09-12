/** Loading, error, and empty as three separate components.
 *
 *  For a monitoring tool these are three different facts about the world:
 *  "still asking", "the collector is broken", "nothing matched". Rendering
 *  them the same way -- or worse, collapsing isLoading || isError into one
 *  spinner -- hides exactly the failure the tool exists to report.
 *
 *  Visuals are the SIGNAL DECK prototype's: shimmer skeleton, tinted alert
 *  strip, centered empty block (`.skeleton`/`.alert`/`.empty` in base.css).
 */

import { useEffect, useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import { remediationFor } from "../lib/apiErrors";
import { Icon } from "./Icon";

/** A skeleton that appears only if the wait is actually perceptible. Showing
 *  it immediately makes a 40ms response flash, which reads as a glitch.
 */
export function Loading({ rows = 3 }: { rows?: number }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setVisible(true), 200);
    return () => clearTimeout(t);
  }, []);

  if (!visible) return null;

  return (
    <div className="skeleton" aria-busy="true" aria-live="polite">
      <span className="muted" style={{ fontSize: 12 }}>
        加载中…
      </span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="sk" style={{ width: `${100 - i * 12}%` }} />
      ))}
    </div>
  );
}

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** role="alert" is not decoration: an error conveyed by red pixels alone
 *  never reaches a screen reader.
 */
export function ErrorState({
  title = "请求失败",
  error,
  onRetry,
  tone = "danger",
  action,
}: {
  title?: string;
  error: unknown;
  onRetry?: () => void;
  tone?: "danger" | "warn";
  /** Extra escape hatch beyond retry, e.g. "查看任务" on the items page's
   *  broken-rule fork. Supplying it suppresses the automatic remediation
   *  link, so a caller with a better next step always wins. */
  action?: React.ReactNode;
}) {
  // Every caller gets this, rather than each one remembering: an error whose
  // own text names the fix ("import a fresh cookie session") should carry the
  // way to it. See lib/apiErrors.
  const fix = action === undefined ? remediationFor(error) : null;
  return (
    <div role="alert" className="alert" data-tone={tone}>
      <Icon
        name={tone === "warn" ? "alert-triangle" : "alert-circle"}
        size={15}
      />
      <strong>{title}</strong>
      <code className="alert-msg">{messageOf(error)}</code>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          重试
        </button>
      ) : null}
      {fix ? (
        <Link className="btn-text" to={fix.to}>
          {fix.label}
          <Icon name="arrow-right" size={13} />
        </Link>
      ) : null}
      {action}
    </div>
  );
}

/** Empty always names the next step. A blank panel leaves the user unable to
 *  tell "nothing matched" from "this screen is broken".
 */
export function Empty({
  message,
  action,
}: {
  message: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name="inbox" size={26} />
      <span>{message}</span>
      {action}
    </div>
  );
}
