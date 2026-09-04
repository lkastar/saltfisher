import { useState } from "react";

import { ApiError, setToken, verifyToken } from "../api/client";

/** The token is checked against a real endpoint before it is stored, so a
 *  typo produces a message on this form rather than a broken app behind it.
 */
export default function LoginPage({
  onAuthenticated,
}: {
  onAuthenticated: (token: string) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const candidate = value.trim();
    if (!candidate) return;

    setChecking(true);
    setError(null);
    try {
      await verifyToken(candidate);
      setToken(candidate);
      onAuthenticated(candidate);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.status} · ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setChecking(false);
    }
  }

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-4)",
      }}
    >
      <form
        onSubmit={submit}
        style={{
          width: "min(360px, 100%)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-3)",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          padding: "var(--space-5)",
        }}
      >
        <h1>咸鱼监控</h1>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-1)",
          }}
        >
          <label htmlFor="token">API Token</label>
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-describedby={error ? "token-error" : undefined}
            aria-invalid={error ? true : undefined}
            autoFocus
          />
          <span className="muted" style={{ fontSize: 11.5 }}>
            与后端 SFD_API_TOKEN 一致。
          </span>
        </div>

        {error ? (
          <p
            id="token-error"
            role="alert"
            className="mono"
            style={{
              margin: 0,
              fontSize: 12,
              color: "var(--danger)",
              background: "var(--danger-bg)",
              border: "1px solid var(--danger)",
              borderRadius: "var(--radius-sm)",
              padding: "var(--space-2)",
              wordBreak: "break-word",
            }}
          >
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          data-variant="primary"
          disabled={checking || !value.trim()}
        >
          {checking ? "验证中…" : "进入"}
        </button>
      </form>
    </main>
  );
}
