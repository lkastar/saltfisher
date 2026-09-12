import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import { ApiError, clearToken } from "../api/client";
import {
  channelsOptions,
  clearCookies,
  createChannel,
  createLlmEndpoint,
  deleteChannel,
  deleteLlmEndpoint,
  importCookies,
  keys,
  llmDefaultPromptOptions,
  llmEndpointsOptions,
  llmModelsOptions,
  llmScenarioOptions,
  mintImportTicket,
  notifyLogsOptions,
  saveLlmScenario,
  sessionOptions,
  testChannel,
  testLlmEndpoint,
  updateChannel,
  updateLlmEndpoint,
  type Channel,
  type ChannelCreate,
  type LlmEndpoint,
  type LlmEndpointUpdate,
  type LlmScenarioConfig,
  type Scenario,
} from "../api/queries";
import { ConfirmInline } from "../components/ConfirmInline";
import { Icon, type IconName } from "../components/Icon";
import { PageHero } from "../components/PageHero";
import { Empty, ErrorState, Loading } from "../components/States";
import { buildBookmarklet } from "../lib/bookmarklet";
import { formatDateTime, formatRelativeTime } from "../lib/format";
import { scenarioReady } from "../lib/llm";

/* One settings page, five anchored sections (design.md: Settings + Channels
 * merge). The anchor-nav mirrors the prototype's scroll-spy; /channels
 * redirects to /settings#channels and the hash-scroll effect lands on the
 * section, same approach as the monitors page's #tasks. */

const SECTIONS: { id: string; label: string; icon: IconName }[] = [
  { id: "session", label: "采集会话", icon: "cookie" },
  { id: "channels", label: "通知渠道", icon: "send" },
  { id: "llm", label: "LLM 端点", icon: "cpu" },
  { id: "ai", label: "AI 场景", icon: "sparkles" },
  { id: "access", label: "面板访问", icon: "key" },
];

/** Which section the viewport is on, for the anchor-nav active state. The
 *  observed elements are the five stable section cards (loading/error states
 *  render INSIDE them, so every id exists at mount). Band matches the
 *  prototype's scroll-spy. */
function useAnchorSpy(): string {
  const [active, setActive] = useState("session");
  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) setActive(entry.target.id);
        }
      },
      { rootMargin: "-30% 0px -60% 0px" },
    );
    for (const { id } of SECTIONS) {
      const el = document.getElementById(id);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
  }, []);
  return active;
}

/** Section cards stack their children with flex gap. */
const COL: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-3)",
};

/* ============================== 通知渠道 ==============================
 * Folded in from the former ChannelsPage (step 7). The backend redacts
 * secrets before they leave the process; this page adds a second layer by
 * never putting one in the DOM at all -- no `value=` on a secret input, and
 * the card shows only whether one exists. Verification greps the rendered
 * page, so "the backend redacts it" is not enough on its own. */

function isSet(config: Record<string, unknown>, key: string): boolean {
  const value = config[key];
  return value !== undefined && value !== null && value !== "";
}

function SecretState({ configured }: { configured: boolean }) {
  // Never the value, only whether one exists.
  return configured ? (
    <span className="secret-state">
      <Icon name="lock" size={11} />
      已设置（永不回显）
    </span>
  ) : (
    <span className="dim">未设置</span>
  );
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
      className="inner-card"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
        gap: "14px 18px",
      }}
    >
      <label className="field">
        <span className="field-label">类型</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as ChannelCreate["kind"])}
        >
          <option value="email">邮件</option>
          <option value="telegram">Telegram</option>
        </select>
      </label>

      <label className="field">
        <span className="field-label">名称</span>
        <input
          name="label"
          required
          maxLength={60}
          placeholder="给自己看的备注"
        />
      </label>

      {kind === "email" ? (
        <>
          <label className="field">
            <span className="field-label">SMTP 主机</span>
            <input
              name="smtp_host"
              required
              placeholder="smtp-relay.brevo.com"
            />
          </label>
          <label className="field">
            <span className="field-label">端口</span>
            <input
              name="smtp_port"
              type="number"
              min={1}
              max={65535}
              defaultValue={587}
            />
          </label>
          <label className="field">
            <span className="field-label">用户名</span>
            <input name="username" required />
          </label>
          <label className="field">
            <span className="field-label">密码 / SMTP key</span>
            <input
              name="password"
              type="password"
              autoComplete="new-password"
            />
            <span className="field-hint">
              多数服务商的 SMTP key 与 API key 不是一个东西，填错会得到 535
            </span>
          </label>
          <label className="field">
            <span className="field-label">发件地址</span>
            <input name="from_addr" required placeholder="bot@example.com" />
            <span className="field-hint">
              域名需通过发信认证，否则会被静默丢弃
            </span>
          </label>
          <label className="field">
            <span className="field-label">收件地址（逗号分隔）</span>
            <input name="to_addrs" required />
          </label>
          <label className="switch-row">
            <input
              name="use_starttls"
              type="checkbox"
              data-switch
              defaultChecked
            />
            STARTTLS（587 端口）
          </label>
          <label className="switch-row">
            <input name="use_ssl" type="checkbox" data-switch />
            直接 SSL（465 端口）
          </label>
        </>
      ) : (
        <>
          <label className="field">
            <span className="field-label">Bot Token</span>
            <input
              name="bot_token"
              type="password"
              autoComplete="new-password"
              required
            />
          </label>
          <label className="field">
            <span className="field-label">Chat ID</span>
            <input name="chat_id" required />
          </label>
        </>
      )}

      <div className="form-actions span-all">
        <button
          type="submit"
          data-variant="primary"
          disabled={create.isPending}
        >
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
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: keys.channels });

  const test = useMutation({
    mutationFn: () => testChannel(channel.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: keys.notifyLogs }),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => updateChannel(channel.id, { enabled }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => deleteChannel(channel.id),
    onSuccess: invalidate,
  });

  const config = channel.config as Record<string, unknown>;
  const secretKey = channel.kind === "email" ? "password" : "bot_token";

  return (
    <article className="inner-card">
      <header className="inner-h">
        <span className="name">{channel.label}</span>
        {/* Kind tone follows the prototype (email green, telegram accent);
            已停用 renders as a neutral pill -- the word carries it, and --warn
            stays reserved for collector degradation. */}
        <span
          className="pill"
          data-tone={
            channel.enabled
              ? channel.kind === "email"
                ? "success"
                : "acc"
              : undefined
          }
        >
          <Icon
            name={channel.kind === "email" ? "mail" : "message-square"}
            size={11}
          />
          {channel.kind === "email" ? "邮件 SMTP" : "Telegram"}
          {channel.enabled ? "" : " · 已停用"}
        </span>
        <span className="built">建于 {formatDateTime(channel.created_at)}</span>
      </header>

      <dl className="dl">
        {Object.entries(config)
          .filter(([key]) => key !== secretKey)
          .map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>{key}</dt>
              <dd>{Array.isArray(value) ? value.join("、") : String(value)}</dd>
            </div>
          ))}
        <div style={{ display: "contents" }}>
          <dt>{secretKey}</dt>
          <dd>
            <SecretState configured={isSet(config, secretKey)} />
          </dd>
        </div>
      </dl>

      <div className="actions-row">
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending}
        >
          <Icon name="send" size={13} />
          {test.isPending ? "发送中…" : "测试发送"}
        </button>
        <button
          type="button"
          onClick={() => toggle.mutate(!channel.enabled)}
          disabled={toggle.isPending}
        >
          <Icon name={channel.enabled ? "pause" : "play"} size={13} />
          {channel.enabled ? "停用" : "启用"}
        </button>
        {confirming ? (
          <ConfirmInline
            verb="删除"
            pending={remove.isPending}
            onConfirm={() => remove.mutate()}
            onCancel={() => setConfirming(false)}
          />
        ) : (
          <button
            type="button"
            className="btn-text"
            data-tone="danger"
            onClick={() => setConfirming(true)}
          >
            <Icon name="trash-2" size={13} />
            删除
          </button>
        )}
      </div>

      {test.data ? (
        test.data.ok ? (
          <p style={{ margin: 0, fontSize: 12.5, color: "var(--success)" }}>
            已交给服务器。
            <span className="muted">
              注意：SMTP 回 250
              只代表对方接收了，不代表送进了收件箱——去邮箱确认一次。
            </span>
          </p>
        ) : (
          <ErrorState
            title="测试发送失败"
            error={test.data.error ?? "未知错误"}
          />
        )
      ) : null}
      {test.isError ? (
        <ErrorState title="测试发送失败" error={test.error} />
      ) : null}
      {remove.isError ? (
        <ErrorState title="删除失败" error={remove.error} />
      ) : null}
    </article>
  );
}

