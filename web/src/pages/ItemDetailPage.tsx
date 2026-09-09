import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import {
  addWatch,
  analyzeItem,
  itemOptions,
  itemPricesOptions,
  keys,
  llmScenarioOptions,
  refreshSeller,
} from "../api/queries";
import { Icon } from "../components/Icon";
import LlmPanel, { LLM_WAIT_NOTE } from "../components/LlmPanel";
import PriceChart from "../components/PriceChart";
import RemoteImage from "../components/RemoteImage";
import SellerProfile from "../components/SellerProfile";
import { ErrorState, Loading } from "../components/States";
import { StatusPill } from "../components/StatusPill";
import {
  displayTitle,
  formatDateTime,
  formatPrice,
  formatRelativeTime,
} from "../lib/format";
import { itemAdvice, scenarioReady } from "../lib/llm";

const GOOFISH_ITEM = "https://www.goofish.com/item?id=";

/** Anchors get none of the button baseline (inline-flex centering, gap), so
 *  the primary CTA link declares its own. Buttons must NOT use this — the
 *  base.css button rule already provides it.
 */
const LINK_BUTTON: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
};

/** The single-item advice trigger (FR-P4-4).
 *
 *  Never cached, on either side: the price and the seller's state are what
 *  this answer turns on, so a stored verdict on a listing that has since
 *  dropped ¥800 is worse than no verdict.
 *
 *  `keyword` is echoed because the choice is not obvious — a listing can sit
 *  in two keywords' ledgers at different price levels, and the backend picks
 *  the rule that saw it first. `notes` are the inputs that did NOT make it
 *  in: an answer that silently dropped the photos looks exactly like one that
 *  read them.
 */
