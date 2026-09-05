import { useEffect, useState } from "react";

import { isFailure, showsRawText, type LlmResult } from "../lib/llm";
import { ErrorState } from "./States";

/** The trigger button and the result area, shared by the market panel on the
 *  analytics page and the advice panel on an item page.
 *
 *  Presentational, like everything else in `components/`: the two pages own
 *  their mutation and hand the outcome down. What is shared is not the call,
 *  it is the four things that are easy to get wrong about rendering one.
 *
 *  ### 1. A minute of nothing is not a spinner
 *
 *  Measured on the live endpoint: 59s for a market reading that parsed first
 *  time, 152s for one that needed the server's single retry, 6s for item
 *  advice. `States.tsx`'s `Loading` waits 200ms before showing anything,
 *  which is right for a 40ms list and wrong here -- and a button that looks
 *  inert for a minute gets clicked again, with every click billed. So the
 *  button disables itself, says how long this takes before it is pressed, and
 *  counts the seconds while it waits.
 *
 *  ### 2. The message is the server's
 *
 *  `starved`, `empty` and `unparsable` are all HTTP 200 and each carries its
 *  own wording. They are printed as given, never rewritten here: a starved
 *  answer must say "raise max_tokens" and must never say "check your prompt",
 *  which would send the user to edit a template that was never the problem.
 *
 *  ### 3. `unparsable` shows the model's own words
 *
 *  Verbatim, in a `<pre>`. A user breaking their own template is a normal
 *  event and the raw text is the only place the mistake is visible.
 *
 *  ### 4. The disclaimer travels with the answer
 *
 *  It comes from the response (`prompts.DISCLAIMER`) and renders whenever
 *  there is model output to disclaim.
 */

/** How long the two calls take, in words, before anyone waits for one.
 *
 *  The retry is named because it doubles both numbers. Measured on the live
 *  endpoint: a market reading that parsed on the first draw took 59s, and one
 *  whose first draw failed validation took 100s + 52s = 152s and billed twice
 *  (`complete_structured` retries once, deliberately). A note promising
 *  "30-60 秒" and then going quiet for two and a half minutes is the same
 *  broken-looking button this panel exists to avoid.
 */
export const LLM_WAIT_NOTE =
  "调用通常要 30–60 秒；模型第一次输出不合格式时服务端会自动重试一次，那就要两倍时间。期间不要重复点击——每次点击都是一次真实计费。";

type LlmPanelProps = {
  title: string;
  /** What this button will do and what it costs. Rendered above it. */
  intro: React.ReactNode;
  runLabel: string;
  onRun: () => void;
  pending: boolean;
  /** True when there is nothing to analyse -- an empty keyword, a listing
   *  with no data. NOT used for an unconfigured scenario: the contract says
   *  that entry is hidden entirely, so the panel never renders at all. */
  disabled?: boolean;
  /** A thrown failure: unreachable backend, 409 unconfigured, 502 upstream.
   *  Distinct from a `result` whose `kind` is not "ok" -- that one is a
   *  successful request reporting what the model did. */
  error: unknown;
  errorTitle: string;
  result: LlmResult | null;
  /** Facts about this particular run: which keyword, how many images, whether
   *  it came from the cache. Rendered under the answer. */
  meta?: React.ReactNode;
  /** The `ok` body, built by the page from its own answer shape. */
  children?: React.ReactNode;
};

/** The waiting state, mounted only while a call is in flight.
 *
 *  Its own component so the elapsed counter starts at zero by mounting
 *  rather than by resetting state from an effect. The count is the only
 *  honest progress indicator available: nothing streams, so there is no
 *  fraction to report, and a bar animating on its own would keep promising
 *  progress at second 50.
 */
function PendingNotice() {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    // role="status" and not the shared Loading skeleton: this wait is a
    // minute long and has to be announced, not implied by grey bars that
    // appear 200ms in.
    <div
      role="status"
      aria-busy="true"
      style={{
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius)",
        padding: "var(--space-3)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        fontSize: 13,
      }}
    >
      <strong>正在调用模型，已等待 {seconds} 秒。</strong>
      <span className="muted">
        {LLM_WAIT_NOTE}实测：行情分析一次过约 59 秒、需要重试时到过 152 秒，单品建议约 6 秒。
      </span>
      {/* ponytail: a counter and no progress bar. Nothing streams, so any bar
          here would be elapsed time wearing a progress costume -- and with a
          retry in play the honest denominator is 152s, which means a bar
          sitting full for 90 seconds. The number that only counts up cannot
          make a promise it has to break. */}
    </div>
  );
}

export default function LlmPanel({
  title,
  intro,
  runLabel,
  onRun,
  pending,
  disabled = false,
  error,
  errorTitle,
  result,
  meta,
  children,
}: LlmPanelProps) {
  const failed = result !== null && isFailure(result.kind);

  return (
    <section
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
      <header style={{ display: "flex", gap: "var(--space-3)", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>{title}</h2>
      </header>

      <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
        {intro}
      </p>

      <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center", flexWrap: "wrap" }}>
        <button
          type="button"
          data-variant="primary"
          onClick={onRun}
          disabled={pending || disabled}
        >
          {pending ? "生成中…" : runLabel}
        </button>
      </div>

      {pending ? <PendingNotice /> : null}

      {error !== null && error !== undefined ? (
        <ErrorState title={errorTitle} error={error} />
      ) : null}

      {result !== null && !pending ? (
        <>
          {result.message ? (
            // The server's wording, printed as given. `no_data` is not a
            // failure -- no call was made and nothing was billed -- so it
            // does not get the danger colour.
            <div
              role={failed ? "alert" : "note"}
              style={{
                border: `1px solid var(--${failed ? "danger" : "border-strong"})`,
                background: failed ? "var(--danger-bg)" : "transparent",
                color: failed ? "var(--danger)" : "var(--text)",
                borderRadius: "var(--radius)",
                padding: "var(--space-3)",
                fontSize: 13,
              }}
            >
              {result.message}
            </div>
          ) : null}

          {showsRawText(result.kind, result.text) ? (
            <pre
              className="mono"
              style={{
                margin: 0,
                padding: "var(--space-3)",
                background: "var(--surface-2)",
                borderRadius: "var(--radius-sm)",
                fontSize: 12,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: 320,
                overflow: "auto",
              }}
            >
              {result.text}
            </pre>
          ) : null}

          {result.kind === "ok" ? children : null}

          {meta ? (
            <p className="muted" style={{ margin: 0, fontSize: 12 }}>
              {meta}
            </p>
          ) : null}

          {/* Only where there is model output to disclaim. Printing it under
              "no listings, so nothing was called" would claim an inference
              that was never made. */}
          {result.kind === "ok" || result.kind === "unparsable" ? (
            <p className="muted" style={{ margin: 0, fontSize: 11.5 }}>
              {result.disclaimer}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
