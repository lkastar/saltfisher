import { useState } from "react";

import { ApiError, setToken, verifyToken } from "../api/client";
import { Backdrop } from "../components/Backdrop";
import { Icon } from "../components/Icon";
import { useMagnetic } from "../lib/fx";

/** The token is checked against a real endpoint before it is stored, so a
 *  typo produces a message on this form rather than a broken app behind it.
 *
 *  Same SIGNAL DECK stage as the app shell: shared Backdrop, giant outlined
 *  SIGNAL behind a frosted card. Auth flow untouched.
 */
export default function LoginPage({
  onAuthenticated,
}: {
  onAuthenticated: (token: string) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  // Login renders instead of AppShell, so the magnetic layer is wired here.
  useMagnetic();

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
    <main className="login-wrap">
      <Backdrop />
      <span className="login-ghost" aria-hidden="true">
        SIGNAL
      </span>
      <form onSubmit={submit} className="card login-card">
        <div className="brand">
          <span className="brand-mark" data-magnetic>
            <Icon name="radar" />
          </span>
          咸鱼监控
        </div>
        <div className="hero-eyebrow" style={{ margin: 0 }}>
          SIGNAL DECK · ACCESS
        </div>
        <div className="field">
          <label className="field-label" htmlFor="token">
            API Token
          </label>
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
          <p id="token-error" role="alert" className="alert" data-tone="danger">
            <Icon name="alert-circle" size={15} />
            <code className="alert-msg">{error}</code>
          </p>
        ) : null}

        <button
          type="submit"
          data-variant="primary"
          data-magnetic
          disabled={checking || !value.trim()}
        >
          {checking ? "验证中…" : "进入"}
        </button>
      </form>
    </main>
  );
}
