import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import {
  channelsOptions,
  itemPricesOptions,
  itemsOptions,
  monitorsOptions,
  notifyLogsOptions,
  sessionOptions,
  statsOverviewOptions,
  watchlistOptions,
  type Channel,
  type ItemFilters,
} from "../api/queries";
import { Icon } from "../components/Icon";
import MonitorTrend from "../components/MonitorTrend";
import { PageHero } from "../components/PageHero";
import RemoteImage from "../components/RemoteImage";
import Spark from "../components/Spark";
import { StatusPill } from "../components/StatusPill";
import { Empty, ErrorState, Loading } from "../components/States";
import { Ticker } from "../components/Ticker";
import {
  formatChangeRatio,
  formatPrice,
  formatRelativeTime,
} from "../lib/format";
import { useCountUp, useReveal } from "../lib/fx";
import { recentWatch } from "../lib/watchlist";

/** Decorative count-up for a KPI number. A leaf on purpose: the rAF loop in
 *  useCountUp re-renders this one span ~40 times, not the whole page.
 */
function CountUp({ value }: { value: number }) {
  return <>{useCountUp(value)}</>;
}

/** The recent-hits window feeds the ticker (12) and the 最近命中 card (5) --
 *  nothing counts from it any more, `/api/stats/overview` counts server-side.
 *  Module-level so the query key is one stable object, not a new identity
 *  per render.
 */
const RECENT_ITEMS: ItemFilters = { sort: "-first_seen", limit: 20 };

/** Dashboard cadence per hook-guidelines: 30s on overview queries, and no
 *  faster -- collection cadence is the real freshness limit.
 */
const POLL = 30_000;

/** Price history for one watched item, as a decorative spark.
 *
 *  NOT polled, unlike everything else on this page: price history moves at
 *  collection cadence (minutes to hours), not dashboard cadence, so a 30s
 *  refetch would multiply the request rate for a line that cannot change.
 *
 *  Renders nothing while pending or on error -- the price and change ratio
 *  beside it are the data, and a spinner per row would turn a calm card into
 *  five flickering boxes.
 */
function WatchSpark({ itemId, color }: { itemId: string; color: string }) {
  const prices = useQuery(itemPricesOptions(itemId));
  if (!prices.isSuccess) return null;
  // A PriceSnapshot is written only when price or status CHANGES, so a stable
  // listing has exactly one point and an item observed once has none. Spark
  // handles both (one point parks a dot at mid-height, zero returns null).
  return (
    <Spark
      values={prices.data.map((p) => p.price_cents)}
      color={color}
      width={90}
      height={22}
    />
  );
}

/** What the user pinned, newest first -- the overview's answer to "what do I
 *  care about", now that rule management lives at /monitors.
 *
 *  Ordered by `added_at` rather than by drop size: a ranking would bury a
 *  just-pinned item that has not moved yet, which is exactly the one the user
 *  came to check.
 *
 *  ponytail: one /api/items/{id}/prices request per row, 5 rows, no batching.
 *  Single-user self-hosted and a fixed row count make that ~5 cheap local
 *  requests. Add a batch endpoint if this list ever grows past ~10 rows.
 */
