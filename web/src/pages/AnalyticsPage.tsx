import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import {
  analyzeMarket,
  listingDurationOptions,
  llmScenarioOptions,
  monitorsOptions,
  priceDistributionOptions,
  priceDropsOptions,
  supplyTrendOptions,
  type AnalyticsQuery,
} from "../api/queries";
import DailyBars from "../components/DailyBars";
import DistroChart from "../components/DistroChart";
import { Icon, type IconName } from "../components/Icon";
import LlmPanel, { LLM_WAIT_NOTE } from "../components/LlmPanel";
import { PageHero } from "../components/PageHero";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import {
  apertureNote,
  legacyClockNote,
  formatChangeRatio,
  formatDuration,
  formatPrice,
  formatRelativeTime,
  shortTitle,
} from "../lib/format";
import { useReveal } from "../lib/fx";
import { marketReading, scenarioReady,
  billingNote,
} from "../lib/llm";

/** Market analysis for one keyword.
 *
 *  One window selector drives all four blocks, and `days` genuinely means
 *  something different in each of them — which listings count, what "before"
 *  means, how wide the chart is, which first sightings are in scope. That is
 *  not an inconsistency to paper over, so every block states its own reading
 *  of the window instead of leaving the user to assume one.
 *
 *  `MarketPanel` is the one control here that spends money, so it sits at the
 *  top and never fires on its own.
 */

const WINDOWS = [7, 30, 90];
const DEFAULT_DAYS = 30;
const DROP_LIMIT = 20;

const CREATE_RULE = <Link to="/monitors">去建一条监控规则</Link>;


/** One stat card. The `note` slot is the prototype's short mono fact (sample
 *  counts); the long 口径 sentence goes in `caption` because each block reads
 *  the window differently and has to say so in words.
 */
function Block({
  icon,
  title,
  note,
  caption,
  children,
}: {
  icon: IconName;
  title: string;
  note?: string;
  caption?: string;
  children: React.ReactNode;
}) {
  // Reveal on the stable wrapper only: pending/error/data all render through
  // this same <section>, so the decorative fade never replays on data arrival.
  const reveal = useReveal();
  return (
    <section className="card" data-reveal ref={reveal}>
      <div className="card-h">
        <h2>
          <Icon name={icon} size={15} />
          {title}
        </h2>
        {note === undefined ? null : <span className="note">{note}</span>}
      </div>
      {caption === undefined ? null : (
        <p className="muted" style={{ fontSize: 12.5, marginBottom: "var(--space-3)" }}>
          {caption}
        </p>
      )}
      {children}
    </section>
  );
}

/** The five quantiles as the prototype's stat strip. Values are real text
 *  (not pixels), and the chart below keeps its full data table.
 *
 *  Structural on purpose: `PriceQuantiles` (cents) and `DurationQuantiles`
 *  (minutes) are two generated schemas with the same shape, and the caller's
 *  `format` is what knows the unit.
 */
type Quantiles = {
  p10?: number | null;
  p25?: number | null;
  p50?: number | null;
  p75?: number | null;
  p90?: number | null;
};

function QuantileRow({
  quantiles: q,
  format,
  meaning,
}: {
  quantiles: Quantiles;
  format: (value: number | null | undefined) => string;
  meaning: string;
}) {
  const cells: [string, number | null | undefined][] = [
    ["P10", q.p10],
    ["P25", q.p25],
    ["中位 P50", q.p50],
    ["P75", q.p75],
    ["P90", q.p90],
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <div className="q-row">
        {cells.map(([label, value]) => (
          <span className="q-item" key={label}>
            <span className="q-k">{label}</span>
            <span
              className="q-v"
              style={label === "中位 P50" ? { color: "var(--acc2)" } : undefined}
            >
              {format(value)}
            </span>
          </span>
        ))}
      </div>
      <p className="dim" style={{ fontSize: 11.5, margin: 0 }}>
        {meaning}
      </p>
    </div>
  );
}

