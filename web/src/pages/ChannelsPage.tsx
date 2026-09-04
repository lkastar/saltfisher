import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../api/client";
import {
  channelsOptions,
  createChannel,
  deleteChannel,
  keys,
  notifyLogsOptions,
  testChannel,
  updateChannel,
  type Channel,
  type ChannelCreate,
} from "../api/queries";
import { Empty, ErrorState, Loading } from "../components/States";
import { formatDateTime, formatRelativeTime } from "../lib/format";

const FIELD: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-1)",
};

/* The backend redacts secrets before they leave the process; this page adds a
 * second layer by never putting one in the DOM at all -- no `value=` on a
 * secret input, and the card shows only whether one exists. Verification greps
 * the rendered page, so "the backend redacts it" is not enough on its own. */

function isSet(config: Record<string, unknown>, key: string): boolean {
  const value = config[key];
  return value !== undefined && value !== null && value !== "";
}

function ChannelForm({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<ChannelCreate["kind"]>("email");
  const create = useMutation({
    mutationFn: createChannel,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.channels });
      onDone();
    },
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (key: string) => String(form.get(key) ?? "").trim();

    const config: Record<string, unknown> =
      kind === "email"
        ? {
            smtp_host: text("smtp_host"),
            smtp_port: Number(text("smtp_port") || 587),
            username: text("username"),
            password: text("password"),
            from_addr: text("from_addr"),
            to_addrs: text("to_addrs")
              .split(/[,\s;]+/)
              .filter(Boolean),
            use_ssl: text("use_ssl") === "on",
            use_starttls: text("use_starttls") === "on",
          }
        : { bot_token: text("bot_token"), chat_id: text("chat_id") };

    create.mutate({ kind, label: text("label"), config, enabled: true });
  }

  const fieldError = (name: string): string | undefined =>
    create.error instanceof ApiError ? create.error.fields[name] : undefined;

  return (
    <form
      onSubmit={submit}
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
        gap: "var(--space-3)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "var(--space-4)",
      }}
    >
      <label style={FIELD}>
        类型
        <select value={kind} onChange={(e) => setKind(e.target.value as ChannelCreate["kind"])}>
          <option value="email">邮件</option>
          <option value="telegram">Telegram</option>
        </select>
      </label>

      <label style={FIELD}>
        名称
        <input name="label" required maxLength={60} placeholder="给自己看的备注" />
      </label>

      {kind === "email" ? (
        <>
          <label style={FIELD}>
            SMTP 主机
            <input name="smtp_host" required placeholder="smtp-relay.brevo.com" />
          </label>
          <label style={FIELD}>
            端口
            <input name="smtp_port" type="number" min={1} max={65535} defaultValue={587} />
          </label>
          <label style={FIELD}>
            用户名
            <input name="username" required />
          </label>
          <label style={FIELD}>
            密码 / SMTP key
            <input name="password" type="password" autoComplete="new-password" />
            <span className="muted" style={{ fontSize: 11 }}>
              多数服务商的 SMTP key 与 API key 不是一个东西，填错会得到 535
            </span>
          </label>
          <label style={FIELD}>
            发件地址
            <input name="from_addr" required placeholder="bot@example.com" />
            <span className="muted" style={{ fontSize: 11 }}>
              域名需通过发信认证，否则会被静默丢弃
            </span>
          </label>
          <label style={FIELD}>
            收件地址（逗号分隔）
            <input name="to_addrs" required />
          </label>
          <label style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
            <input name="use_starttls" type="checkbox" defaultChecked />
            STARTTLS（587 端口）
          </label>
          <label style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
            <input name="use_ssl" type="checkbox" />
            直接 SSL（465 端口）
          </label>
        </>
      ) : (
        <>
          <label style={FIELD}>
            Bot Token
            <input name="bot_token" type="password" autoComplete="new-password" required />
          </label>
          <label style={FIELD}>
            Chat ID
            <input name="chat_id" required />
          </label>
        </>
      )}

      <div style={{ gridColumn: "1 / -1", display: "flex", gap: "var(--space-2)" }}>
        <button type="submit" data-variant="primary" disabled={create.isPending}>
          {create.isPending ? "创建中…" : "创建渠道"}
        </button>
        <button type="button" onClick={onDone}>
          取消
        </button>
      </div>

      {create.isError ? (
        <div style={{ gridColumn: "1 / -1" }}>
          <ErrorState
            title="创建失败"
            error={fieldError("config") ?? create.error}
          />
        </div>
      ) : null}
    </form>
  );
}