function AdvicePanel({ itemId }: { itemId: string }) {
  const config = useQuery(llmScenarioOptions("item"));
  const analyze = useMutation({ mutationFn: () => analyzeItem(itemId) });
  const result = analyze.data ?? null;
  const advice = itemAdvice(result?.data);
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
      title="AI 单品出价建议"
      intro={
        <>
          把这件商品的资料、价格历史、卖家画像、收藏备注和同关键词的行情统计交给模型，
          让它回答值不值得、出多少、有什么风险。{LLM_WAIT_NOTE}
        </>
      }
      runLabel="生成单品建议"
      onRun={() => analyze.mutate()}
      pending={analyze.isPending}
      error={analyze.error}
      errorTitle="生成单品建议失败"
      result={result}
      meta={
        result === null ? null : (
          <>
            {result.keyword === null
              ? "这件商品不在任何关键词的台账里，所以这条建议没有行情统计做参照。"
              : `参照关键词「${result.keyword}」的同期行情（最早发现它的那条规则）。`}
            {` 发出图片 ${result.images_sent} 张。`}
            {result.notes.length > 0 ? ` ${result.notes.join(" ")}` : ""}
          </>
        )
      }
    >
      {advice === null ? (
        <pre
          className="mono"
          style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}
        >
          {result?.text}
        </pre>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          {/* Untoned pill for the verdict, like the market panel: the model
              answers in free text and 「谨慎考虑」 is a good answer. */}
          <div
            style={{
              display: "flex",
              gap: "var(--space-2)",
              flexWrap: "wrap",
              alignItems: "center",
            }}
          >
            <span className="pill">结论 {advice.verdict || "—"}</span>
            <span className="pill" data-tone="acc">
              <Icon name="target" size={11} />
              合理价 {formatPrice(advice.fairPriceCents)}
            </span>
            <span className="pill" data-tone="acc">
              <Icon name="tag" size={11} />
              建议出价 {formatPrice(advice.offerPriceCents)}
            </span>
          </div>
          <p style={{ margin: 0, fontSize: 13.5, whiteSpace: "pre-wrap" }}>{advice.summary}</p>
          {advice.risks.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: "var(--space-4)", fontSize: 13 }}>
              {advice.risks.map((risk) => (
                <li key={risk}>{risk}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </LlmPanel>
  );
}

export default function ItemDetailPage() {
  const { itemId = "" } = useParams();
  const [params] = useSearchParams();
  const monitorParam = params.get("monitor_id");
  const monitorId = monitorParam === null ? undefined : Number(monitorParam);
  const queryClient = useQueryClient();

  const item = useQuery(itemOptions(itemId, monitorId));
  const prices = useQuery(itemPricesOptions(itemId));
  const [hero, setHero] = useState<string | null>(null);

  const watch = useMutation({
    mutationFn: () => addWatch({ item_id: itemId, interval_seconds: 300 }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.watchlist }),
  });

  // On-demand seller-profile fetch (prd round-2 item 5). The seller fields
  // ride on ItemPublic, so re-syncing THIS item's query is what updates the
  // card. onSettled, not onSuccess: a failed refresh may still have stored a
  // partial profile, and returning the promise keeps isPending honest until
  // the fresh item arrives (hook-guidelines).
  const refresh = useMutation({
    mutationFn: refreshSeller,
    onSettled: () => queryClient.invalidateQueries({ queryKey: keys.item(itemId) }),
  });

  // Auto-fetch the seller profile once when it is clearly unfetched: every
  // field a profile fetch fills is null. The ref makes this fire-once per
  // mount — a refetch or an errored auto-attempt never re-fires it (the
  // manual 刷新画像 button stays for retries and stale profiles). An effect
  // triggering a MUTATION is deliberate; the useEffect+fetch ban is about
  // reads, which stay in TanStack Query.
  const autoFetched = useRef(false);
  const seller = item.data;
  useEffect(() => {
    if (autoFetched.current || !seller?.seller_id) return;
    if (
      seller.seller_credit_level == null &&
      seller.seller_sold_count == null &&
      seller.seller_is_shop == null
    ) {
      autoFetched.current = true;
      refresh.mutate(seller.seller_id);
    }
  }, [seller, refresh]);

  if (item.isPending) return <Loading rows={6} />;
  if (item.isError) {
    return (
      <ErrorState
        title="拉取商品失败"
        error={item.error}
        onRetry={() => void item.refetch()}
      />
    );
  }

  const data = item.data;
  const cover = hero ?? data.cover_url ?? data.image_urls[0] ?? null;
  // Seller-card-collected items store the long description AS the title (no
  // short title exists upstream), so the headline is derived: first clause,
  // capped. Whenever displayTitle shortened it, the FULL text must stay
  // reachable on the page — the description card carries it. (round-2)
  const heroTitle = displayTitle(data.title);
  const descriptionBody =
    data.description || (heroTitle !== data.title ? data.title : null);

  return (
    <>
      <Link className="crumb" to="/items">
        <Icon name="arrow-left" size={13} />
        返回命中列表
      </Link>

      {/* item-hero: page-specific layout per the prototype, so it stays inline
          rather than growing base.css a one-caller class. */}
      <header style={{ margin: "var(--space-5) 0 var(--space-4)" }}>
        <div className="hero-eyebrow">ITEM DETAIL · ID: {data.id}</div>
        {/* Clean one-line derived headline; the full stored title travels in
            title= and, when shortened, in the description card below. */}
        <h1
          title={data.title}
          style={{ fontSize: "clamp(22px, 2.6vw, 32px)", lineHeight: 1.25 }}
        >
          {heroTitle}
        </h1>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 16,
            flexWrap: "wrap",
            margin: "12px 0 8px",
          }}
        >
          <span className="mono" style={{ fontSize: "clamp(32px, 3.2vw, 44px)", fontWeight: 600 }}>
            {formatPrice(data.price_cents)}
          </span>
          <StatusPill status={data.status} />
        </div>
        <div className="dim mono" style={{ fontSize: 12 }}>
          {data.region ?? "地区未知"} · 发布 {formatRelativeTime(data.publish_time)} · 首次入库{" "}
          {formatDateTime(data.first_seen_at)}
          {prices.data ? ` · 观测 ${prices.data.length} 次` : ""}
        </div>
      </header>

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {(data.unverified_filters ?? []).length > 0 ? (
          <div className="alert" data-tone="warn" role="note">
            <Icon name="alert-triangle" size={15} />
            <span>
              以下筛选条件因数据缺失未能验证:{" "}
              <strong>{(data.unverified_filters ?? []).join("、")}</strong>
              。命中不代表真的满足全部过滤规则。
            </span>
          </div>
        ) : null}

        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <a
            href={`${GOOFISH_ITEM}${data.id}`}
            target="_blank"
            rel="noreferrer noopener"
            style={{
              ...LINK_BUTTON,
              minHeight: 32,
              padding: "var(--space-1) var(--space-3)",
              borderRadius: "var(--radius-sm)",
              background: "var(--primary)",
              color: "var(--primary-on)",
              fontWeight: 500,
              textDecoration: "none",
            }}
          >
            <Icon name="external-link" size={14} />
            去闲鱼查看原帖
          </a>
          <button
            type="button"
            onClick={() => watch.mutate()}
            disabled={watch.isPending}
          >
            <Icon name="bookmark-plus" size={14} />
            {watch.isPending ? "加入中…" : watch.isSuccess ? "已加入收藏" : "加入收藏并追踪降价"}
          </button>
        </div>
        {watch.isError ? <ErrorState title="加入收藏失败" error={watch.error} /> : null}

        <div className="grid-21">
          {/* min-width:0 comes from the global `.grid-21 > *` rule. */}
          <div className="side-stack">
            <section className="card">
              <RemoteImage src={cover} alt={data.title} width="100%" height={360} />
              {data.image_urls.length > 1 ? (
                <div
                  style={{
                    display: "flex",
                    gap: "var(--space-2)",
                    marginTop: "var(--space-3)",
                    overflowX: "auto",
                  }}
                >
                  {data.image_urls.map((url) => (
                    <button
                      key={url}
                      type="button"
                      onClick={() => setHero(url)}
                      aria-label="查看这张图"
                      aria-pressed={cover === url}
                      style={{
                        padding: 0,
                        border: "none",
                        background: "none",
                        minHeight: 0,
                        borderRadius: "var(--radius-sm)",
                        opacity: cover === url ? 1 : 0.55,
                        // boxShadow, not outline: an inline outline would
                        // override the :focus-visible ring.
                        boxShadow: cover === url ? "0 0 0 1.5px var(--acc)" : undefined,
                      }}
                    >
                      <RemoteImage src={url} alt={data.title} width={64} height={64} />
                    </button>
                  ))}
                </div>
              ) : null}
            </section>

            {descriptionBody !== null ? (
              <section className="card">
                <div className="card-h">
                  <h2>
                    <Icon name="file-text" size={15} />
                    卖家原帖描述
                  </h2>
                  {/* Honest label: this source had no separate description,
                      the body below is the title's full text. */}
                  {data.description ? null : <span className="note">标题全文（该来源无独立描述）</span>}
                </div>
                <div
                  style={{
                    whiteSpace: "pre-wrap",
                    fontSize: 13.5,
                    lineHeight: 1.8,
                    wordBreak: "break-word",
                  }}
                >
                  {descriptionBody}
                </div>
              </section>
            ) : null}

            {prices.isPending ? <Loading rows={3} /> : null}
            {prices.isError ? (
              <ErrorState
                title="拉取价格历史失败"
                error={prices.error}
                onRetry={() => void prices.refetch()}
              />
            ) : null}
            {prices.data ? <PriceChart points={prices.data} /> : null}
          </div>

          <aside>
            <SellerProfile
              seller={data}
              onRefresh={() => refresh.mutate(data.seller_id)}
              refreshing={refresh.isPending}
              refreshError={refresh.error}
              fetchedAt={refresh.data?.fetched_at ?? null}
            />
          </aside>
        </div>

        <AdvicePanel itemId={itemId} />
      </div>
    </>
  );
}
