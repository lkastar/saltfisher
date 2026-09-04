import { useState } from "react";
import { NavLink, Route, Routes } from "react-router";

import { clearToken, getToken } from "./api/client";
import LoginPage from "./pages/LoginPage";
import MonitorsPage from "./pages/MonitorsPage";

const NAV = [{ to: "/", label: "监控任务" }];

function AppShell() {
  return (
    <>
      <header
        style={{
          position: "sticky",
          top: 0,
          zIndex: "var(--z-header)",
          display: "flex",
          alignItems: "center",
          gap: "var(--space-4)",
          padding: "var(--space-2) var(--space-4)",
          background: "var(--surface)",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <strong style={{ fontSize: 14 }}>咸鱼监控</strong>
        <nav style={{ display: "flex", gap: "var(--space-3)", flex: 1 }}>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              style={({ isActive }) => ({
                color: isActive ? "var(--text)" : "var(--text-muted)",
                fontWeight: isActive ? 600 : 400,
                textDecoration: "none",
                padding: "var(--space-1) 0",
                borderBottom: isActive
                  ? "2px solid var(--primary)"
                  : "2px solid transparent",
              })}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <button
          type="button"
          onClick={() => {
            clearToken();
            window.location.reload();
          }}
        >
          退出
        </button>
      </header>
      <main
        style={{
          padding: "var(--space-4)",
          maxWidth: 1280,
          margin: "0 auto",
        }}
      >
        <Routes>
          <Route path="/" element={<MonitorsPage />} />
          <Route
            path="*"
            element={<p className="muted">没有这个页面。</p>}
          />
        </Routes>
      </main>
    </>
  );
}

/** ponytail: no <ProtectedRoute>. One user, one token -- "is there a token"
 *  is the entire authorization model, and request() drops the token and
 *  reloads if the backend ever rejects it.
 */
export default function App() {
  const [token, setTokenState] = useState(getToken);
  if (!token) return <LoginPage onAuthenticated={setTokenState} />;
  return <AppShell />;
}
