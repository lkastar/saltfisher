/** Loading, error, and empty as three separate components.
 *
 *  For a monitoring tool these are three different facts about the world:
 *  "still asking", "the collector is broken", "nothing matched". Rendering
 *  them the same way -- or worse, collapsing isLoading || isError into one
 *  spinner -- hides exactly the failure the tool exists to report.
 */

import { useEffect, useState } from "react";

import { ApiError } from "../api/client";

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
    <div
      aria-busy="true"
      aria-live="polite"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        padding: "var(--space-3)",
      }}
    >
      <span className="muted" style={{ fontSize: 12 }}>
        加载中…
      </span>
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          style={{
            height: 11,
            borderRadius: "var(--radius-sm)",
            background: "var(--surface-2)",
            width: `${100 - i * 12}%`,
          }}
        />
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
}: {
  title?: string;
  error: unknown;
  onRetry?: () => void;
  tone?: "danger" | "warn";
}) {
  return (
    <div
      role="alert"
      style={{
        border: `1px solid var(--${tone})`,
        background: `var(--${tone}-bg)`,
        borderRadius: "var(--radius)",
        padding: "var(--space-3)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        alignItems: "flex-start",
      }}
    >
      <strong style={{ color: `var(--${tone})` }}>{title}</strong>
      <code className="mono" style={{ fontSize: 12, wordBreak: "break-word" }}>
        {messageOf(error)}
      </code>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          重试
        </button>
      ) : null}
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
    <div
      style={{
        padding: "var(--space-5) var(--space-3)",
        textAlign: "center",
        color: "var(--text-muted)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
        alignItems: "center",
      }}
    >
      <span>{message}</span>
      {action}
    </div>
  );
}