function ChannelsSection() {
  const channels = useQuery(channelsOptions());
  const logs = useQuery(notifyLogsOptions());
  const [creating, setCreating] = useState(false);

  return (
    <section className="card" id="channels" style={COL}>
      <div className="card-h">
        <h2>
          <Icon name="send" size={15} />
          通知渠道
        </h2>
        <div className="actions">
          {!creating ? (
            <button
              type="button"
              data-variant="primary"
              onClick={() => setCreating(true)}
            >
              <Icon name="plus" size={13} />
              新建渠道
            </button>
          ) : null}
        </div>
      </div>

      {creating ? <ChannelForm onDone={() => setCreating(false)} /> : null}

      {channels.isPending ? <Loading rows={3} /> : null}
      {channels.isError ? (
        <ErrorState
          title="拉取渠道失败"
          error={channels.error}
          onRetry={() => void channels.refetch()}
        />
      ) : null}
      {channels.data?.length === 0 && !creating ? (
        <Empty message="还没有通知渠道。命中和降价都是通过渠道推送出去的，没有渠道就只能来这里看。" />
      ) : null}

      {channels.data && channels.data.length > 0 ? (
        <div className="channel-grid">
          {channels.data.map((channel) => (
            <ChannelCard key={channel.id} channel={channel} />
          ))}
        </div>
      ) : null}

      <div className="sub-h">
        <Icon name="history" size={12} />
        最近发送记录
      </div>
      {logs.isPending ? <Loading rows={2} /> : null}
      {logs.isError ? (
        <ErrorState title="拉取发送记录失败" error={logs.error} />
      ) : null}
      {logs.data?.length === 0 ? (
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
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
                    <span
                      className="pill"
                      data-tone={log.ok ? "success" : "danger"}
                    >
                      {log.ok ? "成功" : "失败"}
                    </span>
                  </td>
                  <td
                    className="muted"
                    style={{ wordBreak: "break-word", maxWidth: 320 }}
                  >
                    {log.error ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <p className="dim" style={{ margin: 0, fontSize: 11.5 }}>
        密钥永不回显：页面上只显示「已设置 / 未设置」，要更换就重建渠道。
      </p>
    </section>
  );
}

/* ============================== LLM 端点 (FR-P4-2) ==============================
 *
 * The api_key never enters the DOM in any form: no `value`, no
 * `defaultValue`, no masked placeholder standing in for one, and no React
 * state holding one. Responses cannot leak it either -- no response model in
 * `api/llm.py` has a field for it, and the page learns only
 * `api_key_configured`. So the input is write-only: empty means "keep what is
 * stored", and clearing is its own explicit checkbox.
 *
 * Audit by PATTERN, not by grepping for the one key you know
 * (`docs/m1-report.md`): plant a distinctive fake key, then check both
 * `document.documentElement.outerHTML` and every live input `.value`.
 */

/** Model name entry, with discovery as the optional part.
 *
 *  The text input is the primary control and always works. Discovery is a
 *  button beside it, because `GET /endpoints/{id}/models` answering
 *  `{"models": [], "error": "..."}` with HTTP 200 is a NORMAL answer -- many
 *  relay gateways never implement the route. Rendering that as a failure
 *  would block exactly the user the hand-entry fallback exists for, so it
 *  gets a plain note and the input stays usable.
 */
function ModelPicker({
  endpointId,
  inputId,
  value,
  onChange,
}: {
  endpointId: number | null;
  inputId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [wanted, setWanted] = useState(false);
  const models = useQuery({
    ...llmModelsOptions(endpointId ?? 0),
    // 0 is not a real row id. The query is disabled until an endpoint is
    // chosen AND the button is pressed, so it is only ever a placeholder key
    // -- nothing should poll a third party's models route on mount.
    enabled: wanted && endpointId !== null,
  });
  const found = models.data?.models ?? [];

  return (
    <div className="field">
      <label className="field-label" htmlFor={inputId}>
        模型
      </label>
      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <input
          id={inputId}
          className="mono"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={200}
          placeholder="deepseek-v4-pro"
          style={{ flex: "1 1 200px" }}
        />
        <button
          type="button"
          onClick={() => {
            if (wanted) void models.refetch();
            else setWanted(true);
          }}
          disabled={endpointId === null || models.isFetching}
        >
          <Icon name="refresh-cw" size={13} />
          {models.isFetching ? "拉取中…" : "拉取模型列表"}
        </button>
      </div>

      {endpointId === null ? (
        <span className="field-hint">
          先选择端点才能拉取它的模型列表。模型名也可以直接手填。
        </span>
      ) : null}

      {/* Chips rather than a <datalist>: a datalist only opens on typing, so
          a user who does not know any model name never sees the result of the
          button they just pressed. */}
      {found.length > 0 ? (
        <div
          style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap" }}
        >
          {found.map((name) => (
            <button
              key={name}
              type="button"
              className="mono"
              onClick={() => onChange(name)}
              style={{
                fontSize: 11.5,
                minHeight: 26,
                padding: "1px 8px",
                borderRadius: 999,
              }}
            >
              {name}
            </button>
          ))}
        </div>
      ) : null}

      {/* Our request failed (network, 401, our 404) -- that IS an error. */}
      {models.isError ? (
        <ErrorState title="拉取模型列表失败" error={models.error} />
      ) : null}

      {/* Their route failed. Not an error: state the reason and move on. */}
      {models.data?.error ? (
        <span className="field-hint">
          没能拉到模型列表：{models.data.error}
          。很多中转网关不实现这个接口，直接手填模型名就行。
        </span>
      ) : null}
      {models.data && found.length === 0 && !models.data.error ? (
        <span className="field-hint">端点返回了空列表。手填模型名即可。</span>
      ) : null}
    </div>
  );
}

/** Create or edit one endpoint. `endpoint` undefined means create. */
function EndpointForm({
  endpoint,
  onDone,
}: {
  endpoint?: LlmEndpoint;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const prefix = `llm-ep-${endpoint?.id ?? "new"}`;
  const save = useMutation({
    mutationFn: (body: LlmEndpointUpdate) =>
      endpoint === undefined
        ? createLlmEndpoint({
            label: body.label ?? "",
            base_url: body.base_url ?? "",
            api_key: body.api_key ?? "",
            wire_format: body.wire_format ?? "openai",
          })
        : updateLlmEndpoint(endpoint.id, body),
    onSuccess: onDone,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: keys.llmEndpoints }),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (name: string) => String(form.get(name) ?? "").trim();
    const typedKey = String(form.get("api_key") ?? "");
    const body: LlmEndpointUpdate = {
      label: text("label"),
      base_url: text("base_url"),
      wire_format: text("wire_format") === "anthropic" ? "anthropic" : "openai",
    };
    // Three cases, and the middle one is the whole point of the write-only
    // input: clear it explicitly, replace it with what was typed, or send no
    // `api_key` field at all and leave the stored one alone.
    if (form.get("clear_key") === "on") body.api_key = "";
    else if (typedKey) body.api_key = typedKey;
    save.mutate(body);
  }

  return (
    <form
      onSubmit={submit}
      className="inner-card"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
        gap: "14px 18px",
      }}
    >
      <div className="field">
        <label className="field-label" htmlFor={`${prefix}-label`}>
          端点备注名
        </label>
        <input
          id={`${prefix}-label`}
          name="label"
          required
          maxLength={60}
          defaultValue={endpoint?.label}
          placeholder="给自己看的备注"
        />
      </div>

      <div className="field">
        <label className="field-label" htmlFor={`${prefix}-url`}>
          Base URL
        </label>
        <input
          id={`${prefix}-url`}
          className="mono"
          name="base_url"
          required
          maxLength={500}
          defaultValue={endpoint?.base_url}
          placeholder="https://api.deepseek.com"
        />
        <span className="field-hint">
          不带
          <span className="mono"> /chat/completions </span>
          之类的路径，适配层自己拼。
        </span>
      </div>

      <div className="field">
        <label className="field-label" htmlFor={`${prefix}-wire`}>
          协议格式
        </label>
        <select
          id={`${prefix}-wire`}
          name="wire_format"
          defaultValue={endpoint?.wire_format ?? "openai"}
        >
          <option value="openai">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
        </select>
        <span className="field-hint">
          本地 Ollama、中转网关、自建 vLLM 基本都是 OpenAI 兼容。
        </span>
      </div>

      <div className="field">
        <label className="field-label" htmlFor={`${prefix}-key`}>
          API Key
        </label>
        {/* No value, no defaultValue, not even a masked one: nothing ever
            hands this page a stored key to put here. */}
        <input
          id={`${prefix}-key`}
          name="api_key"
          type="password"
          autoComplete="new-password"
          maxLength={500}
          placeholder={
            endpoint === undefined
              ? "本地模型可以留空"
              : endpoint.api_key_configured
                ? "已设置。留空＝不修改"
                : "未设置"
          }
        />
        {endpoint?.api_key_configured ? (
          <span className="switch-row">
            <input id={`${prefix}-clear`} name="clear_key" type="checkbox" />
            <label htmlFor={`${prefix}-clear`}>清除已保存的密钥</label>
          </span>
        ) : null}
        <span className="field-hint">
          密钥只写不读：任何接口响应、页面 HTML、输入框默认值里都不会出现它。
        </span>
      </div>

      <div className="form-actions span-all">
        <button type="submit" data-variant="primary" disabled={save.isPending}>
          <Icon name="save" size={13} />
          {save.isPending
            ? "保存中…"
            : endpoint === undefined
              ? "创建端点"
              : "保存"}
        </button>
        <button type="button" onClick={onDone}>
          取消
        </button>
      </div>

      {save.isError ? (
        <div style={{ gridColumn: "1 / -1" }}>
          <ErrorState title="保存端点失败" error={save.error} />
        </div>
      ) : null}
    </form>
  );
}

function EndpointCard({ endpoint }: { endpoint: LlmEndpoint }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [model, setModel] = useState("");

  const test = useMutation({
    mutationFn: () => testLlmEndpoint(endpoint.id, model.trim()),
  });
  const remove = useMutation({
    mutationFn: () => deleteLlmEndpoint(endpoint.id),
    // keys.llm, not just the endpoint list: deleting an endpoint detaches and
    // disables every scenario that pointed at it, so those forms are stale too.
    onSettled: () => queryClient.invalidateQueries({ queryKey: keys.llm }),
  });

  if (editing)
    return (
      <EndpointForm endpoint={endpoint} onDone={() => setEditing(false)} />
    );

  return (
    <article className="inner-card">
      <header className="inner-h">
        <span className="name">{endpoint.label}</span>
        <span className="pill mono">{endpoint.wire_format}</span>
        <span className="built">
          建于 {formatDateTime(endpoint.created_at)}
        </span>
      </header>

      <dl className="dl">
        <dt>Base URL</dt>
        <dd>{endpoint.base_url}</dd>
        <dt>API Key</dt>
        <dd>
          <SecretState configured={endpoint.api_key_configured} />
        </dd>
      </dl>

      {/* The test needs a model name because a connection is only testable
          through the model it will use -- `POST /test?model=` requires one,
          and "the base_url resolves" is not the thing that breaks. */}
      <ModelPicker
        endpointId={endpoint.id}
        inputId={`llm-ep-${endpoint.id}-test-model`}
        value={model}
        onChange={setModel}
      />

      <div className="actions-row">
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending || !model.trim()}
        >
          <Icon name="zap" size={13} />
          {test.isPending ? "测试中…" : "测试连接"}
        </button>
        <button type="button" onClick={() => setEditing(true)}>
          <Icon name="edit" size={13} />
          编辑
        </button>
        {confirming ? (
          <ConfirmInline
            verb="删除"
            pending={remove.isPending}
            onConfirm={() => remove.mutate()}
            onCancel={() => setConfirming(false)}
          />
        ) : (
          <button
            type="button"
            className="btn-text"
            data-tone="danger"
            onClick={() => setConfirming(true)}
          >
            <Icon name="trash-2" size={13} />
            删除
          </button>
        )}
      </div>

      {confirming ? (
        <p className="muted" style={{ margin: 0, fontSize: 11.5 }}>
          删除会把指向它的场景配置解绑并停用——那些场景没有端点就跑不了。
        </p>
      ) : null}

      {test.isPending ? (
        <p style={{ margin: 0, fontSize: 12.5 }}>
          正在发一次真实调用，可能要几十秒。
        </p>
      ) : null}
      {test.data ? (
        test.data.ok ? (
          <p style={{ margin: 0, fontSize: 12.5, color: "var(--success)" }}>
            连通。模型回了：<span className="mono">{test.data.text}</span>
          </p>
        ) : (
          // The server's own wording: "budget went to reasoning" and "wrong
          // model id" have different fixes and it already told them apart.
          <ErrorState
            title="测试连接失败"
            error={test.data.error ?? "未知错误"}
          />
        )
      ) : null}
      {test.isError ? (
        <ErrorState title="测试连接失败" error={test.error} />
      ) : null}
      {remove.isError ? (
        <ErrorState title="删除失败" error={remove.error} />
      ) : null}
    </article>
  );
}

/** One scenario's whole config, PUT as one object.
 *
 *  The draft is local state seeded from the server row, which is the "hold
 *  the edit in the form, submit, invalidate" case `state-management.md`
 *  allows -- three of these controls are written by buttons ("restore
 *  default", a model chip) and not only by typing, so they cannot be
 *  uncontrolled DOM state.
 */
function ScenarioForm({
  scenario,
  title,
  intro,
  config,
  endpoints,
}: {
  scenario: Scenario;
  title: string;
  intro: string;
  config: LlmScenarioConfig;
  endpoints: LlmEndpoint[];
}) {
  const queryClient = useQueryClient();
  const defaults = useQuery(llmDefaultPromptOptions(scenario));
  const [draft, setDraft] = useState({
    endpoint_id: config.endpoint_id,
    model: config.model ?? "",
    prompt_template: config.prompt_template ?? "",
    send_images: config.send_images,
    max_tokens: config.max_tokens === null ? "" : String(config.max_tokens),
    enabled: config.enabled,
  });
  // Derived, not remounted on a changing key: deleting an endpoint detaches
  // every scenario that pointed at it, and a draft still holding that id
  // would show a <select> with no matching <option>. Reading it as "未选择"
  // handles that at render time, where a remount would also throw away the
  // mutation state the moment the user saves -- which is when "已保存" is the
  // only feedback they get.
  const endpointId = endpoints.some((row) => row.id === draft.endpoint_id)
    ? draft.endpoint_id
    : null;
  const save = useMutation({
    mutationFn: () =>
      saveLlmScenario(scenario, {
        endpoint_id: endpointId,
        model: draft.model.trim() || null,
        // "" and null both mean "use the built-in default" on the backend.
        prompt_template: draft.prompt_template.trim() || null,
        send_images: draft.send_images,
        // "" means "the built-in default", which is what clearing the box asks
        // for. Number("") is 0, which the backend would reject as below the
        // floor -- an unhelpful 422 for someone who just emptied a field.
        max_tokens: draft.max_tokens.trim() ? Number(draft.max_tokens) : null,
        enabled: draft.enabled,
      }),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: keys.llmScenario(scenario) }),
  });

  const prefix = `llm-scenario-${scenario}`;
  const ready = scenarioReady({
    endpoint_id: endpointId,
    model: draft.model.trim() || null,
    enabled: draft.enabled,
  });

  return (
    <>
      <div className="card-h">
        <h2>
          <Icon name="sparkles" size={15} />
          AI 场景 · {title}
        </h2>
        <span className="pill" data-tone={ready ? "success" : undefined}>
          {ready ? "已就绪" : "未就绪"}
        </span>
      </div>
      <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
        {intro}
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-3)",
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor={`${prefix}-endpoint`}>
            指定端点
          </label>
          <select
            id={`${prefix}-endpoint`}
            value={endpointId ?? ""}
            style={{ maxWidth: 280 }}
            onChange={(event) =>
              setDraft((old) => ({
                ...old,
                endpoint_id:
                  event.target.value === "" ? null : Number(event.target.value),
              }))
            }
          >
            <option value="">未选择</option>
            {endpoints.map((endpoint) => (
              <option key={endpoint.id} value={endpoint.id}>
                {endpoint.label}
              </option>
            ))}
          </select>
        </div>

        <ModelPicker
          endpointId={endpointId}
          inputId={`${prefix}-model`}
          value={draft.model}
          onChange={(model) => setDraft((old) => ({ ...old, model }))}
        />

        <div className="field">
          <label className="field-label" htmlFor={`${prefix}-prompt`}>
            提示词模板
          </label>
          <textarea
            id={`${prefix}-prompt`}
            value={draft.prompt_template}
            onChange={(event) =>
              setDraft((old) => ({
                ...old,
                prompt_template: event.target.value,
              }))
            }
            rows={10}
            maxLength={20000}
            placeholder="留空表示使用内置默认模板"
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              resize: "vertical",
            }}
            aria-describedby={`${prefix}-placeholders`}
          />
          <div className="actions-row">
            <button
              type="button"
              onClick={() =>
                setDraft((old) => ({
                  ...old,
                  prompt_template:
                    defaults.data?.prompt_template ?? old.prompt_template,
                }))
              }
              disabled={defaults.data === undefined}
            >
              <Icon name="rotate-ccw" size={13} />
              恢复默认模板
            </button>
            {draft.prompt_template ? (
              <button
                type="button"
                onClick={() =>
                  setDraft((old) => ({ ...old, prompt_template: "" }))
                }
              >
                <Icon name="eraser" size={13} />
                清空（改用内置默认）
              </button>
            ) : null}
          </div>

          {/* The placeholder contract, from the same response as the default
              template. Without it the template is a contract nobody can see:
              `prompts.render` substitutes with str.replace, so a typo'd
              {plcaeholder} survives into the prompt as literal text and the
              answer just quietly degrades -- no error anywhere. */}
          <div id={`${prefix}-placeholders`} className="field-hint">
            {defaults.isError ? (
              <ErrorState title="拉取默认模板失败" error={defaults.error} />
            ) : defaults.data === undefined ? (
              "正在拉取占位符列表…"
            ) : (
              <>
                可用占位符（会被替换成真实数据）：
                {defaults.data.placeholders.map((name) => (
                  <span
                    key={name}
                    className="mono"
                    style={{ color: "var(--acc2)" }}
                  >
                    {" "}
                    {`{${name}}`}
                  </span>
                ))}
                。写错的占位符会原样留在提示词里，不会报错，只会让回答变差。
              </>
            )}
          </div>
        </div>

        {scenario === "item" ? (
          <div className="field">
            <span className="switch-row">
              <input
                id={`${prefix}-images`}
                type="checkbox"
                data-switch
                checked={draft.send_images}
                onChange={(event) =>
                  setDraft((old) => ({
                    ...old,
                    send_images: event.target.checked,
                  }))
                }
              />
              <label htmlFor={`${prefix}-images`}>附带商品图片</label>
            </span>
            {/* What it costs, measured, rather than presented as free. */}
            <span className="field-hint">
              最多 3 张，通常只有封面 1
              张；模型不支持视觉时自动降级为纯文本并在结果中标注
            </span>
          </div>
        ) : null}

        <div className="field">
          <label className="field-label" htmlFor={`${prefix}-budget`}>
            回答的 token 上限
          </label>
          <input
            id={`${prefix}-budget`}
            className="mono"
            type="number"
            min={1024}
            max={65536}
            step={1024}
            placeholder="留空用默认值 16384"
            style={{ maxWidth: 280 }}
            value={draft.max_tokens}
            onChange={(event) =>
              setDraft((old) => ({ ...old, max_tokens: event.target.value }))
            }
          />
          {/* This field exists because the starved answer's own advice is
              "raise max_tokens". Before it, taking that advice meant editing
              Python -- an error naming a knob the product does not offer is
              not actionable. The numbers are measured, not guessed. */}
          <span className="field-hint">
            推理模型的思考过程占用同一预算，建议 ≥ 16384
          </span>
        </div>

        <span className="switch-row">
          <input
            id={`${prefix}-enabled`}
            type="checkbox"
            data-switch
            checked={draft.enabled}
            onChange={(event) =>
              setDraft((old) => ({ ...old, enabled: event.target.checked }))
            }
          />
          <label htmlFor={`${prefix}-enabled`}>启用这个场景</label>
        </span>

        <div className="actions-row">
          <button
            type="submit"
            data-variant="primary"
            disabled={save.isPending}
          >
            <Icon name="save" size={13} />
            {save.isPending ? "保存中…" : "保存配置"}
          </button>
          {save.isSuccess && !save.isPending ? (
            <span style={{ fontSize: 12.5, color: "var(--success)" }}>
              已保存
            </span>
          ) : null}
        </div>
        {save.isError ? (
          <ErrorState title="保存配置失败" error={save.error} />
        ) : null}
      </form>
    </>
  );
}

