import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import { ApiError } from "../api/client";
import {
  channelsOptions,
  createMonitor,
  deleteMonitor,
  itemsOptions,
  keys,
  monitorsOptions,
  notifyLogsOptions,
  runMonitor,
  sessionOptions,
  updateMonitor,
  type Channel,
  type ItemFilters,
  type Monitor,
  type MonitorCreate,
} from "../api/queries";
import { Icon } from "../components/Icon";
import { PageHero } from "../components/PageHero";
import RemoteImage from "../components/RemoteImage";
import Spark from "../components/Spark";
import { Empty, ErrorState, Loading } from "../components/States";
import { Ticker } from "../components/Ticker";
import {
  formatPrice,
  formatPriceRange,
  formatRelativeTime,
  parseUtc,
  parseYuanToCents,
} from "../lib/format";
import { useCountUp, useReveal } from "../lib/fx";
import { sellerLabel } from "../lib/itemFilters";
import { countSince, dailyCounts, daysAgoStart, windowCovers } from "../lib/overview";

/** Decorative count-up for a KPI number. A leaf on purpose: the rAF loop in
 *  useCountUp re-renders this one span ~40 times, not the whole page.
 */
function CountUp({ value }: { value: number }) {
  return <>{useCountUp(value)}</>;
}

/** Mirrors the backend floor. It is an anti-ban rule, not a UI hint: polling
 *  faster is how the upstream session gets challenged.
 */
const MIN_INTERVAL = 60;

const CONDITIONS = ["全新", "几乎全新", "轻微使用", "明显使用"] as const;

/** Both list endpoints cap their responses at 200 rows (`/api/items` and
 *  `/api/notify-logs`). The KPIs below are counted from these capped windows,
 *  so every count checks `windowCovers` and admits "N+" when the cap cut the
 *  period short -- an under-count shown as a count is the dashboard lying.
 */
const WINDOW_CAP = 200;

/** Module-level so the query key is one stable object, not a new identity
 *  per render.
 */
const RECENT_ITEMS: ItemFilters = { sort: "-first_seen", limit: WINDOW_CAP };

/** Dashboard cadence per hook-guidelines: 30s on overview queries, and no
 *  faster -- collection cadence is the real freshness limit.
 */
const POLL = 30_000;

/** What the rule watches. `keyword === null` IS the rule type -- see
 *  `models.Monitor`, where the xor CHECK is also the reason a `kind` column
 *  would be a second truth. Rendering `m.keyword` straight left this cell
 *  blank for every seller rule.
 */
function RuleTarget({ m }: { m: Monitor }) {
  if (m.keyword != null) return <>{m.keyword}</>;
  // Unreachable through the API (the xor is a CHECK constraint), but a blank
  // cell is what this component exists to stop, so it does not return one.
  if (m.seller_id == null) return <span className="muted">目标缺失</span>;
  return (
    <span style={{ display: "inline-flex", gap: "var(--space-1)", alignItems: "baseline" }}>
      <span className="pill" title="盯这个卖家的全部在售商品，不按关键词搜索">
        卖家
      </span>
      {/* URLSearchParams, not string concatenation: a seller id is base64 and
          `+` in a raw query string reads back as a space -- see itemFilters. */}
      <Link to={`/items?${new URLSearchParams({ seller_id: m.seller_id }).toString()}`}>
        {sellerLabel(m.seller_nick, m.seller_id)}
      </Link>
    </span>
  );
}

/** The health cell is the reason the tasks section exists. A rule that
 *  silently stopped collecting looks identical to one that found nothing,
 *  unless the error, the failure streak, and the baseline flag are all on
 *  screen.
 */