function DistributionBlock({ query }: { query: AnalyticsQuery }) {
  const dist = useQuery(priceDistributionOptions(query));

  if (dist.isPending) {
    return (
      <Block icon="bar-chart-2" title="价格分布">
        <Loading rows={4} />
      </Block>
    );
  }
  if (dist.isError) {
    return (
      <Block icon="bar-chart-2" title="价格分布">
        <ErrorState title="拉取价格分布失败" error={dist.error} onRetry={() => dist.refetch()} />
      </Block>
    );
  }

  const { quantiles: q, sample_size, fresh_size, data_days } = dist.data;
  const caption = `口径：最近 ${query.days} 天内还被看到过的商品，每件只取最新一次报价 · 已收集 ${data_days} 天 · 样本 ${sample_size} 件，其中 ${fresh_size} 件最近两轮仍在售`;

  if (sample_size === 0) {
    return (
      <Block icon="bar-chart-2" title="价格分布" note="0 SAMPLES" caption={caption}>
        <Empty
          message={`最近 ${query.days} 天里没有这个关键词的在售报价。换更长的窗口，或等下一轮采集。`}
        />
      </Block>
    );
  }

  return (
    <Block
      icon="bar-chart-2"
      title="价格分布"
      note={`${sample_size} SAMPLES · KDE & RUG`}
      caption={caption}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        {/* Two samples is where statistics.quantiles starts working, so one
            listing is an ordinary day-one state and not an error. Saying so
            beats printing five dashes and letting the user guess. */}
        {sample_size < 2 ? (
          <p className="muted" style={{ fontSize: 13, margin: 0 }}>
            只有 1 件样本，给不出分位数——下面这一档就是它本身。
          </p>
        ) : (
          <QuantileRow
            quantiles={q}
            format={formatPrice}
            meaning="一半的商品报价低于中位数。"
          />
        )}
        <DistroChart
          samples={dist.data.samples}
          sampleSize={sample_size}
          buckets={dist.data.histogram.map((b) => ({
            lo: b.lo_cents,
            hi: b.hi_cents,
            count: b.count,
          }))}
          format={formatPrice}
          label="价格"
          median={q.p50 ?? null}
        />
      </div>
    </Block>
  );
}