function ChannelCard({ channel }: { channel: Channel }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.channels });

  const test = useMutation({
    mutationFn: () => testChannel(channel.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.notifyLogs }),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => updateChannel(channel.id, { enabled }),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: () => deleteChannel(channel.id), onSuccess: invalidate });

  const config = channel.config as Record<string, unknown>;
  const secretKey = channel.kind === "email" ? "password" : "bot_token";

  return (
    <article
      style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--surface)",
        padding: "var(--space-3)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
      }}
    >
      <header style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
        <strong>{channel.label}</strong>
        <span className="pill" data-tone={channel.enabled ? "success" : "warn"}>
          {channel.kind === "email" ? "邮件" : "Telegram"}
          {channel.enabled ? "" : " · 已停用"}
        </span>
        <span className="muted" style={{ fontSize: 11, marginLeft: "auto" }}>
          建于 {formatDateTime(channel.created_at)}
        </span>
      </header>

      <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 12px", fontSize: 12.5 }}>
        {Object.entries(config)
          .filter(([key]) => key !== secretKey)
          .map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt className="muted">{key}</dt>
              <dd style={{ margin: 0, wordBreak: "break-all" }}>
                {Array.isArray(value) ? value.join("、") : String(value)}
              </dd>
            </div>
          ))}
        <div style={{ display: "contents" }}>
          <dt className="muted">{secretKey}</dt>
          {/* Never the value, only whether one exists. */}
          <dd style={{ margin: 0 }}>{isSet(config, secretKey) ? "已设置" : "未设置"}</dd>
        </div>
      </dl>

      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <button type="button" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? "发送中…" : "测试发送"}
        </button>
        <button
          type="button"
          onClick={() => toggle.mutate(!channel.enabled)}
          disabled={toggle.isPending}
        >
          {channel.enabled ? "停用" : "启用"}
        </button>
        {confirming ? (
          <>
            <button
              type="button"
              data-variant="danger"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              确认删除
            </button>
            <button type="button" onClick={() => setConfirming(false)}>
              取消
            </button>
          </>
        ) : (
          <button type="button" data-variant="danger" onClick={() => setConfirming(true)}>
            删除
          </button>
        )}
      </div>

      {test.data ? (
        test.data.ok ? (
          <p style={{ margin: 0, fontSize: 12.5, color: "var(--success)" }}>
            已交给服务器。<span className="muted">
              注意：SMTP 回 250 只代表对方接收了，不代表送进了收件箱——去邮箱确认一次。
            </span>
          </p>
        ) : (
          <ErrorState title="测试发送失败" error={test.data.error ?? "未知错误"} />
        )
      ) : null}
      {test.isError ? <ErrorState title="测试发送失败" error={test.error} /> : null}
      {remove.isError ? <ErrorState title="删除失败" error={remove.error} /> : null}
    </article>
  );
}

export default function ChannelsPage() {
  const channels = useQuery(channelsOptions());
  const logs = useQuery(notifyLogsOptions());
  const [creating, setCreating] = useState(false);

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <header style={{ display: "flex", gap: "var(--space-3)", alignItems: "baseline" }}>
        <h1>通知渠道</h1>
        {!creating ? (
          <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
            新建渠道
          </button>
        ) : null}
      </header>

      {creating ? <ChannelForm onDone={() => setCreating(false)} /> : null}

      {channels.isPending ? <Loading rows={3} /> : null}
      {channels.isError ? (
        <ErrorState
          title="拉取渠道失败"
          error={channels.error}
          onRetry={() => void channels.refetch()}
        />
      ) : null}
      {channels.data?.length === 0 ? (
        <Empty message="还没有通知渠道。命中和降价都是通过渠道推送出去的，没有渠道就只能来这里看。" />
      ) : null}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
          gap: "var(--space-3)",
        }}
      >
        {(channels.data ?? []).map((channel) => (
          <ChannelCard key={channel.id} channel={channel} />
        ))}
      </div>

      <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        <h2>发送记录</h2>
        {logs.isPending ? <Loading rows={2} /> : null}
        {logs.isError ? <ErrorState title="拉取发送记录失败" error={logs.error} /> : null}
        {logs.data?.length === 0 ? (
          <p className="muted" style={{ fontSize: 13 }}>
            还没有发送过。
          </p>
        ) : null}
        {logs.data && logs.data.length > 0 ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>渠道</th>
                  <th style={{ textAlign: "right" }}>商品数</th>
                  <th>结果</th>
                  <th>错误</th>
                </tr>
              </thead>
              <tbody>
                {logs.data.map((log) => (
                  <tr key={log.id}>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {formatRelativeTime(log.sent_at)}
                    </td>
                    <td>{log.kind}</td>
                    <td className="num">{log.item_count}</td>
                    <td>
                      <span className="pill" data-tone={log.ok ? "success" : "danger"}>
                        {log.ok ? "成功" : "失败"}
                      </span>
                    </td>
                    <td className="muted" style={{ wordBreak: "break-word", maxWidth: 320 }}>
                      {log.error ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <p className="muted" style={{ fontSize: 11.5 }}>
        密钥永不回显：页面上只显示「已设置 / 未设置」，要更换就重建渠道。
      </p>
    </section>
  );
}