function Health({ m }: { m: Monitor }) {
  const streak = m.consecutive_failures > 1 ? ` ×${m.consecutive_failures}` : "";
  // Stopped-and-failing is the worst state and the easiest to misread: the
  // backend auto-disables a rule after repeated failures, so reporting only
  // "collecting failed" lets the user believe it is still retrying when it
  // will never run again until re-enabled.
  if (!m.enabled && m.last_error) {
    return (
      <span className="pill" data-tone="danger" title={m.last_error}>
        已停用 · 报错{streak}
      </span>
    );
  }
  if (m.last_error) {
    return (
      <span className="pill" data-tone="warn" title={m.last_error}>
        采集报错{streak}
      </span>
    );
  }
  if (!m.enabled) {
    return <span className="muted">已停用</span>;
  }
  if (!m.baseline_done) {
    return (
      <span
        className="pill"
        data-tone="warn"
        title="首轮只建立基线，不推送，避免把存量商品当成新命中"
      >
        建立基线
      </span>
    );
  }
  return (
    <span className="pill" data-tone="success">
      正常
    </span>
  );
}

const FIELD: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-1)",
};

function MonitorForm({
  onDone,
  sessionUsable,
  channels,
}: {
  onDone: () => void;
  sessionUsable: boolean;
  channels: Channel[];
}) {
  const queryClient = useQueryClient();
  const create = useMutation({
    mutationFn: createMonitor,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.monitors });
      onDone();
    },
  });

  const fieldError = (name: string): string | undefined =>
    create.error instanceof ApiError ? create.error.fields[name] : undefined;

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (key: string): string => String(form.get(key) ?? "").trim();
    const cents = (key: string): number | null => parseYuanToCents(text(key));
    const int = (key: string): number | null => {
      const raw = text(key);
      if (!raw) return null;
      const value = Number(raw);
      return Number.isFinite(value) ? Math.trunc(value) : null;
    };
    // Tri-state selects: "" means "do not filter on this at all", which is
    // different from false. Sending false would exclude every listing whose
    // flag could not be determined.
    const tri = (key: string): boolean | null => {
      const raw = text(key);
      return raw === "" ? null : raw === "true";
    };

    const payload: MonitorCreate = {
      name: text("name"),
      keyword: text("keyword"),
      exclude_words: text("exclude_words"),
      price_min_cents: cents("price_min"),
      price_max_cents: cents("price_max"),
      published_within_hours: int("published_within_hours"),
      region: text("region") || null,
      condition: text("condition") || null,
      free_shipping: tri("free_shipping"),
      min_seller_credit: int("min_seller_credit"),
      exclude_shop: text("exclude_shop") === "on",
      interval_seconds: int("interval_seconds") ?? 300,
      // Without at least one channel a hit goes nowhere: the panel becomes the
      // only place it exists, and real-time push -- the entire point -- never
      // happens. Found in T8, where every rule the panel created had none.
      channel_ids: form.getAll("channel_ids").map((value) => Number(value)),
    };
    create.mutate(payload);
  }

  return (
    <form
      onSubmit={submit}
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
        gap: "var(--space-3)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "var(--space-4)",
        marginBottom: "var(--space-3)",
      }}
    >
      <label style={FIELD}>
        名称
        <input
          name="name"
          required
          maxLength={100}
          aria-describedby={fieldError("name") ? "err-name" : undefined}
          aria-invalid={fieldError("name") ? true : undefined}
        />
        {fieldError("name") ? (
          <span id="err-name" role="alert" style={{ color: "var(--danger)", fontSize: 11.5 }}>
            {fieldError("name")}
          </span>
        ) : null}
      </label>

      <label style={FIELD}>
        关键词
        <input name="keyword" required maxLength={100} placeholder="iPhone 13 128G" />
        {fieldError("keyword") ? (
          <span role="alert" style={{ color: "var(--danger)", fontSize: 11.5 }}>
            {fieldError("keyword")}
          </span>
        ) : null}
        {/* This form only builds keyword rules. Without saying where the other
            kind comes from, a user reads "keyword required" as "watching a
            seller is not possible". */}
        <span className="muted" style={{ fontSize: 11 }}>
          想盯某个卖家的全部在售商品？去<Link to="/items">命中商品</Link>
          页筛出那个卖家，从那里创建——卖家规则不在这个表单里建。
        </span>
      </label>

      <label style={FIELD}>
        排除词（空格分隔）
        <input name="exclude_words" placeholder="碎屏 主板 拆机" />
      </label>

      <label style={FIELD}>
        最低价（元）
        <input name="price_min" type="number" min={0} />
      </label>

      <label style={FIELD}>
        最高价（元）
        <input name="price_max" type="number" min={0} />
        {fieldError("price_max_cents") ? (
          <span role="alert" style={{ color: "var(--danger)", fontSize: 11.5 }}>
            {fieldError("price_max_cents")}
          </span>
        ) : null}
      </label>

      <label style={FIELD}>
        发布时间窗（小时）
        <input name="published_within_hours" type="number" min={1} placeholder="不限" />
      </label>

      <label style={FIELD}>
        地区
        <input name="region" placeholder="不限" />
      </label>

      <label style={FIELD}>
        成色
        <select name="condition" defaultValue="">
          <option value="">不限</option>
          {CONDITIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>

      <label style={FIELD}>
        包邮
        <select name="free_shipping" defaultValue="">
          <option value="">不限</option>
          <option value="true">只要包邮</option>
          <option value="false">只要不包邮</option>
        </select>
        <span className="muted" style={{ fontSize: 11 }}>
          「不限」与「只要包邮」不同：包邮是从描述推测的，不限才不会漏掉说不清的
        </span>
      </label>

      <label style={FIELD}>
        卖家信用下限
        <input
          name="min_seller_credit"
          type="number"
          min={0}
          disabled={!sessionUsable}
          placeholder={sessionUsable ? "不限" : "需要先导入凭证"}
        />
        {!sessionUsable ? (
          <span className="muted" style={{ fontSize: 11 }}>
            没有可用会话就抓不到卖家画像，这条规则会被保守放行并标注。先去
            <Link to="/settings">设置</Link>导入凭证。
          </span>
        ) : null}
      </label>

      <label style={FIELD}>
        采集间隔（秒）
        <input
          name="interval_seconds"
          type="number"
          min={MIN_INTERVAL}
          defaultValue={300}
          aria-describedby="hint-interval"
          aria-invalid={fieldError("interval_seconds") ? true : undefined}
        />
        <span id="hint-interval" className="muted" style={{ fontSize: 11 }}>
          下限 {MIN_INTERVAL} 秒，是防封规则不是装饰
        </span>
        {fieldError("interval_seconds") ? (
          <span role="alert" style={{ color: "var(--danger)", fontSize: 11.5 }}>
            {fieldError("interval_seconds")}
          </span>
        ) : null}
      </label>

      <label
        style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", alignSelf: "end" }}
      >
        <input name="exclude_shop" type="checkbox" />
        排除鱼小铺（商家）
      </label>

      <fieldset style={{ gridColumn: "1 / -1" }}>
        <legend>推送到哪些渠道</legend>
        {channels.length === 0 ? (
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            还没有通知渠道。命中只会留在这个页面里，不会推送给你——先去
            <Link to="/settings#channels">通知渠道</Link>建一个。
          </p>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-3)" }}>
            {channels.map((ch) => (
              <label
                key={ch.id}
                style={{ display: "flex", gap: "var(--space-1)", alignItems: "center" }}
              >
                <input
                  type="checkbox"
                  name="channel_ids"
                  value={ch.id}
                  defaultChecked={ch.enabled}
                />
                {ch.label}
                <span className="muted" style={{ fontSize: 11 }}>
                  （{ch.kind === "email" ? "邮件" : "Telegram"}
                  {ch.enabled ? "" : " · 已停用"}）
                </span>
              </label>
            ))}
          </div>
        )}
      </fieldset>

      <div
        style={{
          gridColumn: "1 / -1",
          display: "flex",
          gap: "var(--space-2)",
          alignItems: "center",
        }}
      >
        <button type="submit" data-variant="primary" disabled={create.isPending}>
          {create.isPending ? "创建中…" : "创建"}
        </button>
        <button type="button" onClick={onDone}>
          取消
        </button>
      </div>

      {create.isError && !(create.error instanceof ApiError && create.error.status === 422) ? (
        <div style={{ gridColumn: "1 / -1" }}>
          <ErrorState title="创建失败" error={create.error} />
        </div>
      ) : null}
    </form>
  );
}