function DropsBlock({ query }: { query: AnalyticsQuery }) {
  const drops = useQuery(priceDropsOptions({ ...query, limit: DROP_LIMIT }));

  if (drops.isPending) {
    return (
      <Block icon="trending-down" title="降价排行">
        <Loading rows={4} />
      </Block>
    );
  }
  if (drops.isError) {
    return (
      <Block icon="trending-down" title="降价排行">
        <ErrorState title="拉取降价排行失败" error={drops.error} onRetry={() => drops.refetch()} />
      </Block>
    );
  }

  const { rows, sample_size, data_days } = drops.data;
  const caption = `口径：拿 ${query.days} 天前的报价和现价比 · 已收集 ${data_days} 天 · ${sample_size} 件商品两个时点都有报价，其中 ${rows.length} 件降了价。窗口内才上架的没有旧价，不参与。`;
  const deepest = Math.max(...rows.map((row) => row.drop_bps), 1);

  if (rows.length === 0) {
    return (
      <Block
        icon="trending-down"
        title="降价排行"
        note={`0 / ${sample_size} DROPS`}
        caption={caption}
      >
        <Empty
          message={
            sample_size === 0
              ? `还没有 ${query.days} 天前的报价可比——这个窗口内的商品都是新看到的。`
              : `${sample_size} 件商品在这个窗口里都没有降价。`
          }
        />
      </Block>
    );
  }

  return (
    <Block
      icon="trending-down"
      title="降价排行"
      note={`TOP ${rows.length} / ${sample_size}`}
      caption={caption}
    >
      {/* A ranking, not a chart: rows read better than bars when the question
          is "which one", and the inline bar carries the magnitude. */}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>商品</th>
              <th>价格变化</th>
              <th aria-label="降幅比例条" />
              <th style={{ textAlign: "right" }}>降幅</th>
              <th>最后见到</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.item_id}>
                <td>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <RemoteImage
                      src={row.cover_url}
                      alt={shortTitle(row.title)}
                      width={40}
                      height={40}
                    />
                    <div style={{ minWidth: 0 }}>
                      <Link
                        className="t2l"
                        to={`/items/${row.item_id}`}
                        title={row.title}
                        style={{ maxWidth: 200, fontSize: 13 }}
                      >
                        {row.title}
                      </Link>
                      {row.is_fresh ? null : (
                        // Not a colour-only hint: without the words, a user
                        // clicks through to a listing that is already gone.
                        <span className="pill" data-tone="danger">
                          <Icon name="x" size={11} />
                          已离开观测范围
                        </span>
                      )}
                    </div>
                  </div>
                </td>
                <td className="mono" style={{ whiteSpace: "nowrap", fontSize: 12.5 }}>
                  <span className="dim">{formatPrice(row.then_cents)}</span>
                  {" → "}
                  <span style={{ fontWeight: 600, fontSize: 13 }}>
                    {formatPrice(row.now_cents)}
                  </span>
                </td>
                <td>
                  <div className="bar-track" aria-hidden="true">
                    <div
                      className="bar"
                      style={{ width: `${Math.max((row.drop_bps / deepest) * 100, 6)}%` }}
                    />
                  </div>
                </td>
                <td className="num" style={{ color: "var(--green)", fontWeight: 600 }}>
                  {formatChangeRatio(-row.drop_bps / 10000)}
                </td>
                <td className="dim" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {formatRelativeTime(row.last_seen_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Block>
  );
}

function TrendBlock({ query }: { query: AnalyticsQuery }) {
  const trend = useQuery(supplyTrendOptions(query));

  if (trend.isPending) {
    return (
      <Block icon="activity" title="供应量趋势">
        <Loading rows={4} />
      </Block>
    );
  }
  if (trend.isError) {
    return (
      <Block icon="activity" title="供应量趋势">
        <ErrorState
          title="拉取供应量趋势失败"
          error={trend.error}
          onRetry={() => trend.refetch()}
        />
      </Block>
    );
  }

  const { days, sample_size, data_days } = trend.data;
  const caption = `口径：横轴就是这 ${query.days} 天 · 已收集 ${data_days} 天 · 窗口内新增 ${sample_size} 件。「没有新货」和「我们没在看」是两回事，图上分开画。`;

  if (days.length === 0) {
    return (
      <Block icon="activity" title="供应量趋势" note="NO DATA" caption={caption}>
        <Empty message="这个关键词没有任何采集记录。" action={CREATE_RULE} />
      </Block>
    );
  }

  return (
    <Block
      icon="activity"
      title="供应量趋势"
      note="NEW ITEMS PER DAY · UTC"
      caption={caption}
    >
      <DailyBars days={days} />
    </Block>
  );
}

function DurationBlock({ query }: { query: AnalyticsQuery }) {
  const duration = useQuery(listingDurationOptions(query));

  if (duration.isPending) {
    return (
      <Block icon="hourglass" title="离开观测范围的时长">
        <Loading rows={4} />
      </Block>
    );
  }
  if (duration.isError) {
    return (
      <Block icon="hourglass" title="离开观测范围的时长">
        <ErrorState
          title="拉取离开观测范围时长失败"
          error={duration.error}
          onRetry={() => duration.refetch()}
        />
      </Block>
    );
  }

  const {
    quantiles: q,
    histogram,
    sample_size,
    data_days,
    aperture_pages_min,
    aperture_pages_max,
    aperture_rows,
    legacy_clock_rows,
  } = duration.data;
  const caption = `口径：首次见到落在最近 ${query.days} 天内、且已经连续两轮没再出现的商品，量的是「首次见到 → 最后见到」这段时间 · 已收集 ${data_days} 天 · 样本 ${sample_size} 件`;

  // The aperture is the reason this metric exists in this shape rather than as
  // a market number, so it is stated in words above the chart every time. The
  // two cases that make the distribution unreadable -- it changed, or we
  // cannot say what it was -- come back as `caveat` and get the emphasised
  // box rather than the muted line.
  const { aperture, caveat } = apertureNote(aperture_pages_min, aperture_pages_max, aperture_rows);
  // A second caveat with a different lifetime: the aperture one is about how
  // wide a net we cast, this one about which clock stopped. It disappears on
  // its own as new cycles stamp MonitorHit.last_hit_at, so it is not worth a
  // dismiss control.
  const legacy = legacyClockNote(legacy_clock_rows, sample_size);

  return (
    <Block
      icon="hourglass"
      title="离开观测范围的时长（≈卖多快）"
      note={sample_size === 0 ? "0 SAMPLES" : `${sample_size} SAMPLES · KDE & RUG`}
      caption={caption}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {aperture}
        </p>
        {caveat === null ? null : (
          // Not a --warn pill: that colour is reserved for risk control and a
          // degraded collector (styling-guidelines.md). This is a caveat about
          // what the numbers can mean, and it has to be readable in words
          // rather than inferred from a hue.
          <p
            style={{
              fontSize: 13,
              fontWeight: 600,
              margin: 0,
              padding: "var(--space-2) var(--space-3)",
              border: "1px solid var(--border-strong)",
              borderRadius: "var(--radius)",
            }}
          >
            {caveat}
          </p>
        )}
        {legacy === null ? null : (
          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            {legacy}
          </p>
        )}
        {sample_size === 0 ? (
          // Two different empties. "Nothing has left yet" is a fact about the
          // listings; "no history at all" is a fact about the rule, and one of
          // them is fixed by waiting while the other is not.
          <Empty
            message={
              data_days === 0
                ? "这个关键词没有任何采集记录。"
                : `最近 ${query.days} 天里首次见到的商品还都在搜索结果里，没有「已经离开」的可以统计。等它们掉出观测范围，或者换更长的窗口。`
            }
            action={data_days === 0 ? CREATE_RULE : undefined}
          />
        ) : (
          <>
            {sample_size < 2 ? (
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                只有 1 件样本，给不出分位数——下面这一档就是它本身。
              </p>
            ) : (
              <QuantileRow
                quantiles={q}
                format={formatDuration}
                meaning="一半的商品在中位数这么久之后就不再出现了。"
              />
            )}
            <DistroChart
              samples={duration.data.samples}
              sampleSize={sample_size}
              buckets={histogram.map((b) => ({
                lo: b.lo_minutes,
                hi: b.hi_minutes,
                count: b.count,
              }))}
              format={formatDuration}
              label="离开观测范围的时长"
              median={q.p50 ?? null}
            />
          </>
        )}
      </div>
    </Block>
  );
}

/** The market-analysis trigger (FR-P4-3).
 *
 *  User-triggered only, and it stays that way: the mutation fires from a
 *  click and nothing here refetches. A `useQuery` would re-run on window
 *  focus, and every run is a billed call.
 *
 *  Repeat clicks inside the same window are free — the server caches on a
 *  digest of the rendered prompt, so a hit says so in the footer rather than
 *  silently looking like a fresh answer.
 */
function MarketPanel({ query }: { query: AnalyticsQuery }) {
  const config = useQuery(llmScenarioOptions("market"));
  const analyze = useMutation({ mutationFn: analyzeMarket });
  const result = analyze.data ?? null;
  const reading = marketReading(result?.reading);
  const ready = scenarioReady(config.data);

  // Hidden, not disabled, when the scenario is off or unconfigured. The
  // contract is explicit (overall-design prd.md:259: "默认关闭；场景未配置或未
  // 启用时对应入口整体隐藏，不得报错"), and the reasoning holds: the feature
  // ships OFF, so a permanently dead button on this page is what most users
  // would see forever. Discovery belongs on the settings page, next to the
  // switch that turns it on.
  //
  // `config.isPending` is not "not ready" -- rendering nothing and then
  // popping a panel in is worse than waiting one tick.
  if (config.isPending) return null;
  if (!ready) return null;

  return (
    <LlmPanel
      title="AI 行情解读"
      intro={
        <>
          把下面这些统计量（分位数、降价排行、供应量趋势、离开观测范围时长）交给模型，
          让它回答「现在什么水位、该等还是该出手」。模型看不到原始商品列表。{LLM_WAIT_NOTE}
        </>
      }
      runLabel={`分析「${query.keyword}」最近 ${query.days} 天`}
      onRun={() => analyze.mutate({ keyword: query.keyword, days: query.days })}
      pending={analyze.isPending}
      error={analyze.error}
      errorTitle="行情分析失败"
      result={result}
      meta={
        result === null
          ? null
          : `口径：关键词「${result.keyword}」最近 ${result.window_days} 天 · 已收集 ${result.data_days} 天 · 样本 ${result.sample_size} 件 · ${billingNote(
              result.cached,
              result.calls,
            )}`
      }
    >
      {reading === null ? (
        // `ok` with an unreadable body should not render an empty card. The
        // model's own text is the honest fallback.
        <pre
          className="mono"
          style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}
        >
          {result?.text}
        </pre>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          {/* No tone on these pills. The model answers in free text -- 「略偏低」
              is a good answer (`market.MarketReading` keeps them as str for
              that reason) -- so any colour mapping would be guessing, and
              colour never carries meaning alone here anyway. */}
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <span className="pill">水位 {reading.level || "—"}</span>
            <span className="pill">趋势 {reading.trend || "—"}</span>
            <span className="pill">建议 {reading.advice || "—"}</span>
          </div>
          <p style={{ margin: 0, fontSize: 13.5, whiteSpace: "pre-wrap" }}>{reading.summary}</p>
          {reading.reasons.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: "var(--space-4)", fontSize: 13 }}>
              {reading.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </LlmPanel>
  );
}

export default function AnalyticsPage() {
  const [params, setParams] = useSearchParams();
  const monitors = useQuery(monitorsOptions());

  if (monitors.isPending) return <Loading rows={6} />;
  if (monitors.isError) {
    return (
      <ErrorState
        title="拉取监控规则失败"
        error={monitors.error}
        onRetry={() => monitors.refetch()}
      />
    );
  }

  // Seller rules have no keyword (`keyword` is null on them), and every
  // number on this page is defined against a keyword's search results. So
  // they are dropped here rather than rendered as a blank option: a picker
  // entry that produces a 422 is worse than one that is absent.
  const keywords = [
    ...new Set(monitors.data.flatMap((m) => (m.keyword === null ? [] : [m.keyword]))),
  ];
  // `||`, not `??`: a hand-edited or truncated `?keyword=` gives the empty
  // string, which the API rejects with a 422 (keyword is min_length=1) and the
  // page would answer with three error banners instead of a chart.
  const keyword = params.get("keyword") || keywords[0];

  if (keyword === undefined) {
    return (
      <Empty message="还没有监控规则，所以没有关键词可以分析。" action={CREATE_RULE} />
    );
  }

  // A shared link can name a keyword whose rule has since been deleted. The
  // ledger outlives the rule and the API answers for it, so the option stays
  // selectable and the page says why it is not in the list.
  const orphan = !keywords.includes(keyword);
  const rawDays = Number(params.get("days"));
  const days = WINDOWS.includes(rawDays) ? rawDays : DEFAULT_DAYS;
  const query: AnalyticsQuery = { keyword, days };

  function update(key: string, value: string) {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  }

  return (
    <>
      <PageHero
        eyebrow={`ANALYTICS DECK · ${days}-DAY ROLLING WINDOW`}
        ghost="MARKET"
        title={
          <>
            行情分析<span className="thin"> / {shortTitle(keyword)}</span>
          </>
        }
        meta={
          <>
            <span>
              <Icon name="calendar" size={12} />
              最近 {days} 天 · 日界 UTC
            </span>
            <span>
              <Icon name="database" size={12} />
              {keywords.length} 个关键词可选
            </span>
            {orphan ? (
              <span>
                <Icon name="info" size={12} />
                「{shortTitle(keyword)}」的规则已删除，历史数据仍可查
              </span>
            ) : null}
          </>
        }
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <form className="filterbar" onSubmit={(e) => e.preventDefault()}>
          <label className="field">
            <span className="field-label">监控关键词</span>
            <select value={keyword} onChange={(e) => update("keyword", e.target.value)}>
              {(orphan ? [keyword, ...keywords] : keywords).map((kw) => (
                <option key={kw} value={kw}>
                  {kw}
                  {orphan && kw === keyword ? "（规则已删除）" : ""}
                </option>
              ))}
            </select>
          </label>

          <div className="field">
            <span className="field-label">统计窗口（天）</span>
            {/* Segmented control, not a select: three options, chosen often,
                and the whole set should be visible without opening a popup. */}
            <div className="seg" role="group" aria-label="统计窗口（天）">
              {WINDOWS.map((n) => (
                <button
                  key={n}
                  type="button"
                  className="seg-btn"
                  aria-pressed={days === n}
                  onClick={() => update("days", String(n))}
                >
                  {n} 天
                </button>
              ))}
            </div>
          </div>

          <div className="spacer" />

          {/* The one caveat that applies to every number below. A keyword is
              not a product category: these are the listings OUR searches saw,
              not the market. */}
          <p className="filterbar-note">
            <Icon name="info" size={13} />
            <span>
              以下数字都来自「{keyword}
              」这个关键词的搜索结果，包含被规则价格区间和排除词挡掉的商品——市场是市场，规则是规则。
              不是整个闲鱼。日期与日界均为 UTC。四块内容里「{days}
              天」的含义各不相同，见每块自己的口径。
            </span>
          </p>
        </form>

        {/* Above the numbers it reads, not below them: it is a reading OF the
            four blocks, and burying the trigger at the bottom of a long scroll
            hides the one control on this page that costs money. */}
        <MarketPanel query={query} />

        <div className="grid-2">
          <DistributionBlock query={query} />
          <DropsBlock query={query} />
          <TrendBlock query={query} />
          <DurationBlock query={query} />
        </div>
      </div>
    </>
  );
}
