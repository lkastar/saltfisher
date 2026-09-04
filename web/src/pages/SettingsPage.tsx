import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { clearToken } from "../api/client";
import { clearCookies, importCookies, keys, sessionOptions } from "../api/queries";
import { ErrorState, Loading } from "../components/States";
import { formatDateTime, formatRelativeTime } from "../lib/format";

/** Session health, credential import, credential removal.
 *
 *  Importing a cookie header is the one step that cannot be automated: a
 *  headless container cannot solve a slider, so the login state has to come
 *  from a human's own browser. Without this screen the tool cannot be
 *  deployed at all.
 *
 *  The LLM endpoint and per-scenario prompt configuration that the PRD lists
 *  is deliberately NOT here yet: nothing reads it until M4, and a form that
 *  stores settings no code consumes -- with no way to test a connection or
 *  list models -- looks functional while doing nothing.
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

      <article
        style={{
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          background: "var(--surface)",
          padding: "var(--space-4)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-3)",
        }}
      >
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
            </dl>
          </>
        ) : null}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            const value = paste.trim();
            if (value) doImport.mutate(value);
          }}
          style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}
        >
          <label htmlFor="cookie-paste">导入 cookie</label>
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

      <article
        style={{
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          background: "var(--surface)",
          padding: "var(--space-4)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
        }}
      >
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

      <p className="muted" style={{ fontSize: 11.5 }}>
        LLM 端点与场景提示词配置留到 M4：现在没有任何代码读它，也没有「测试连接」和
        「拉取模型列表」，配了也无从验证。
      </p>
    </section>
  );
}
