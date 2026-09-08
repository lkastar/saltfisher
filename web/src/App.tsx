import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router";

import { clearToken, getToken } from "./api/client";
import { sessionOptions } from "./api/queries";
import { Icon, type IconName } from "./components/Icon";
import AnalyticsPage from "./pages/AnalyticsPage";
import ItemDetailPage from "./pages/ItemDetailPage";
import ItemsPage from "./pages/ItemsPage";
import LoginPage from "./pages/LoginPage";
import MonitorsPage from "./pages/MonitorsPage";
import OverviewPage from "./pages/OverviewPage";
import SettingsPage from "./pages/SettingsPage";
import WatchlistPage from "./pages/WatchlistPage";

/** Prototype nav, except 监控任务 points at /monitors instead of /#tasks:
 *  the overview has no #tasks section until step 4, and redirecting away from
 *  MonitorsPage now would orphan monitor create/edit. Step 4 flips this to
 *  /#tasks and adds the redirect. */
const NAV: { to: string; label: string; icon: IconName; end?: boolean }[] = [
  { to: "/", label: "总览", icon: "layout-dashboard", end: true },
  { to: "/monitors", label: "监控任务", icon: "scan-search" },
  { to: "/items", label: "命中商品", icon: "package" },
  { to: "/watchlist", label: "收藏追踪", icon: "bookmark" },
  { to: "/analytics", label: "行情分析", icon: "chart-spline" },
  { to: "/settings", label: "设置", icon: "settings" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  const label = theme === "light" ? "切换至深色模式" : "切换至浅色模式";
  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label={label}
      title={label}
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

/** Session health in the topbar. The prototype's static "LIVE" pill, wired to
 *  the real /api/session state — a hardcoded LIVE would claim liveness the
 *  backend never proved. Amber is reserved for risk control (spec). */
function TopbarStatus() {
  const session = useQuery({ ...sessionOptions(), refetchInterval: 30_000 });
  let tone: "ok" | "warn" | "err" | "dim" = "dim";
  let label = "…";
  if (session.isError) {
    tone = "err";
    label = "离线";
  } else if (session.data) {
    if (!session.data.usable) {
      tone = "err";
      label = "会话不可用";
    } else if (session.data.needs_verification || session.data.challenged_apis.length > 0) {
      tone = "warn";
      label = "风控";
    } else {
      tone = "ok";
      label = "LIVE";
    }
  }
  return (
    <span className="topbar-status" data-tone={tone}>
      <span className={tone === "ok" ? "dot dot-pulse" : "dot"} />
      {label}
    </span>
  );
}

function AppShell() {
  return (
    <>
      <div className="bg-grid" />
      <div className="bg-vignette" />
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">
            <span className="brand-mark">
              <Icon name="radar" />
            </span>
            咸鱼监控
          </NavLink>
          <nav className="main-nav">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                aria-label={item.label}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="topbar-actions">
            <ThemeToggle />
            <TopbarStatus />
            <button
              type="button"
              className="btn-logout"
              onClick={() => {
                clearToken();
                window.location.reload();
              }}
            >
              <Icon name="log-out" />
              退出
            </button>
          </div>
        </div>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          {/* Kept (not redirected) until step 4 folds monitor CRUD into the
              overview — see NAV note above. */}
          <Route path="/monitors" element={<MonitorsPage />} />
          <Route path="/items" element={<ItemsPage />} />
          <Route path="/items/:itemId" element={<ItemDetailPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          {/* Channel CRUD is unreachable until step 7 merges it into the
              settings page; ChannelsPage.tsx stays on disk for that merge. */}
          <Route path="/channels" element={<Navigate to="/settings#channels" replace />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<p className="muted">没有这个页面。</p>} />
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