function ScenarioSection({
  id,
  scenario,
  title,
  intro,
  endpoints,
}: {
  /** Anchor id; only the first scenario card carries one (#ai). */
  id?: string;
  scenario: Scenario;
  title: string;
  intro: string;
  endpoints: LlmEndpoint[];
}) {
  const config = useQuery(llmScenarioOptions(scenario));

  // The card wrapper is stable so the anchor spy can observe #ai at mount;
  // the three states swap only its contents.
  return (
    <section className="card" id={id} style={COL}>
      {config.isPending ? <Loading rows={3} /> : null}
      {config.isError ? (
        <ErrorState
          title={`拉取${title}配置失败`}
          error={config.error}
          onRetry={() => void config.refetch()}
        />
      ) : null}
      {config.data ? (
        <ScenarioForm
          scenario={scenario}
          title={title}
          intro={intro}
          config={config.data}
          endpoints={endpoints}
        />
      ) : null}
    </section>
  );
}

function LlmSection() {
  const endpoints = useQuery(llmEndpointsOptions());
  const [creating, setCreating] = useState(false);

  return (
    <>
      <section className="card" id="llm" style={COL}>
        <div className="card-h">
          <h2>
            <Icon name="cpu" size={15} />
            LLM 推理端点
          </h2>
          <div className="actions">
            {!creating ? (
              <button
                type="button"
                data-variant="primary"
                onClick={() => setCreating(true)}
              >
                <Icon name="plus" size={13} />
                新建端点
              </button>
            ) : null}
          </div>
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          任何 OpenAI 兼容或 Anthropic 格式的端点都行，包括本地 Ollama 和自建
          vLLM。 「测试连接」会发一次真实调用——它测的是这条路真的通，不是
          base_url 能解析。
        </p>

        {creating ? <EndpointForm onDone={() => setCreating(false)} /> : null}

        {endpoints.isPending ? <Loading rows={2} /> : null}
        {endpoints.isError ? (
          <ErrorState
            title="拉取端点失败"
            error={endpoints.error}
            onRetry={() => void endpoints.refetch()}
          />
        ) : null}
        {endpoints.data?.length === 0 && !creating ? (
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            还没有端点。两个 AI 场景都要先有端点才能用。
          </p>
        ) : null}

        {(endpoints.data ?? []).map((endpoint) => (
          <EndpointCard key={endpoint.id} endpoint={endpoint} />
        ))}
      </section>

      <ScenarioSection
        id="ai"
        scenario="market"
        title="行情分析"
        intro="入口在行情分析页，手动触发"
        endpoints={endpoints.data ?? []}
      />
      <ScenarioSection
        scenario="item"
        title="单品建议"
        intro="入口在商品详情页，手动触发"
        endpoints={endpoints.data ?? []}
      />
    </>
  );
}

