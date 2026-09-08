import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { clearToken } from "../api/client";
import {
  clearCookies,
  createLlmEndpoint,
  deleteLlmEndpoint,
  importCookies,
  keys,
  mintImportTicket,
  llmDefaultPromptOptions,
  llmEndpointsOptions,
  llmModelsOptions,
  llmScenarioOptions,
  saveLlmScenario,
  sessionOptions,
  testLlmEndpoint,
  updateLlmEndpoint,
  type LlmEndpoint,
  type LlmEndpointUpdate,
  type LlmScenarioConfig,
  type Scenario,
} from "../api/queries";
import { ErrorState, Loading } from "../components/States";
import { buildBookmarklet } from "../lib/bookmarklet";
import { formatDateTime, formatRelativeTime } from "../lib/format";
import { scenarioReady } from "../lib/llm";

const CARD: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  background: "var(--surface)",
  padding: "var(--space-4)",
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-3)",
};

const FIELD: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-1)",
};

const CHECKBOX: React.CSSProperties = {
  display: "flex",
  gap: "var(--space-2)",
  alignItems: "center",
  fontSize: 13,
};

/* ---------- LLM endpoints (FR-P4-2) ----------
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
    <div style={FIELD}>
      <label htmlFor={inputId}>模型</label>
      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <input
          id={inputId}
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
          {models.isFetching ? "拉取中…" : "拉取模型列表"}
        </button>
      </div>

      {endpointId === null ? (
        <span className="muted" style={{ fontSize: 11.5 }}>
          先选择端点才能拉取它的模型列表。模型名也可以直接手填。
        </span>
      ) : null}

      {/* Chips rather than a <datalist>: a datalist only opens on typing, so
          a user who does not know any model name never sees the result of the
          button they just pressed. */}
      {found.length > 0 ? (
        <div style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
          {found.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => onChange(name)}
              style={{ fontSize: 11.5, minHeight: 26, padding: "1px 8px" }}
            >
              {name}
            </button>
          ))}
        </div>
      ) : null}

      {/* Our request failed (network, 401, our 404) -- that IS an error. */}
      {models.isError ? <ErrorState title="拉取模型列表失败" error={models.error} /> : null}

      {/* Their route failed. Not an error: state the reason and move on. */}
      {models.data?.error ? (
        <span className="muted" style={{ fontSize: 11.5 }}>
          没能拉到模型列表：{models.data.error}
          。很多中转网关不实现这个接口，直接手填模型名就行。
        </span>
      ) : null}
      {models.data && found.length === 0 && !models.data.error ? (
        <span className="muted" style={{ fontSize: 11.5 }}>
          端点返回了空列表。手填模型名即可。
        </span>
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
    onSettled: () => queryClient.invalidateQueries({ queryKey: keys.llmEndpoints }),
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
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
        gap: "var(--space-3)",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius)",
        padding: "var(--space-3)",
      }}
    >
      <div style={FIELD}>
        <label htmlFor={`${prefix}-label`}>名称</label>
        <input
          id={`${prefix}-label`}
          name="label"
          required
          maxLength={60}
          defaultValue={endpoint?.label}
          placeholder="给自己看的备注"
        />
      </div>

      <div style={FIELD}>
        <label htmlFor={`${prefix}-url`}>Base URL</label>
        <input
          id={`${prefix}-url`}
          name="base_url"
          required
          maxLength={500}
          defaultValue={endpoint?.base_url}
          placeholder="https://api.deepseek.com"
        />
        <span className="muted" style={{ fontSize: 11 }}>
          不带
          <span className="mono"> /chat/completions </span>
          之类的路径，适配层自己拼。
        </span>
      </div>

      <div style={FIELD}>
        <label htmlFor={`${prefix}-wire`}>协议格式</label>
        <select
          id={`${prefix}-wire`}
          name="wire_format"
          defaultValue={endpoint?.wire_format ?? "openai"}
        >
          <option value="openai">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
        </select>
        <span className="muted" style={{ fontSize: 11 }}>
          本地 Ollama、中转网关、自建 vLLM 基本都是 OpenAI 兼容。
        </span>
      </div>

      <div style={FIELD}>
        <label htmlFor={`${prefix}-key`}>API Key</label>
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
          <span style={CHECKBOX}>
            <input id={`${prefix}-clear`} name="clear_key" type="checkbox" />
            <label htmlFor={`${prefix}-clear`}>清除已保存的密钥</label>
          </span>
        ) : null}
        <span className="muted" style={{ fontSize: 11 }}>
          密钥只写不读：任何接口响应、页面 HTML、输入框默认值里都不会出现它。
        </span>
      </div>

      <div style={{ gridColumn: "1 / -1", display: "flex", gap: "var(--space-2)" }}>
        <button type="submit" data-variant="primary" disabled={save.isPending}>
          {save.isPending ? "保存中…" : endpoint === undefined ? "创建端点" : "保存"}
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

  const test = useMutation({ mutationFn: () => testLlmEndpoint(endpoint.id, model.trim()) });
  const remove = useMutation({
    mutationFn: () => deleteLlmEndpoint(endpoint.id),
    // ["llm"], not just the endpoint list: deleting an endpoint detaches and
    // disables every scenario that pointed at it, so those forms are stale too.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["llm"] }),
  });

  if (editing) return <EndpointForm endpoint={endpoint} onDone={() => setEditing(false)} />;

  return (
    <article
      style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "var(--space-3)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
      }}
    >
      <header style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
        <strong>{endpoint.label}</strong>
        <span className="pill">{endpoint.wire_format}</span>
        <span className="muted" style={{ fontSize: 11, marginLeft: "auto" }}>
          建于 {formatDateTime(endpoint.created_at)}
        </span>
      </header>

      <dl
        style={{
          margin: 0,
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "2px 12px",
          fontSize: 12.5,
        }}
      >
        <dt className="muted">Base URL</dt>
        <dd className="mono" style={{ margin: 0, wordBreak: "break-all" }}>
          {endpoint.base_url}
        </dd>
        <dt className="muted">API Key</dt>
        {/* Never the value, only whether one exists. */}
        <dd style={{ margin: 0 }}>{endpoint.api_key_configured ? "已设置" : "未设置"}</dd>
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

      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending || !model.trim()}
        >
          {test.isPending ? "测试中…" : "测试连接"}
        </button>
        <button type="button" onClick={() => setEditing(true)}>
          编辑
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
          <ErrorState title="测试连接失败" error={test.data.error ?? "未知错误"} />
        )
      ) : null}
      {test.isError ? <ErrorState title="测试连接失败" error={test.error} /> : null}
      {remove.isError ? <ErrorState title="删除失败" error={remove.error} /> : null}
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
    onSettled: () => queryClient.invalidateQueries({ queryKey: keys.llmScenario(scenario) }),
  });

  const prefix = `llm-scenario-${scenario}`;
  const ready = scenarioReady({
    endpoint_id: endpointId,
    model: draft.model.trim() || null,
    enabled: draft.enabled,
  });

  return (
    <article style={CARD}>
      <header style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>{title}</h2>
        <span className="pill" data-tone={ready ? "success" : undefined}>
          {ready ? "已就绪" : "未就绪"}
        </span>
      </header>
      <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
        {intro}
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
        style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}
      >
        <div style={FIELD}>
          <label htmlFor={`${prefix}-endpoint`}>端点</label>
          <select
            id={`${prefix}-endpoint`}
            value={endpointId ?? ""}
            onChange={(event) =>
              setDraft((old) => ({
                ...old,
                endpoint_id: event.target.value === "" ? null : Number(event.target.value),
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

        <div style={FIELD}>
          <label htmlFor={`${prefix}-prompt`}>提示词模板</label>
          <textarea
            id={`${prefix}-prompt`}
            value={draft.prompt_template}
            onChange={(event) =>
              setDraft((old) => ({ ...old, prompt_template: event.target.value }))
            }
            rows={10}
            maxLength={20000}
            placeholder="留空表示使用内置默认模板"
            style={{ fontFamily: "var(--font-mono)", fontSize: 12, resize: "vertical" }}
            aria-describedby={`${prefix}-placeholders`}
          />
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() =>
                setDraft((old) => ({
                  ...old,
                  prompt_template: defaults.data?.prompt_template ?? old.prompt_template,
                }))
              }
              disabled={defaults.data === undefined}
            >
              恢复默认模板
            </button>
            {draft.prompt_template ? (
              <button
                type="button"
                onClick={() => setDraft((old) => ({ ...old, prompt_template: "" }))}
              >
                清空（改用内置默认）
              </button>
            ) : null}
          </div>

          {/* The placeholder contract, from the same response as the default
              template. Without it the template is a contract nobody can see:
              `prompts.render` substitutes with str.replace, so a typo'd
              {plcaeholder} survives into the prompt as literal text and the
              answer just quietly degrades -- no error anywhere. */}
          <div id={`${prefix}-placeholders`} className="muted" style={{ fontSize: 11.5 }}>
            {defaults.isError ? (
              <ErrorState title="拉取默认模板失败" error={defaults.error} />
            ) : defaults.data === undefined ? (
              "正在拉取占位符列表…"
            ) : (
              <>
                可用占位符（会被替换成真实数据）：
                {defaults.data.placeholders.map((name) => (
                  <span key={name} className="mono">
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
          <div style={FIELD}>
            <span style={CHECKBOX}>
              <input
                id={`${prefix}-images`}
                type="checkbox"
                checked={draft.send_images}
                onChange={(event) =>
                  setDraft((old) => ({ ...old, send_images: event.target.checked }))
                }
              />
              <label htmlFor={`${prefix}-images`}>附带商品图片</label>
            </span>
            {/* What it costs, measured, rather than presented as free. */}
            <span className="muted" style={{ fontSize: 11.5 }}>
              实测一件商品的 3 张图约 1.3 MB base64，而 327 件里有 325 件只有 1 张图（就是封面），
              所以多数情况下只会发 1 张。是否真的用得上取决于模型有没有视觉能力——
              这一点没有任何地方记录，后端只能按模型名猜；猜不支持时会降级为纯文本，
              并在结果里说明降级了。
            </span>
          </div>
        ) : null}

        <div style={FIELD}>
          <label htmlFor={`${prefix}-budget`}>回答的 token 上限</label>
          <input
            id={`${prefix}-budget`}
            type="number"
            min={1024}
            max={65536}
            step={1024}
            placeholder="留空用默认值 16384"
            value={draft.max_tokens}
            onChange={(event) => setDraft((old) => ({ ...old, max_tokens: event.target.value }))}
          />
          {/* This field exists because the starved answer's own advice is
              "raise max_tokens". Before it, taking that advice meant editing
              Python -- an error naming a knob the product does not offer is
              not actionable. The numbers are measured, not guessed. */}
          <span className="muted" style={{ fontSize: 11.5 }}>
            这些模型会先"想"再答，想的部分也算在这个上限里。实测 4096 时行情分析
            只想不答（返回空回答），16384 才答得出来。看到「预算被推理用光」的提示就调大这里。
          </span>
        </div>

        <span style={CHECKBOX}>
          <input
            id={`${prefix}-enabled`}
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => setDraft((old) => ({ ...old, enabled: event.target.checked }))}
          />
          <label htmlFor={`${prefix}-enabled`}>启用这个场景</label>
        </span>

        <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
          <button type="submit" data-variant="primary" disabled={save.isPending}>
            {save.isPending ? "保存中…" : "保存配置"}
          </button>
          {save.isSuccess && !save.isPending ? (
            <span style={{ fontSize: 12.5, color: "var(--success)" }}>已保存</span>
          ) : null}
        </div>
        {save.isError ? <ErrorState title="保存配置失败" error={save.error} /> : null}
      </form>
    </article>
  );
}

function ScenarioSection({
  scenario,
  title,
  intro,
  endpoints,
}: {
  scenario: Scenario;
  title: string;
  intro: string;
  endpoints: LlmEndpoint[];
}) {
  const config = useQuery(llmScenarioOptions(scenario));

  if (config.isPending) return <Loading rows={3} />;
  if (config.isError) {
    return (
      <ErrorState
        title={`拉取${title}配置失败`}
        error={config.error}
        onRetry={() => void config.refetch()}
      />
    );
  }
  return (
    <ScenarioForm
      scenario={scenario}
      title={title}
      intro={intro}
      config={config.data}
      endpoints={endpoints}
    />
  );
}

function LlmSection() {
  const endpoints = useQuery(llmEndpointsOptions());
  const [creating, setCreating] = useState(false);

  return (
    <>
      <article style={CARD}>
        <header style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>LLM 端点</h2>
          {!creating ? (
            <button type="button" data-variant="primary" onClick={() => setCreating(true)}>
              新建端点
            </button>
          ) : null}
        </header>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          任何 OpenAI 兼容或 Anthropic 格式的端点都行，包括本地 Ollama 和自建 vLLM。
          「测试连接」会发一次真实调用——它测的是这条路真的通，不是 base_url 能解析。
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
      </article>

      <ScenarioSection
        scenario="market"
        title="行情分析"
        intro="喂给模型的是已聚合的统计量（分位数、降价排行、供应量趋势、离开观测范围时长），不是原始商品列表。入口在行情分析页，只在你点击时才调用。"
        endpoints={endpoints.data ?? []}
      />
      <ScenarioSection
        scenario="item"
        title="单品建议"
        intro="喂给模型的是单件商品、它的价格史、卖家画像、收藏备注，以及它所属关键词的行情统计。入口在商品详情页，只在你点击时才调用。"
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
  const script = mint.data ? buildBookmarklet(window.location.origin, mint.data.ticket) : "";
  // An https goofish page cannot fetch a plain-http panel -- mixed content,
  // blocked in the browser, nothing the backend can send fixes it. localhost
  // and 127.0.0.1 are the exception browsers make. Detectable exactly, so the
  // page says so up front instead of letting the click fail as a TypeError.
  const blockedByMixedContent =
    window.location.protocol === "http:" &&
    !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

  return (
    <div
      style={{
        border: "1px dashed var(--border-strong)",
        borderRadius: "var(--radius-sm)",
        padding: "var(--space-3)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        fontSize: 12.5,
      }}
    >
      <strong>快捷方式：书签脚本</strong>
      <p className="muted" style={{ margin: 0 }}>
        生成一个书签拖到书签栏，之后在<strong>已登录的闲鱼页面</strong>上点它，就会把
        <span className="mono"> document.cookie </span>
        直接送回面板，不用再走开发者工具。
      </p>
      {/* Say what it sends. The script now also reads the browser environment
          so the collector stops claiming to be a different machine than the
          one the cookies came from; that is worth one sentence rather than a
          surprise for anyone who reads the script below. */}
      <p className="muted" style={{ margin: 0 }}>
        它同时会捎上这台浏览器的<strong>环境信息</strong>（UA、语言、时区、屏幕尺寸），让采集
        请求和凭证来自同一台机器。都是页面上已经能读到的只读属性，不做任何额外探测。
      </p>
      {blockedByMixedContent ? (
        <p className="muted" style={{ margin: 0 }}>
          <strong>这台面板用不了书签脚本</strong>：它开在明文 http 的
          <span className="mono"> {window.location.host} </span>
          上，而闲鱼页面是 https——浏览器不允许 https 页面去 fetch http 地址，后端加什么头都
          绕不过去。请用下面的开发者工具流程，或者给面板配上 https。
        </p>
      ) : null}
      <button
        type="button"
        onClick={() => mint.mutate()}
        disabled={mint.isPending || blockedByMixedContent}
        style={{ alignSelf: "flex-start" }}
      >
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
            {formatDateTime(mint.data.expires_at)} 过期（约 10 分钟）。用过或过期后再点一次
            「重新生成」换一张——旧书签会明确告诉你是过期还是已用过。票据本身是密钥，别贴给别人。
          </p>
          {/* Same rule as everywhere else: the import is not the result. And
              the "unusable" this leaves behind is expected, not a failure --
              without saying so here the page contradicts docs/operations.md. */}
          <p style={{ margin: 0 }}>
            导入成功只代表 cookie 收下了。<strong>去「监控任务」点一次「立即运行」</strong>
            ，跑通了才算恢复。
          </p>
          <p className="muted" style={{ margin: 0 }}>
            书签导入后上面会显示「会话不可用 ·<span className="mono"> no _m_h5_tk </span>
            」，这是<strong>正常的</strong>：签名 token 只存在于 taobao 域，闲鱼页面上读不到，
            由下一个周期的浏览器兜底去领。那个周期会慢一些、只看第 1 页，之后就恢复正常。
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

/** Session health, credential import, credential removal, and the LLM
 *  configuration the PRD deferred until something could verify it.
 *
 *  Importing a cookie header is the one step that cannot be automated: a
 *  headless container cannot solve a slider, so the login state has to come
 *  from a human's own browser. Without this screen the tool cannot be
 *  deployed at all.
 *
 *  The LLM block waited for P4 on purpose (`docs/m1-report.md`: 做一个存了也
 *  无从验证的表单比没有更糟). It ships now because there is something behind
 *  every control: a test call, a real model list, two triggers that consume
 *  what is saved here.
 */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const session = useQuery(sessionOptions());
  const [paste, setPaste] = useState("");
  const [confirmingClear, setConfirmingClear] = useState(false);

  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.session });
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
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <h1>设置</h1>

      <article style={CARD}>
        <h2>采集会话</h2>

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
              <div
                role="alert"
                style={{
                  background: "var(--warn-bg)",
                  border: "1px solid var(--warn)",
                  borderRadius: "var(--radius-sm)",
                  padding: "var(--space-3)",
                  fontSize: 13,
                }}
              >
                <strong style={{ color: "var(--warn)" }}>需要人工验证。</strong>{" "}
                上游对这些接口出了风控挑战：
                <span className="mono"> {state.challenged_apis.join("、")}</span>
                。自动重试不会好转，请按下面的步骤在自己的浏览器里过一次验证，再重新导入 cookie。
              </div>
            ) : !state.usable ? (
              <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>
                没有可用会话，采集只能走浏览器兜底或直接失败。
              </p>
            ) : state.proven ? (
              <p style={{ margin: 0, fontSize: 13, color: "var(--success)" }}>
                会话可用，最近一次采集成功于 {formatRelativeTime(state.last_success_at)}。
              </p>
            ) : (
              /* The distinction that matters: importing a cookie clears the
                 challenge map unconditionally, so "no challenge" right after
                 an import only means nothing has failed YET. Reporting that as
                 a healthy session sent a user chasing a working panel while
                 the detail endpoint was still blocked. */
              <div
                style={{
                  background: "var(--surface-2)",
                  border: "1px solid var(--border-strong)",
                  borderRadius: "var(--radius-sm)",
                  padding: "var(--space-3)",
                  fontSize: 13,
                }}
              >
                <strong>凭证已导入，但还没有被验证过。</strong>{" "}
                导入只是收下了 cookie；要等一次真实采集成功，这里才会变成「会话可用」。
                去「监控任务」页对任一规则点「立即运行」，或等下一轮调度。
              </div>
            )}

            <dl
              style={{
                margin: 0,
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "2px 12px",
                fontSize: 12.5,
              }}
            >
              <dt className="muted">来源</dt>
              <dd style={{ margin: 0 }}>{state.origin ?? "—"}</dd>
              <dt className="muted">建立于</dt>
              <dd style={{ margin: 0 }}>
                {state.established_at
                  ? `${formatDateTime(state.established_at)}（${formatRelativeTime(state.established_at)}）`
                  : "—"}
              </dd>
              <dt className="muted">最近错误</dt>
              <dd style={{ margin: 0, wordBreak: "break-word" }}>{state.last_error ?? "—"}</dd>
              <dt className="muted">已导入 cookie</dt>
              {/* Names only. The values never leave the backend. */}
              <dd className="mono" style={{ margin: 0, wordBreak: "break-all" }}>
                {state.cookie_names.length > 0 ? state.cookie_names.join(" ") : "—"}
              </dd>
              <dt className="muted">采集端身份</dt>
              {/* A summary line, never the snapshot. Same rule as the cookie
                  list: hardwareConcurrency / deviceMemory and the rest stay in
                  the backend -- rendering them here would hand a page's worth
                  of fingerprint to anything that can read this panel. */}
              <dd style={{ margin: 0, wordBreak: "break-word" }}>
                {state.fingerprint ?? "内置默认值（开发者工具导入不带环境信息）"}
                {state.fingerprint && !state.fingerprint_applied ? (
                  <span className="muted">
                    {" "}
                    ——<strong>已记录，但没有采用</strong>
                    。这份快照来自移动端浏览器，而本工具驱动的每一个页面和接口都是 PC 版（
                    <span className="mono">pc.search</span> /{" "}
                    <span className="mono">pc.detail</span>
                    ）。带着手机 UA 去请求 PC 接口，是把一种不一致换成更糟的一种，所以采集仍
                    然用内置默认值。想让它生效，请在电脑浏览器上重新点一次书签。
                  </span>
                ) : null}
              </dd>
            </dl>
          </>
        ) : null}

        <BookmarkletBlock />

        <form
          onSubmit={(event) => {
            event.preventDefault();
            const value = paste.trim();
            if (value) doImport.mutate(value);
          }}
          style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}
        >
          <label htmlFor="cookie-paste">导入 cookie（开发者工具，最稳的一条路）</label>
          <textarea
            id="cookie-paste"
            value={paste}
            onChange={(event) => setPaste(event.target.value)}
            rows={4}
            placeholder="cookie2=…; unb=…; _m_h5_tk=…"
            style={{ fontFamily: "var(--font-mono)", fontSize: 12, resize: "vertical" }}
            aria-describedby="cookie-help"
          />
          <div id="cookie-help" className="muted" style={{ fontSize: 11.5 }}>
            <p style={{ margin: 0 }}>
              在自己的浏览器里登录闲鱼，开发者工具 → Network → 任一请求 → 复制整个
              <span className="mono"> Cookie </span>
              请求头，粘贴到这里。值只存在后端，页面上永远只显示 cookie 名字。
            </p>
            {/* Copy the header from the request being reproduced, rather than
                hunting for a particular cookie name. Which cookie carries a
                passed verification was never actually measured here -- an
                early research note guessed `x5sec`, a user passed a slider and
                got no such cookie. The request's own header sidesteps the
                question: it is by definition the complete credential set that
                endpoint receives, on the right domain. */}
            <p style={{ margin: "var(--space-2) 0 0" }}>
              <strong>只登录往往不够</strong>
              ，风控挑战出现在<strong>商品详情</strong>那条路上。所以：先在浏览器里打开一个
              商品详情页（<span className="mono">goofish.com/item?id=…</span>），出现滑块就
              完成它，刷新，然后在 Network 里过滤
              <span className="mono"> detail </span>
              ，找到
              <span className="mono"> mtop.taobao.idle.pc.detail </span>
              这个请求，复制<strong>它的</strong>
              <span className="mono"> Cookie </span>
              请求头。
            </p>
            <p style={{ margin: "var(--space-1) 0 0" }}>
              为什么要指定这个请求：它的 Cookie 头按定义就是详情端点实际收到的完整凭证集，
              域也一定是对的，不需要判断哪个 cookie 名字才是关键。
            </p>
          </div>
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <button
              type="submit"
              data-variant="primary"
              disabled={doImport.isPending || !paste.trim()}
            >
              {doImport.isPending ? "导入中…" : "导入"}
            </button>
            {confirmingClear ? (
              <>
                <button
                  type="button"
                  data-variant="danger"
                  onClick={() => doClear.mutate()}
                  disabled={doClear.isPending}
                >
                  确认清除
                </button>
                <button type="button" onClick={() => setConfirmingClear(false)}>
                  取消
                </button>
              </>
            ) : (
              <button
                type="button"
                data-variant="danger"
                onClick={() => setConfirmingClear(true)}
                disabled={!state?.cookie_names.length}
              >
                清除凭证
              </button>
            )}
          </div>
          {doImport.isError ? <ErrorState title="导入失败" error={doImport.error} /> : null}
          {doClear.isError ? <ErrorState title="清除失败" error={doClear.error} /> : null}
          {doImport.isSuccess ? (
            <p style={{ margin: 0, fontSize: 12.5, color: "var(--success)" }}>
              已导入。若上面仍显示没有可用会话，通常是缺少
              <span className="mono"> _m_h5_tk </span>
              ——采集器会自己取一个，等一轮再看。
            </p>
          ) : null}
        </form>
      </article>

      <article style={CARD}>
        <h2>面板访问</h2>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          面板用的是后端启动时的
          <span className="mono"> SFD_API_TOKEN </span>
          ，只能在服务端改。这里只能忘掉浏览器里存的那一份。
        </p>
        <button
          type="button"
          onClick={() => {
            clearToken();
            window.location.reload();
          }}
          style={{ alignSelf: "flex-start" }}
        >
          忘掉本机 token 并退出
        </button>
      </article>

      <LlmSection />
    </section>
  );
}
