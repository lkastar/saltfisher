import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import {
  createMonitor,
  deleteMonitor,
  keys,
  monitorsOptions,
  runMonitor,
  sessionOptions,
  updateMonitor,
  type Monitor,
  type MonitorCreate,
} from "../api/queries";
import { Empty, ErrorState, Loading } from "../components/States";
import { formatPrice, formatRelativeTime, parseYuanToCents } from "../lib/format";

/** Mirrors the backend floor. It is an anti-ban rule, not a UI hint: polling
 *  faster is how the upstream session gets challenged.
 */
const MIN_INTERVAL = 60;

const CONDITIONS = ["全新", "几乎全新", "轻微使用", "明显使用"] as const;

function priceRange(m: Monitor): string {
  const lo = m.price_min_cents;
  const hi = m.price_max_cents;
  if (lo == null && hi == null) return "不限";
  if (lo == null) return `≤ ${formatPrice(hi)}`;
  if (hi == null) return `≥ ${formatPrice(lo)}`;
  return `${formatPrice(lo)} – ${formatPrice(hi)}`;
}

/** The health cell is the reason this page exists. A rule that silently
 *  stopped collecting looks identical to one that found nothing, unless the
 *  error, the failure streak, and the baseline flag are all on screen.
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
}: {
  onDone: () => void;
  sessionUsable: boolean;
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
      channel_ids: [],
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

export default function MonitorsPage() {
  const queryClient = useQueryClient();
  const query = useQuery(monitorsOptions());
  const session = useQuery(sessionOptions());
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

  if (query.isPending) return <Loading rows={4} />;
  if (query.isError) {
    return (
      <ErrorState
        title="拉取监控任务失败"
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <header style={{ display: "flex", gap: "var(--space-3)", alignItems: "baseline" }}>
        <h1>监控任务</h1>
        {!creating ? (
          <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
            新建监控
          </button>
        ) : null}
      </header>

      {creating ? (
        <MonitorForm
          onDone={() => setCreating(false)}
          sessionUsable={session.data?.usable ?? false}
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

      {query.data.length === 0 ? (
        <Empty
          message="还没有监控任务。"
          action={
            <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
              新建监控
            </button>
          }
        />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>关键词</th>
                <th style={{ textAlign: "right" }}>价格区间</th>
                <th style={{ textAlign: "right" }}>间隔</th>
                <th style={{ textAlign: "right" }}>命中</th>
                <th>上次运行</th>
                <th>路径</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {query.data.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}</td>
                  <td>{m.keyword}</td>
                  <td className="num">{priceRange(m)}</td>
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
      )}
    </section>
  );
}