function WatchTrend() {
  const reveal = useReveal();
  const watch = useQuery({ ...watchlistOptions(), refetchInterval: POLL });
  const rows = recentWatch(watch.data ?? [], 5);

  return (
    <section className="card" data-reveal ref={reveal}>
      <div className="card-h">
        <h2>
          <Icon name="bookmark" size={15} />
          收藏走势
        </h2>
        <Link className="btn-text" to="/watchlist">
          全部
          <Icon name="arrow-right" size={13} />
        </Link>
      </div>

      {watch.isPending ? <Loading rows={3} /> : null}
      {watch.isError ? (
        <ErrorState
          title="拉取收藏失败"
          error={watch.error}
          onRetry={() => void watch.refetch()}
        />
      ) : null}
      {watch.isSuccess && rows.length === 0 ? (
        <Empty
          message="还没有收藏任何商品。"
          action={<Link to="/watchlist">去收藏一件</Link>}
        />
      ) : null}

      {rows.map((entry) => {
        // Green is cheaper here, the inverse of the convention users bring
        // with them -- which is why the arrow and the number in
        // formatChangeRatio carry the meaning and this only tints them.
        const tone =
          entry.change_cents === 0
            ? "var(--acc)"
            : entry.change_cents < 0
              ? "var(--green)"
              : "var(--red)";
        return (
          <div className="watch-row" key={entry.item_id}>
            <RemoteImage
              src={entry.cover_url}
              alt={entry.title}
              width={44}
              height={44}
            />
            <div className="watch-main">
              <div className="watch-title">
                <Link to={`/items/${entry.item_id}`} title={entry.title}>
                  {entry.title}
                </Link>
              </div>
              <span className="watch-age">{formatRelativeTime(entry.added_at)}</span>
            </div>
            <div className="watch-spark">
              <WatchSpark itemId={entry.item_id} color={tone} />
            </div>
            <div className="watch-figures">
              <span className="watch-price">{formatPrice(entry.price_cents)}</span>
              <span className="mono" style={{ color: tone }}>
                {formatChangeRatio(entry.change_ratio)}
              </span>
            </div>
            <StatusPill status={entry.status} />
          </div>
        );
      })}
    </section>
  );
}

