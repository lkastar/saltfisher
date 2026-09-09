import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation, type Location } from "react-router";

import { clearToken, getToken } from "./api/client";
import { sessionOptions } from "./api/queries";
import { Backdrop } from "./components/Backdrop";
import { Icon, type IconName } from "./components/Icon";
import { useMagnetic, useScrollProgress, useSpotlight } from "./lib/fx";
import AnalyticsPage from "./pages/AnalyticsPage";
import ItemDetailPage from "./pages/ItemDetailPage";
import ItemsPage from "./pages/ItemsPage";
import LoginPage from "./pages/LoginPage";
import OverviewPage from "./pages/OverviewPage";
import SettingsPage from "./pages/SettingsPage";
import WatchlistPage from "./pages/WatchlistPage";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "总览", icon: "layout-dashboard" },
  { to: "/#tasks", label: "监控任务", icon: "scan-search" },
  { to: "/items", label: "命中商品", icon: "package" },
  { to: "/watchlist", label: "收藏追踪", icon: "bookmark" },
  { to: "/analytics", label: "行情分析", icon: "chart-spline" },
  { to: "/settings", label: "设置", icon: "settings" },
];

/** Plain Links with hand-computed active state, not NavLink: `/` and
 *  `/#tasks` share a pathname, so NavLink's matcher would mark BOTH 总览 and
 *  监控任务 aria-current at `/`. The hash is the tiebreaker.
 */
function navActive(to: string, location: Location): boolean {
  if (to === "/") return location.pathname === "/" && location.hash !== "#tasks";
  if (to === "/#tasks") return location.pathname === "/" && location.hash === "#tasks";
  return location.pathname === to || location.pathname.startsWith(`${to}/`);
}

function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  const label = theme === "light" ? "切换至深色模式" : "切换至浅色模式";
  return (
    <button
      type="button"
      className="theme-toggle"
      data-magnetic
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

/** Decorative backdrop lives in components/Backdrop.tsx (shared with the
 *  login page). AppShell adds the interactive layers on top: card spotlight,
 *  magnetic elements, and the scroll-progress hairline.
 */
function AppShell() {
  const location = useLocation();
  const progress = useRef<HTMLDivElement | null>(null);
  useSpotlight();
  useMagnetic();
  useScrollProgress(progress);
  return (
    <>
      <Backdrop />
      <header className="topbar">
        <div ref={progress} className="scroll-progress" aria-hidden="true" />
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <span className="brand-mark" data-magnetic>
              <Icon name="radar" />
            </span>
            咸鱼监控
          </Link>
          <nav className="main-nav">
            {NAV.map((item) => {
              const active = navActive(item.to, location);
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  aria-label={item.label}
                  aria-current={active ? "page" : undefined}
                  className={active ? "active" : ""}
                >
                  <Icon name={item.icon} />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </nav>
          <div className="topbar-actions">
            <ThemeToggle />
            <TopbarStatus />
            <button
              type="button"
              className="btn-logout"
              aria-label="退出"
              onClick={() => {
                clearToken();
                window.location.reload();
              }}
            >
              <Icon name="log-out" />
              <span className="btn-logout-text">退出</span>
            </button>
          </div>
        </div>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          {/* Monitor CRUD now lives in the overview's #tasks card. */}
          <Route path="/monitors" element={<Navigate to="/#tasks" replace />} />
          <Route path="/items" element={<ItemsPage />} />
          <Route path="/items/:itemId" element={<ItemDetailPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          {/* Channel CRUD lives in the settings page's 通知渠道 section. */}
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
