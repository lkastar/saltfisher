import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import {
  listingDurationOptions,
  monitorsOptions,
  priceDistributionOptions,
  priceDropsOptions,
  supplyTrendOptions,
  type AnalyticsQuery,
} from "../api/queries";
import DailyBars from "../components/DailyBars";
import Histogram from "../components/Histogram";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import {
  apertureNote,
  legacyClockNote,
  formatChangeRatio,
  formatDuration,
  formatPrice,
  formatRelativeTime,
} from "../lib/format";

/** Market analysis for one keyword.
 *
 *  One window selector drives all four blocks, and `days` genuinely means
 *  something different in each of them — which listings count, what "before"
 *  means, how wide the chart is, which first sightings are in scope. That is
 *  not an inconsistency to paper over, so every block states its own reading
 *  of the window instead of leaving the user to assume one.
 *
 *  No LLM button. M4 owns that, and a disabled placeholder promises a feature
 *  that does not exist.
 */

const WINDOWS = [7, 30, 90];
const DEFAULT_DAYS = 30;
const DROP_LIMIT = 20;

const CREATE_RULE = <Link to="/">去建一条监控规则</Link>;

/** Long titles reach 250+ characters in real captures; an aria-label built
 *  from one reads the whole listing before saying what the control is.
 */
function shortTitle(title: string): string {
  return title.length > 18 ? `${title.slice(0, 18)}…` : title;
}

function Block({
  title,
  note,
  children,
}: {
  title: string;
  note: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      <h2>{title}</h2>
      <p className="muted" style={{ fontSize: 12 }}>
        {note}
      </p>
      {children}
    </section>
  );
}