/** The monitor CRUD from the former MonitorsPage, folded into the overview as
 *  the `#tasks` card (the standalone /monitors route redirects here).
 */
function TasksSection({
  monitors,
  sessionUsable,
  channels,
}: {
  monitors: UseQueryResult<Monitor[], Error>;
  sessionUsable: boolean;
  channels: Channel[];
}) {
  const queryClient = useQueryClient();
  const reveal = useReveal();
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.monitors });
  const run = useMutation({ mutationFn: runMonitor, onSuccess: invalidate });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateMonitor(id, { enabled }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: deleteMonitor,
    onSuccess: () => {
      setConfirming(null);
      void invalidate();
    },
  });

  const rules = monitors.data ?? [];
  const active = rules.filter((m) => m.enabled).length;

  return (
    // scroll-margin keeps the sticky topbar from covering the anchor target.
    <section className="card" id="tasks" style={{ scrollMarginTop: 72 }} data-reveal ref={reveal}>
      <div className="card-h">
        <h2>
          <Icon name="activity" size={15} />
          监控任务健康
        </h2>
        <div className="actions">
          {monitors.isSuccess ? (
            <span className="note">
              {rules.length} RULES · {active} ACTIVE
            </span>
          ) : null}
          {!creating ? (
            <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
              新建监控
            </button>
          ) : null}
        </div>
      </div>

      {creating ? (
        <MonitorForm
          onDone={() => setCreating(false)}
          sessionUsable={sessionUsable}
          channels={channels}
        />
      ) : null}

      {run.isError ? <ErrorState title="立即运行失败" error={run.error} /> : null}
      {remove.isError ? <ErrorState title="删除失败" error={remove.error} /> : null}
      {run.isSuccess && run.data ? (
        <p className="muted" style={{ fontSize: 12 }}>
          本轮采集 {run.data.collected} 条，通过筛选 {run.data.passed} 条，需通知{" "}
          {run.data.notifiable} 条
          {run.data.collector ? `（走 ${run.data.collector}）` : ""}
          {run.data.baseline ? "，首轮仅建立基线" : ""}
          {run.data.error ? `，错误：${run.data.error}` : ""}
        </p>
      ) : null}

      {monitors.isPending ? <Loading rows={4} /> : null}
      {monitors.isError ? (
        <ErrorState
          title="拉取监控任务失败"
          error={monitors.error}
          onRetry={() => void monitors.refetch()}
        />
      ) : null}

      {monitors.isSuccess && rules.length === 0 ? (
        <Empty
          message="还没有监控任务。"
          action={
            <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
              新建监控
            </button>
          }
        />
      ) : null}

      {rules.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>关键词 / 卖家</th>
                <th style={{ textAlign: "right" }}>价格区间</th>
                <th style={{ textAlign: "right" }}>间隔</th>
                <th style={{ textAlign: "right" }}>命中</th>
                <th>上次运行</th>
                <th>路径</th>
                <th>状态</th>
                <th>推送</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rules.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}</td>
                  <td>
                    <RuleTarget m={m} />
                  </td>
                  <td className="num">{formatPriceRange(m.price_min_cents, m.price_max_cents)}</td>
                  <td className="num">{m.interval_seconds}s</td>
                  <td className="num">
                    <Link to={`/items?monitor_id=${m.id}`}>{m.hit_count}</Link>
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {formatRelativeTime(m.last_run_at)}
                  </td>
                  <td className="muted">{m.last_collector ?? "—"}</td>
                  <td>
                    <Health m={m} />
                  </td>
                  <td>
                    {/* A rule with no channel collects and then tells nobody.
                        That is worth a warning, not a blank cell. */}
                    {m.channel_ids.length === 0 ? (
                      <span
                        className="pill"
                        data-tone="warn"
                        title="命中不会推送给任何人，只会留在命中列表里"
                      >
                        无渠道
                      </span>
                    ) : (
                      <span className="muted">{m.channel_ids.length} 个</span>
                    )}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: "var(--space-1)" }}>
                      <button
                        type="button"
                        onClick={() => run.mutate(m.id)}
                        disabled={run.isPending}
                      >
                        {run.isPending && run.variables === m.id ? "运行中…" : "立即运行"}
                      </button>
                      <button
                        type="button"
                        onClick={() => toggle.mutate({ id: m.id, enabled: !m.enabled })}
                        disabled={toggle.isPending}
                      >
                        {m.enabled ? "停用" : "启用"}
                      </button>
                      {/* Inline confirmation rather than confirm(): a native
                          dialog blocks the event loop, which would also make
                          the panel unresponsive to any automated check. */}
                      {confirming === m.id ? (
                        <>
                          <button
                            type="button"
                            data-variant="danger"
                            onClick={() => remove.mutate(m.id)}
                            disabled={remove.isPending}
                          >
                            确认删除
                          </button>
                          <button type="button" onClick={() => setConfirming(null)}>
                            取消
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          data-variant="danger"
                          onClick={() => setConfirming(m.id)}
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

export default function OverviewPage() {
  const location = useLocation();
  const reveal = useReveal();
  const monitors = useQuery({ ...monitorsOptions(), refetchInterval: POLL });
  const recent = useQuery({ ...itemsOptions(RECENT_ITEMS), refetchInterval: POLL });
  const logs = useQuery({ ...notifyLogsOptions(WINDOW_CAP), refetchInterval: POLL });
  const session = useQuery(sessionOptions());
  const channels = useQuery(channelsOptions());

  // /#tasks must land on the tasks card, on load and on every nav click.
  // location identity changes per navigation, so clicking 监控任务 while
  // already at /#tasks still scrolls.
  useEffect(() => {
    if (!location.hash) return;
    document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [location]);

  const rules = monitors.data ?? [];
  const enabledRules = rules.filter((m) => m.enabled);
  const failing = rules.filter((m) => m.last_error !== null);
  const fastest =
    enabledRules.length > 0 ? Math.min(...enabledRules.map((m) => m.interval_seconds)) : null;

  // "Hits" on this dashboard are captured items -- the same fact ItemsPage
  // lists. Counts come from the capped recent-items window; see WINDOW_CAP.
  const firstSeens = (recent.data ?? []).map((i) => i.first_seen_at);
  const todayStart = daysAgoStart(1);
  const todayCount = countSince(firstSeens, todayStart);
  const todayExact = windowCovers(firstSeens, todayStart, WINDOW_CAP);
  const hitsCover7 = recent.isSuccess && windowCovers(firstSeens, daysAgoStart(7), WINDOW_CAP);
  const hitsDaily = hitsCover7 ? dailyCounts(firstSeens, 7) : null;

  const sentAts = (logs.data ?? []).map((l) => l.sent_at);
  // Recomputed each render on purpose: the 30s poll re-renders the page, and
  // "the last 24 hours" is supposed to slide with it.
  const dayAgo = new Date(new Date().getTime() - 86_400_000);
  const pushes24 = (logs.data ?? []).filter(
    (l) => parseUtc(l.sent_at).getTime() >= dayAgo.getTime(),
  );
  const pushesExact = windowCovers(sentAts, dayAgo, WINDOW_CAP);
  const pushesOk = pushes24.filter((l) => l.ok).length;
  const pushesFailed = pushes24.length - pushesOk;
  const pushesCover7 = logs.isSuccess && windowCovers(sentAts, daysAgoStart(7), WINDOW_CAP);
  const pushesDaily = pushesCover7 ? dailyCounts(sentAts, 7) : null;

  return (
    <>
      <Ticker items={(recent.data ?? []).slice(0, 12)} />

      <PageHero
        eyebrow="SYSTEM OVERVIEW"
        ghost="SIGNAL"
        title={
          <>
            总览<span className="thin"> / 信号台</span>
          </>
        }
        meta={
          <>
            <span>
              <Icon name="timer" size={12} />
              {fastest !== null ? `最快每 ${fastest} 秒轮询` : "没有启用中的规则"}
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
        <div className="card kpi" data-reveal ref={reveal}>
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
              ) : (
                <span className="pill" data-tone="success">
                  运行中
                </span>
              )
            ) : null}
          </div>
          <span className="kpi-value">
            {monitors.isPending ? "…" : monitors.isError ? "—" : <CountUp value={enabledRules.length} />}
            {monitors.isSuccess ? <span className="unit">/ {rules.length} 启用</span> : null}
          </span>
          <span className="kpi-sub">
            {monitors.isError
              ? "拉取失败，详见下方任务表"
              : monitors.isSuccess
                ? `${rules.length - enabledRules.length} 条停用 · ${failing.length} 条报错`
                : " "}
          </span>
          {/* ponytail: no sparkline here -- no endpoint records rule counts
              over time, and a spark drawn from nothing would be decoration
              pretending to be history. */}
        </div>

        <div className="card kpi" data-reveal ref={reveal}>
          <div className="kpi-top">
            <span className="kpi-label">
              <Icon name="target" size={13} />
              今日新增命中
            </span>
          </div>
          <span className="kpi-value">
            {recent.isPending ? (
              "…"
            ) : recent.isError ? (
              "—"
            ) : (
              <>
                <CountUp value={todayCount} />
                {todayExact ? "" : "+"}
              </>
            )}
          </span>
          <span className="kpi-sub">
            {recent.isError
              ? "拉取失败，详见最近命中"
              : hitsDaily
                ? `昨日 ${hitsDaily[5] ?? 0} · 7 日均 ${Math.round(hitsDaily.reduce((a, b) => a + b, 0) / 7)}`
                : recent.isSuccess
                  ? `窗口只有最近 ${WINDOW_CAP} 条，7 日统计不完整`
                  : " "}
          </span>
          {hitsDaily ? (
            <div className="kpi-spark">
              <Spark values={hitsDaily} color="var(--green)" />
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
            {logs.isPending ? (
              "…"
            ) : logs.isError ? (
              "—"
            ) : (
              <>
                <CountUp value={pushes24.length} />
                {pushesExact ? "" : "+"}
              </>
            )}
            {logs.isSuccess ? <span className="unit">次</span> : null}
          </span>
          <span className="kpi-sub">
            {logs.isError
              ? "拉取失败，详见最近推送"
              : logs.isSuccess
                ? `成功 ${pushesOk} · 失败 ${pushesFailed}`
                : " "}
          </span>
          {pushesDaily ? (
            <div className="kpi-spark">
              <Spark values={pushesDaily} />
            </div>
          ) : null}
        </div>
      </section>

      <div className="grid-21">
        <TasksSection
          monitors={monitors}
          sessionUsable={session.data?.usable ?? false}
          channels={channels.data ?? []}
        />

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
                <RemoteImage src={item.cover_url} alt={item.title} width={48} height={48} />
                <div className="hit-body">
                  <div className="hit-title">
                    <Link to={`/items/${item.id}`} title={item.title}>
                      {item.title}
                    </Link>
                  </div>
                  <div className="hit-meta">
                    <span className="hit-price">{formatPrice(item.price_cents)}</span>
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
              <div className="push-item" key={log.id} title={log.error ?? undefined}>
                {/* The word carries the failure; the dot only accelerates it. */}
                <span
                  className="dot"
                  style={{ color: log.ok ? "var(--green)" : "var(--red)" }}
                />
                <span>
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