/** The bookmarklet path: one click on a goofish tab instead of five devtools
 *  steps.
 *
 *  What it hands out is a single-use ticket, never the panel token -- the
 *  script executes inside a page goofish serves, and anything on that page can
 *  read what it sends. Worst case the ticket is stolen and buys exactly one
 *  cookie import.
 *
 *  It does not replace the devtools flow below. That one is guaranteed
 *  complete (the request's own Cookie header, right domains, every field), and
 *  it is what still works if the page's CSP refuses the bookmarklet's fetch.
 */
function BookmarkletBlock() {
  const mint = useMutation({ mutationFn: mintImportTicket });
  const script = mint.data
    ? buildBookmarklet(window.location.origin, mint.data.ticket)
    : "";
  // An https goofish page cannot fetch a plain-http panel -- mixed content,
  // blocked in the browser, nothing the backend can send fixes it. localhost
  // and 127.0.0.1 are the exception browsers make. Detectable exactly, so the
  // page says so up front instead of letting the click fail as a TypeError.
  const blockedByMixedContent =
    window.location.protocol === "http:" &&
    !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

  return (
    <div className="bookmarklet">
      <p className="muted" style={{ margin: 0 }}>
        生成一个书签拖到书签栏，之后在<strong>已登录的闲鱼页面</strong>
        上点它，就会把
        <span className="mono"> document.cookie </span>
        直接送回面板，不用再走开发者工具。
      </p>
      {/* Say what it sends. The script now also reads the browser environment
          so the collector stops claiming to be a different machine than the
          one the cookies came from; that is worth one sentence rather than a
          surprise for anyone who reads the script below. */}
      <p className="muted" style={{ margin: 0 }}>
        它同时会捎上这台浏览器的<strong>环境信息</strong>
        （UA、语言、时区、屏幕尺寸），让采集
        请求和凭证来自同一台机器。都是页面上已经能读到的只读属性，不做任何额外探测。
      </p>
      {blockedByMixedContent ? (
        <p className="muted" style={{ margin: 0 }}>
          <strong>这台面板用不了书签脚本</strong>：它开在明文 http 的
          <span className="mono"> {window.location.host} </span>
          上，而闲鱼页面是 https——浏览器不允许 https 页面去 fetch http
          地址，后端加什么头都
          绕不过去。请用下面的开发者工具流程，或者给面板配上 https。仓库里的
          <span className="mono"> extension/ </span>
          扩展不在页面里发请求，所以不受这一条限制（但 Chrome
          的本地网络访问限制是否管得到 扩展还没有定论，见 docs/operations.md）。
        </p>
      ) : null}
      <button
        type="button"
        onClick={() => mint.mutate()}
        disabled={mint.isPending || blockedByMixedContent}
        style={{ alignSelf: "flex-start" }}
      >
        <Icon name="bookmark-plus" size={13} />
        {mint.isPending ? "生成中…" : mint.data ? "重新生成" : "生成书签"}
      </button>
      {mint.isError ? <ErrorState title="生成失败" error={mint.error} /> : null}
      {mint.data ? (
        <>
          <p style={{ margin: 0 }}>
            把下面这个链接<strong>拖到书签栏</strong>
            （右键复制链接也行），然后到闲鱼标签页上点一下：
          </p>
          <a
            /* React sanitises a `javascript:` href, so it is set on the DOM
               node directly. Dragging needs a real anchor with a real href --
               a copyable text box alone would mean hand-creating a bookmark. */
            ref={(node) => node?.setAttribute("href", script)}
            onClick={(event) => event.preventDefault()}
            style={{
              alignSelf: "flex-start",
              padding: "4px 12px",
              border: "1px solid var(--border-strong)",
              borderRadius: "var(--radius-sm)",
              background: "var(--surface-2)",
              cursor: "grab",
            }}
          >
            导入闲鱼凭证
          </a>
          <p className="muted" style={{ margin: 0 }}>
            这张票据<strong>只能用一次</strong>，
            {formatDateTime(mint.data.expires_at)} 过期（约 10
            分钟）。用过或过期后再点一次
            「重新生成」换一张——旧书签会明确告诉你是过期还是已用过。票据本身是密钥，别贴给别人。
          </p>
          {/* Same rule as everywhere else: the import is not the result. And
              the "unusable" this leaves behind is expected, not a failure --
              without saying so here the page contradicts docs/operations.md. */}
          <p style={{ margin: 0 }}>
            导入成功只代表 cookie 收下了。
            <strong>去「监控任务」点一次「立即运行」</strong>
            ，跑通了才算恢复。
          </p>
          <p className="muted" style={{ margin: 0 }}>
            书签导入后上面会显示「会话不可用 ·
            <span className="mono"> no _m_h5_tk </span>
            」，这是<strong>正常的</strong>：签名 token 只存在于 taobao
            域，闲鱼页面上读不到，
            由下一个周期的浏览器兜底去领。那个周期会慢一些、只看第 1
            页，之后就恢复正常。
          </p>
          <details>
            <summary className="muted">书签里是什么（可复制）</summary>
            <textarea
              readOnly
              value={script}
              rows={4}
              onFocus={(event) => event.currentTarget.select()}
              style={{
                width: "100%",
                marginTop: "var(--space-2)",
                fontFamily: "var(--font-mono)",
                fontSize: 11,
                resize: "vertical",
              }}
            />
          </details>
        </>
      ) : null}
    </div>
  );
}