function DistributionBlock({ query }: { query: AnalyticsQuery }) {
  const dist = useQuery(priceDistributionOptions(query));

  if (dist.isPending) return <Loading rows={4} />;
  if (dist.isError) {
    return (
      <ErrorState title="拉取价格分布失败" error={dist.error} onRetry={() => dist.refetch()} />
    );
  }

  const { quantiles: q, sample_size, fresh_size, data_days } = dist.data;
  const note = `口径：最近 ${query.days} 天内还被看到过的商品，每件只取最新一次报价 · 已收集 ${data_days} 天 · 样本 ${sample_size} 件，其中 ${fresh_size} 件最近两轮仍在售`;

  if (sample_size === 0) {
    return (
      <Block title="价格分布" note={note}>
        <Empty
          message={`最近 ${query.days} 天里没有这个关键词的在售报价。换更长的窗口，或等下一轮采集。`}
        />
      </Block>
    );
  }

  return (
    <Block title="价格分布" note={note}>
      {/* Two samples is where statistics.quantiles starts working, so one
          listing is an ordinary day-one state and not an error. Saying so
          beats printing five dashes and letting the user guess. */}
      {sample_size < 2 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          只有 1 件样本，给不出分位数——下面这一档就是它本身。
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption
              className="muted"
              style={{
                captionSide: "top",
                textAlign: "left",
                padding: "var(--space-2)",
                fontSize: 12,
              }}
            >
              分位数：一半的商品报价低于中位数。
            </caption>
            <thead>
              <tr>
                <th>P10</th>
                <th>P25</th>
                <th>中位 P50</th>
                <th>P75</th>
                <th>P90</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                {[q.p10, q.p25, q.p50, q.p75, q.p90].map((cents, i) => (
                  <td key={i} className="num" style={{ textAlign: "left" }}>
                    {formatPrice(cents)}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>
      )}
      <Histogram
        buckets={dist.data.histogram.map((b) => ({
          lo: b.lo_cents,
          hi: b.hi_cents,
          count: b.count,
        }))}
        format={formatPrice}
        label="价格"
        median={q.p50 ?? null}
      />
    </Block>
  );
}

function DropsBlock({ query }: { query: AnalyticsQuery }) {
  const drops = useQuery(priceDropsOptions({ ...query, limit: DROP_LIMIT }));

  if (drops.isPending) return <Loading rows={4} />;
  if (drops.isError) {
    return <ErrorState title="拉取降价排行失败" error={drops.error} onRetry={() => drops.refetch()} />;
  }

  const { rows, sample_size, data_days } = drops.data;
  const note = `口径：拿 ${query.days} 天前的报价和现价比 · 已收集 ${data_days} 天 · ${sample_size} 件商品两个时点都有报价，其中 ${rows.length} 件降了价。窗口内才上架的没有旧价，不参与。`;
  const deepest = Math.max(...rows.map((row) => row.drop_bps), 1);

  if (rows.length === 0) {
    return (
      <Block title="降价排行" note={note}>
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
    <Block title="降价排行" note={note}>
      {/* A ranking, not a chart: rows read better than bars when the question
          is "which one", and the inline bar carries the magnitude. */}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th />
              <th>商品</th>
              <th style={{ textAlign: "right" }}>窗口初价</th>
              <th style={{ textAlign: "right" }}>现价</th>
              <th style={{ textAlign: "right" }}>降幅</th>
              <th>最后见到</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.item_id}>
                <td>
                  <RemoteImage src={row.cover_url} alt={shortTitle(row.title)} width={40} height={40} />
                </td>
                <td style={{ maxWidth: 320 }}>
                  <Link
                    to={`/items/${row.item_id}`}
                    title={row.title}
                    style={{
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                      wordBreak: "break-word",
                    }}
                  >
                    {row.title}
                  </Link>
                  {row.is_fresh ? null : (
                    // Not a colour-only hint: without the words, a user
                    // clicks through to a listing that is already gone.
                    <span className="pill" data-tone="danger" style={{ marginTop: 2 }}>
                      已离开观测范围
                    </span>
                  )}
                </td>
                <td className="num muted">{formatPrice(row.then_cents)}</td>
                <td className="num" style={{ fontWeight: 600 }}>
                  {formatPrice(row.now_cents)}
                </td>
                <td className="num" style={{ color: "var(--success)", fontWeight: 600 }}>
                  {formatChangeRatio(-row.drop_bps / 10000)}
                  <div
                    aria-hidden="true"
                    style={{
                      marginTop: 3,
                      height: 4,
                      borderRadius: 2,
                      background: "var(--success)",
                      width: `${Math.max((row.drop_bps / deepest) * 100, 6)}%`,
                      marginLeft: "auto",
                    }}
                  />
                </td>
                <td className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
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

  if (trend.isPending) return <Loading rows={4} />;
  if (trend.isError) {
    return <ErrorState title="拉取供应量趋势失败" error={trend.error} onRetry={() => trend.refetch()} />;
  }

  const { days, sample_size, data_days } = trend.data;
  const note = `口径：横轴就是这 ${query.days} 天 · 已收集 ${data_days} 天 · 窗口内新增 ${sample_size} 件。「没有新货」和「我们没在看」是两回事，图上分开画。`;

  if (days.length === 0) {
    return (
      <Block title="供应量趋势" note={note}>
        <Empty message="这个关键词没有任何采集记录。" action={CREATE_RULE} />
      </Block>
    );
  }

  return (
    <Block title="供应量趋势" note={note}>
      <DailyBars days={days} />
    </Block>
  );
}

function DurationBlock({ query }: { query: AnalyticsQuery }) {
  const duration = useQuery(listingDurationOptions(query));

  if (duration.isPending) return <Loading rows={4} />;
  if (duration.isError) {
    return (
      <ErrorState
        title="拉取离开观测范围时长失败"
        error={duration.error}
        onRetry={() => duration.refetch()}
      />
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
  const note = `口径：首次见到落在最近 ${query.days} 天内、且已经连续两轮没再出现的商品，量的是「首次见到 → 最后见到」这段时间 · 已收集 ${data_days} 天 · 样本 ${sample_size} 件`;

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
    <Block title="离开观测范围的时长" note={note}>
      <p className="muted" style={{ fontSize: 12 }}>
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
            <p className="muted" style={{ fontSize: 13 }}>
              只有 1 件样本，给不出分位数——下面这一档就是它本身。
            </p>
          ) : (
            <div className="table-scroll">
              <table>
                <caption
                  className="muted"
                  style={{
                    captionSide: "top",
                    textAlign: "left",
                    padding: "var(--space-2)",
                    fontSize: 12,
                  }}
                >
                  分位数：一半的商品在中位数这么久之后就不再出现了。
                </caption>
                <thead>
                  <tr>
                    <th>P10</th>
                    <th>P25</th>
                    <th>中位 P50</th>
                    <th>P75</th>
                    <th>P90</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    {[q.p10, q.p25, q.p50, q.p75, q.p90].map((minutes, i) => (
                      <td key={i} className="num" style={{ textAlign: "left" }}>
                        {formatDuration(minutes)}
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
          )}
          <Histogram
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
    </Block>
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

  const keywords = [...new Set(monitors.data.map((m) => m.keyword))];
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
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <h1>行情分析</h1>

      <form
        onSubmit={(e) => e.preventDefault()}
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-3)",
          alignItems: "flex-end",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          padding: "var(--space-3)",
        }}
      >
        <label
          htmlFor="analytics-keyword"
          style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}
        >
          关键词
          <select
            id="analytics-keyword"
            value={keyword}
            onChange={(e) => update("keyword", e.target.value)}
          >
            {(orphan ? [keyword, ...keywords] : keywords).map((kw) => (
              <option key={kw} value={kw}>
                {kw}
                {orphan && kw === keyword ? "（规则已删除）" : ""}
              </option>
            ))}
          </select>
        </label>

        <label
          htmlFor="analytics-days"
          style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}
        >
          时间窗口
          <select
            id="analytics-days"
            value={days}
            onChange={(e) => update("days", e.target.value)}
          >
            {WINDOWS.map((n) => (
              <option key={n} value={n}>
                最近 {n} 天
              </option>
            ))}
          </select>
        </label>
      </form>

      {/* The one caveat that applies to every number below. A keyword is not
          a product category: these are the listings OUR searches saw, not the
          market. */}
      <p className="muted" style={{ fontSize: 12 }}>
        以下数字都来自「{keyword}」这个关键词的搜索结果，包含被规则价格区间和排除词挡掉的商品——市场是市场，规则是规则。
        不是整个闲鱼。日期与日界均为 UTC。四块内容里「{days} 天」的含义各不相同，见每块自己的口径。
      </p>

      <DistributionBlock query={query} />
      <DropsBlock query={query} />
      <TrendBlock query={query} />
      <DurationBlock query={query} />
    </div>
  );
}
