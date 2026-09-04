import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Empty, ErrorState, Loading } from "../components/States";
import {
  keys,
  monitorsOptions,
  runMonitor,
  type Monitor,
} from "../api/queries";
import { formatPrice, formatRelativeTime } from "../lib/format";

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

export default function MonitorsPage() {
  const queryClient = useQueryClient();
  const query = useQuery(monitorsOptions());
  const run = useMutation({
    mutationFn: runMonitor,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.monitors }),
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
  if (query.data.length === 0) {
    return <Empty message="还没有监控任务。" />;
  }

  return (
    <section
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
      }}
    >
      <h1>监控任务</h1>

      {run.isError ? (
        <ErrorState title="立即运行失败" error={run.error} />
      ) : null}
      {run.isSuccess && run.data ? (
        <p className="muted" style={{ fontSize: 12 }}>
          本轮采集 {run.data.collected} 条，通过筛选 {run.data.passed} 条，
          需通知 {run.data.notifiable} 条
          {run.data.collector ? `（走 ${run.data.collector}）` : ""}
          {run.data.baseline ? "，首轮仅建立基线" : ""}
          {run.data.error ? `，错误：${run.data.error}` : ""}
        </p>
      ) : null}

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
                <td className="num">{m.hit_count}</td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {formatRelativeTime(m.last_run_at)}
                </td>
                <td className="muted">{m.last_collector ?? "—"}</td>
                <td>
                  <Health m={m} />
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => run.mutate(m.id)}
                    disabled={run.isPending}
                  >
                    {run.isPending && run.variables === m.id
                      ? "运行中…"
                      : "立即运行"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