export default function OverviewPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const reveal = useReveal();
  const monitors = useQuery({ ...monitorsOptions(), refetchInterval: POLL });
  const recent = useQuery({
    ...itemsOptions(RECENT_ITEMS),
    refetchInterval: POLL,
  });
  const logs = useQuery({ ...notifyLogsOptions(), refetchInterval: POLL });
  const stats = useQuery({ ...statsOverviewOptions(), refetchInterval: POLL });
  const session = useQuery(sessionOptions());
  const channels = useQuery(channelsOptions());

  // Legacy alias: monitor CRUD lived here as the #tasks card until 2026-09-12.
  // React Router cannot route on a hash, so the redirect is an effect. `replace`
  // keeps the dead URL out of history — Back should leave, not bounce.
  useEffect(() => {
    if (location.hash === "#tasks") navigate("/monitors", { replace: true });
  }, [location, navigate]);

  const rules = monitors.data ?? [];
  const enabledRules = rules.filter((m) => m.enabled);
  const failing = rules.filter((m) => m.last_error !== null);
  const fastest =
    enabledRules.length > 0
      ? Math.min(...enabledRules.map((m) => m.interval_seconds))
      : null;

  // Every count below comes from `/api/stats/overview` (server-side, no row
  // cap), which retired the old client-side derivations in lib/overview.ts
  // and their "N+" cap-honesty captions. Day boundaries are UTC calendar
  // days, same as the analytics endpoints.
  const daily = stats.data?.daily ?? [];
  const runsSpark = daily.map((d) => d.runs_total);
  const hitsSpark = daily.map((d) => d.new_hits);
  const pushesSpark = daily.map((d) => d.pushes);
  // A zero-run day has no success rate; it is omitted rather than invented.
  // The spark is decorative (aria-hidden, no axis), so gaps closing up is
  // acceptable where a fabricated 100% would not be.
  const rateSpark = daily
    .filter((d) => d.runs_total > 0)
    .map((d) => 1 - d.runs_failed / d.runs_total);

  const runs = stats.data?.runs_24h;
  const successRate =
    runs !== undefined && runs.total > 0 ? 1 - runs.failed / runs.total : null;
  const pushes = stats.data?.pushes_24h;

  return (
    <>
      <Ticker items={(recent.data ?? []).slice(0, 12)} />

      <PageHero
        eyebrow="SYSTEM OVERVIEW"
        ghost="SIGNAL"
        title="总览"
        meta={
          <>
            <span>
              <Icon name="timer" size={12} />
              {fastest !== null
                ? `最快每 ${fastest} 秒轮询`
                : "没有启用中的规则"}
            </span>
            {session.data ? (
              <span>
                <Icon name="satellite-dish" size={12} />
                {session.data.usable &&
                !session.data.needs_verification &&
                session.data.challenged_apis.length === 0 ? (
                  <span className="ok">会话正常</span>
                ) : (
                  <span className="warn">
                    {session.data.usable ? "会话被风控" : "会话不可用"}
                  </span>
                )}
              </span>
            ) : null}
            {stats.data ? (
              <span>
                <Icon name="database" size={12} />
                累计观测 {stats.data.items_total} 件
              </span>
            ) : null}
            {failing.length > 0 ? (
              <span>
                <Icon name="alert-triangle" size={12} />
                <span className="warn">{failing.length} 条规则采集报错</span>
              </span>
            ) : null}
          </>
        }
      />

      <section className="kpi-grid">
        {/* The rule COUNT is overview material; the rules themselves are not.
            The card is the link to where they live. */}
        <Link className="card kpi" to="/monitors" data-reveal ref={reveal}>
          <div className="kpi-top">
            <span className="kpi-label">
              <Icon name="scan-search" size={13} />
              监控任务
            </span>
            {monitors.isSuccess && rules.length > 0 ? (
              failing.length > 0 ? (
                <span className="pill" data-tone="warn">
                  有报错
                </span>
              ) : enabledRules.length > 0 ? (
                <span className="pill" data-tone="success">
                  运行中
                </span>
              ) : (
                // Every rule is stopped: "运行中" next to "0 启用" would be a
                // lie. Neutral pill — nothing is wrong, nothing is running.
                <span className="pill">全部停用</span>
              )
            ) : null}
          </div>
          <span className="kpi-value">
            {monitors.isPending ? (
              "…"
            ) : monitors.isError ? (
              "—"
            ) : (
              <CountUp value={enabledRules.length} />
            )}
            {monitors.isSuccess ? (
              <span className="unit">/ {rules.length} 启用</span>
            ) : null}
          </span>
          <span className="kpi-sub">
            {monitors.isError
              ? "拉取失败"
              : monitors.isSuccess
                ? `${rules.length - enabledRules.length} 条停用 · ${failing.length} 条报错`
                : " "}
          </span>
          {runsSpark.length > 1 ? (
            <div className="kpi-spark">
              <Spark values={runsSpark} />
            </div>
          ) : null}
        </Link>

        <div className="card kpi" data-reveal ref={reveal}>
          <div className="kpi-top">
            <span className="kpi-label">
              <Icon name="target" size={13} />
              今日新增命中
            </span>
          </div>
          <span className="kpi-value">
            {stats.isPending ? (
              "…"
            ) : stats.isError ? (
              "—"
            ) : (
              <CountUp value={stats.data.hits.today} />
            )}
          </span>
          <span className="kpi-sub">
            {stats.isError
              ? "拉取统计失败"
              : stats.isSuccess
                ? `昨日 ${stats.data.hits.yesterday} · 7 日均 ${Math.round(stats.data.hits.avg_7d * 10) / 10}`
                : " "}
          </span>
          {hitsSpark.length > 1 ? (
            <div className="kpi-spark">
              <Spark values={hitsSpark} color="var(--green)" />
            </div>
          ) : null}
        </div>

        <div className="card kpi" data-reveal ref={reveal}>
          <div className="kpi-top">
            <span className="kpi-label">
              <Icon name="gauge" size={13} />
              24H 采集成功率
            </span>
            {/* --warn is exactly for this: a degraded collector. */}
            {runs !== undefined && runs.failed > 0 ? (
              <span className="pill" data-tone="warn">
                有失败
              </span>
            ) : null}
          </div>
          <span className="kpi-value">
            {/* No CountUp: the rate is not an integer, and useCountUp rounds. */}
            {stats.isPending
              ? "…"
              : stats.isError || successRate === null
                ? "—"
                : `${(successRate * 100).toFixed(1)}`}
            {successRate !== null ? <span className="unit">%</span> : null}
          </span>
          <span className="kpi-sub">
            {stats.isError
              ? "拉取统计失败"
              : runs !== undefined
                ? runs.total === 0
                  ? "24H 内没有采集运行"
                  : `${runs.total} 次运行 · ${runs.failed} 次失败`
                : " "}
          </span>
          {rateSpark.length > 1 ? (
            <div className="kpi-spark">
              <Spark values={rateSpark} />
            </div>
          ) : null}
        </div>

        <div className="card kpi" data-reveal ref={reveal}>
          <div className="kpi-top">
            <span className="kpi-label">
              <Icon name="send" size={13} />
              24H 推送
            </span>
          </div>
          <span className="kpi-value">
            {stats.isPending ? (
              "…"
            ) : stats.isError ? (
              "—"
            ) : (
              <CountUp value={stats.data.pushes_24h.total} />
            )}
            {stats.isSuccess ? <span className="unit">次</span> : null}
          </span>
          <span className="kpi-sub">
            {stats.isError
              ? "拉取统计失败"
              : pushes !== undefined
                ? `邮件 ${pushes.email} · Telegram ${pushes.telegram} · ${
                    pushes.failed === 0 ? "全部成功" : `${pushes.failed} 次失败`
                  }`
                : " "}
          </span>
          {pushesSpark.length > 1 ? (
            <div className="kpi-spark">
              <Spark values={pushesSpark} />
            </div>
          ) : null}
        </div>
      </section>

      <div className="grid-21">
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-3)",
          }}
        >
          <MonitorTrend monitors={rules} />
          <WatchTrend />
        </div>

        <aside className="side-stack">
          <section className="card" data-reveal ref={reveal}>
            <div className="card-h">
              <h2>
                <Icon name="zap" size={15} />
                最近命中
              </h2>
              <Link className="btn-text" to="/items">
                全部
                <Icon name="arrow-right" size={13} />
              </Link>
            </div>
            {recent.isPending ? <Loading rows={3} /> : null}
            {recent.isError ? (
              <ErrorState
                title="拉取命中失败"
                error={recent.error}
                onRetry={() => void recent.refetch()}
              />
            ) : null}
            {recent.isSuccess && recent.data.length === 0 ? (
              <Empty message="还没有命中商品。规则跑起来之后，新命中会出现在这里。" />
            ) : null}
            {(recent.data ?? []).slice(0, 5).map((item) => (
              <div className="hit-item" key={item.id}>
                <RemoteImage
                  src={item.cover_url}
                  alt={item.title}
                  width={48}
                  height={48}
                />
                <div className="hit-body">
                  <div className="hit-title">
                    <Link to={`/items/${item.id}`} title={item.title}>
                      {item.title}
                    </Link>
                  </div>
                  <div className="hit-meta">
                    <span className="hit-price">
                      {formatPrice(item.price_cents)}
                    </span>
                    <span>{formatRelativeTime(item.first_seen_at)}</span>
                  </div>
                </div>
              </div>
            ))}
          </section>

          <section className="card" data-reveal ref={reveal}>
            <div className="card-h">
              <h2>
                <Icon name="bell" size={15} />
                最近推送
              </h2>
              <span className="note">NOTIFYLOG</span>
            </div>
            {logs.isPending ? <Loading rows={3} /> : null}
            {logs.isError ? (
              <ErrorState
                title="拉取推送记录失败"
                error={logs.error}
                onRetry={() => void logs.refetch()}
              />
            ) : null}
            {logs.isSuccess && logs.data.length === 0 ? (
              <Empty message="还没有推送过。命中要配了渠道才会推送出去。" />
            ) : null}
            {(logs.data ?? []).slice(0, 6).map((log) => (
              <div
                className="push-item"
                key={log.id}
                title={log.error ?? undefined}
              >
                {/* The word carries the failure; the dot only accelerates it. */}
                <span
                  className="dot"
                  style={{ color: log.ok ? "var(--green)" : "var(--red)" }}
                />
                <span className="push-text">
                  {log.ok ? "" : "失败 · "}
                  {log.kind} · {channelLabel(channels.data, log.channel_id)}
                </span>
                <span className="time">{formatRelativeTime(log.sent_at)}</span>
              </div>
            ))}
          </section>
        </aside>
      </div>
    </>
  );
}

function channelLabel(channels: Channel[] | undefined, id: number): string {
  return channels?.find((c) => c.id === id)?.label ?? `渠道 ${id}`;
}