/** Session health, credential import/removal, notification channels, LLM
 *  endpoints + scenarios, and panel access -- one page, five anchored
 *  sections (design.md merge decision).
 *
 *  Importing a cookie header is the one step that cannot be automated: a
 *  headless container cannot solve a slider, so the login state has to come
 *  from a human's own browser. Without this screen the tool cannot be
 *  deployed at all.
 */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const session = useQuery(sessionOptions());
  // Hero meta only; the section queries share these cache entries.
  const channels = useQuery(channelsOptions());
  const endpoints = useQuery(llmEndpointsOptions());
  const [paste, setPaste] = useState("");
  const [confirmingClear, setConfirmingClear] = useState(false);
  const activeSection = useAnchorSpy();

  // /settings#channels (the old /channels route redirects here) must land on
  // the channels card, on load and on every nav click -- same approach as the
  // the monitors page's #tasks. location identity changes per navigation, so clicking
  // an anchor while already at that hash still scrolls.
  useEffect(() => {
    if (!location.hash) return;
    document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [location]);

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: keys.session });
  const doImport = useMutation({
    mutationFn: (cookie_header: string) =>
      // origin is spelled out because the generated type requires it;
      // it is the only site these cookies come from.
      importCookies({ cookie_header, origin: "https://www.goofish.com" }),
    onSuccess: () => {
      setPaste("");
      void refresh();
    },
  });
  const doClear = useMutation({
    mutationFn: clearCookies,
    onSuccess: () => {
      setConfirmingClear(false);
      void refresh();
    },
  });

  const state = session.data;

  return (
    <>
      <PageHero
        eyebrow="SYSTEM PREFERENCES · CREDENTIAL VAULT"
        ghost="CONFIG"
        title={
          <>
            设置<span className="thin"> / 系统控制台</span>
          </>
        }
        meta={
          <>
            <span>
              <Icon name="shield-check" size={12} />
              <span className="ok">
                密钥永不回显：页面只显示「已设置 / 未设置」
              </span>
            </span>
            {endpoints.data ? (
              <span>
                <Icon name="cpu" size={12} />
                LLM 端点 {endpoints.data.length} 个
              </span>
            ) : null}
            {channels.data ? (
              <span>
                <Icon name="bell" size={12} />
                通知渠道 {channels.data.filter((c) => c.enabled).length}/
                {channels.data.length} 启用
              </span>
            ) : null}
          </>
        }
      />

      <div className="settings-grid">
        <nav className="anchor-nav" aria-label="设置分区">
          {SECTIONS.map((s) => (
            <Link
              key={s.id}
              to={`#${s.id}`}
              className={activeSection === s.id ? "active" : ""}
              aria-current={activeSection === s.id ? "true" : undefined}
            >
              <Icon name={s.icon} size={14} />
              {s.label}
            </Link>
          ))}
        </nav>

        <div className="settings-col">
          {/* ---------- 1. 采集会话 ---------- */}
          <section className="card" id="session" style={COL}>
            <div className="card-h">
              <h2>
                <Icon name="cookie" size={15} />
                采集会话
              </h2>
              {state ? (
                state.needs_verification ? (
                  <span className="pill" data-tone="warn">
                    需要人工验证
                  </span>
                ) : !state.usable ? (
                  <span className="pill" data-tone="danger">
                    无可用会话
                  </span>
                ) : state.proven ? (
                  <span className="pill" data-tone="success">
                    <Icon name="check" size={11} />
                    会话可用
                  </span>
                ) : (
                  <span className="pill">已导入 · 未验证</span>
                )
              ) : null}
            </div>

            {session.isPending ? <Loading rows={2} /> : null}
            {session.isError ? (
              <ErrorState
                title="拉取会话状态失败"
                error={session.error}
                onRetry={() => void session.refetch()}
              />
            ) : null}

            {state ? (
              <>
                {/* Three distinct verdicts. "Usable" and "a human must act" are
                    not opposites: a session can be established and still be
                    challenged on one endpoint, which is a wait-and-retry, while
                    needing verification means nothing will improve on its own. */}
                {state.needs_verification ? (
                  <div role="alert" className="alert" data-tone="warn">
                    <Icon name="alert-triangle" size={15} />
                    <span>
                      <strong>需要人工验证。</strong>{" "}
                      上游对这些接口出了风控挑战：
                      <span className="mono">
                        {" "}
                        {state.challenged_apis.join("、")}
                      </span>
                      。自动重试不会好转，请按下面的步骤在自己的浏览器里过一次验证，再重新导入
                      cookie。
                    </span>
                  </div>
                ) : !state.usable ? (
                  <p
                    style={{
                      margin: 0,
                      fontSize: 13,
                      color: "var(--text-muted)",
                    }}
                  >
                    没有可用会话，采集只能走浏览器兜底或直接失败。
                  </p>
                ) : state.proven ? (
                  <p
                    style={{
                      margin: 0,
                      fontSize: 13,
                      color: "var(--success)",
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    <span className="dot dot-pulse" />
                    会话可用，最近一次采集成功于{" "}
                    {formatRelativeTime(state.last_success_at)}。
                  </p>
                ) : (
                  /* The distinction that matters: importing a cookie clears the
                     challenge map unconditionally, so "no challenge" right after
                     an import only means nothing has failed YET. Reporting that as
                     a healthy session sent a user chasing a working panel while
                     the detail endpoint was still blocked. */
                  <div className="inner-card" style={{ fontSize: 13 }}>
                    <span>
                      <strong>凭证已导入，但还没有被验证过。</strong>{" "}
                      导入只是收下了
                      cookie；要等一次真实采集成功，这里才会变成「会话可用」。
                      去「监控任务」页对任一规则点「立即运行」，或等下一轮调度。
                    </span>
                  </div>
                )}

                <div className="sub-h">
                  <Icon name="info" size={12} />
                  会话详情（已脱敏）
                </div>
                <dl className="dl">
                  <dt>来源</dt>
                  <dd>{state.origin ?? "—"}</dd>
                  <dt>建立于</dt>
                  <dd>
                    {state.established_at
                      ? `${formatDateTime(state.established_at)}（${formatRelativeTime(state.established_at)}）`
                      : "—"}
                  </dd>
                  <dt>最近错误</dt>
                  <dd style={{ wordBreak: "break-word" }}>
                    {state.last_error ?? "—"}
                  </dd>
                  <dt>已导入 cookie</dt>
                  {/* Names only. The values never leave the backend. */}
                  <dd style={{ color: "var(--acc2)" }}>
                    {state.cookie_names.length > 0
                      ? state.cookie_names.join(" ")
                      : "—"}{" "}
                    {state.cookie_names.length > 0 ? (
                      <span className="dim">（仅名称，永不回显值）</span>
                    ) : null}
                  </dd>
                  <dt>采集端身份</dt>
                  {/* A summary line, never the snapshot. Same rule as the cookie
                      list: hardwareConcurrency / deviceMemory and the rest stay in
                      the backend -- rendering them here would hand a page's worth
                      of fingerprint to anything that can read this panel. */}
                  <dd style={{ wordBreak: "break-word" }}>
                    {state.fingerprint ??
                      "内置默认值（开发者工具导入不带环境信息）"}
                    {state.fingerprint && !state.fingerprint_applied ? (
                      <span className="muted">
                        {" "}
                        ——<strong>已记录，但没有采用</strong>
                        。这份快照来自移动端浏览器，而本工具驱动的每一个页面和接口都是
                        PC 版（
                        <span className="mono">pc.search</span> /{" "}
                        <span className="mono">pc.detail</span>
                        ）。带着手机 UA 去请求 PC
                        接口，是把一种不一致换成更糟的一种，所以采集仍
                        然用内置默认值。想让它生效，请在电脑浏览器上重新点一次书签。
                      </span>
                    ) : null}
                  </dd>
                </dl>
              </>
            ) : null}

            <div className="sub-h">
              <Icon name="bookmark-plus" size={12} />
              快捷书签导入
            </div>
            <BookmarkletBlock />

            <div className="sub-h">
              <Icon name="file-code-2" size={12} />
              手动粘贴 Cookie 凭证（开发者工具，最稳的一条路）
            </div>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const value = paste.trim();
                if (value) doImport.mutate(value);
              }}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "var(--space-2)",
              }}
            >
              <label className="field-label" htmlFor="cookie-paste">
                Cookie 键值对字符串
              </label>
              <textarea
                id="cookie-paste"
                value={paste}
                onChange={(event) => setPaste(event.target.value)}
                rows={4}
                placeholder="cookie2=…; unb=…; _m_h5_tk=…"
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 12,
                  resize: "vertical",
                }}
                aria-describedby="cookie-help"
              />
              <div id="cookie-help" className="field-hint">
                <p>
                  1. 登录闲鱼，打开任一商品详情页（
                  <span className="mono">goofish.com/item?id=…</span>
                  ），出现滑块则完成验证。
                </p>
                {/* Copy the header from the request being reproduced, rather than
                    hunting for a particular cookie name. Which cookie carries a
                    passed verification was never actually measured here -- an
                    early research note guessed `x5sec`, a user passed a slider and
                    got no such cookie. The request's own header sidesteps the
                    question: it is by definition the complete credential set that
                    endpoint receives, on the right domain. */}
                <p>
                  2. 刷新后打开开发者工具 → Network → 过滤
                  <span className="mono"> detail </span>→ 选中
                  <span className="mono"> mtop.taobao.idle.pc.detail </span>→
                  复制它的
                  <span className="mono"> Cookie </span>
                  请求头，粘贴到这里。
                </p>
                <p>值仅存于后端，页面只显示 cookie 名。</p>
              </div>
              <div className="actions-row">
                <button
                  type="submit"
                  data-variant="primary"
                  disabled={doImport.isPending || !paste.trim()}
                >
                  <Icon name="upload" size={13} />
                  {doImport.isPending ? "导入中…" : "导入凭据"}
                </button>
                {confirmingClear ? (
                  <ConfirmInline
                    verb="清除"
                    pending={doClear.isPending}
                    onConfirm={() => doClear.mutate()}
                    onCancel={() => setConfirmingClear(false)}
                  />
                ) : (
                  <button
                    type="button"
                    data-variant="danger"
                    onClick={() => setConfirmingClear(true)}
                    disabled={!state?.cookie_names.length}
                  >
                    <Icon name="trash-2" size={13} />
                    清除会话凭证
                  </button>
                )}
              </div>
              {doImport.isError ? (
                <ErrorState title="导入失败" error={doImport.error} />
              ) : null}
              {doClear.isError ? (
                <ErrorState title="清除失败" error={doClear.error} />
              ) : null}
              {doImport.isSuccess ? (
                <p
                  style={{ margin: 0, fontSize: 12.5, color: "var(--success)" }}
                >
                  已导入。若上面仍显示没有可用会话，通常是缺少
                  <span className="mono"> _m_h5_tk </span>
                  ——采集器会自己取一个，等一轮再看。
                </p>
              ) : null}
            </form>
          </section>

          {/* ---------- 2. 通知渠道 ---------- */}
          <ChannelsSection />

          {/* ---------- 3 + 4. LLM 端点与 AI 场景 ---------- */}
          <LlmSection />

          {/* ---------- 5. 面板访问 ---------- */}
          <section className="card" id="access" style={COL}>
            <div className="card-h">
              <h2>
                <Icon name="key" size={15} />
                面板访问安全
              </h2>
            </div>
            <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
              面板用的是后端启动时的
              <span className="mono"> SFD_API_TOKEN </span>
              ，只能在服务端改。这里只能忘掉浏览器里存的那一份。
            </p>
            <button
              type="button"
              data-variant="danger"
              onClick={() => {
                clearToken();
                window.location.reload();
              }}
              style={{ alignSelf: "flex-start" }}
            >
              <Icon name="shield-off" size={13} />
              忘掉本机 Token 并退出
            </button>
          </section>
        </div>
      </div>
    </>
  );
}
