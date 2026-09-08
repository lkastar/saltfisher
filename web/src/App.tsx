import { useState } from "react";
import { NavLink, Route, Routes } from "react-router";

import { clearToken, getToken } from "./api/client";
import { Icon } from "./components/Icon";
import AnalyticsPage from "./pages/AnalyticsPage";
import ChannelsPage from "./pages/ChannelsPage";
import ItemDetailPage from "./pages/ItemDetailPage";
import ItemsPage from "./pages/ItemsPage";
import LoginPage from "./pages/LoginPage";
import MonitorsPage from "./pages/MonitorsPage";
import SettingsPage from "./pages/SettingsPage";
import WatchlistPage from "./pages/WatchlistPage";

const NAV = [
  { to: "/", label: "监控任务" },
  { to: "/items", label: "命中商品" },
  { to: "/watchlist", label: "收藏追踪" },
  { to: "/analytics", label: "行情分析" },
  { to: "/channels", label: "通知渠道" },
  { to: "/settings", label: "设置" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  const label = theme === "light" ? "切换至深色模式" : "切换至浅色模式";
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      style={{ display: "inline-flex", alignItems: "center" }}
      onClick={() => {
        const next = theme === "light" ? "dark" : "light";
        document.documentElement.dataset.theme = next;
        localStorage.setItem("sfd-theme", next);
        setTheme(next);
      }}
    >
      <Icon name={theme === "light" ? "moon" : "sun"} />
    </button>
  );
}

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
        <ThemeToggle />
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
          <Route path="/items" element={<ItemsPage />} />
          <Route path="/items/:itemId" element={<ItemDetailPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/channels" element={<ChannelsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
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
