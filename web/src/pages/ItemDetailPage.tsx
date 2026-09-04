import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import {
  addWatch,
  itemOptions,
  itemPricesOptions,
  keys,
} from "../api/queries";
import PriceChart from "../components/PriceChart";
import RemoteImage from "../components/RemoteImage";
import SellerProfile from "../components/SellerProfile";
import { ErrorState, Loading } from "../components/States";
import { formatDateTime, formatPrice, formatRelativeTime } from "../lib/format";

const GOOFISH_ITEM = "https://www.goofish.com/item?id=";

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

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <nav style={{ fontSize: 12 }}>
        <Link to="/items">← 返回命中列表</Link>
      </nav>

      <header style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        <h1>{data.title}</h1>
        <div
          style={{
            display: "flex",
            gap: "var(--space-4)",
            flexWrap: "wrap",
            alignItems: "baseline",
          }}
        >
          <span className="mono" style={{ fontSize: 22, fontWeight: 600 }}>
            {formatPrice(data.price_cents)}
          </span>
          {data.status === "on_sale" ? (
            <span className="pill" data-tone="success">
              在售
            </span>
          ) : (
            <span className="pill" data-tone="danger">
              {data.status === "sold" ? "已售" : "已下架"}
            </span>
          )}
          <span className="muted" style={{ fontSize: 12 }}>
            {data.region ?? "地区未知"} · 发布 {formatRelativeTime(data.publish_time)} · 首次入库{" "}
            {formatDateTime(data.first_seen_at)}
          </span>
        </div>

        {(data.unverified_filters ?? []).length > 0 ? (
          <p
            role="note"
            style={{
              margin: 0,
              fontSize: 12.5,
              color: "var(--text)",
              background: "var(--warn-bg)",
              border: "1px solid var(--warn)",
              borderRadius: "var(--radius-sm)",
              padding: "var(--space-2)",
            }}
          >
            <strong style={{ color: "var(--warn)" }}>保守放行：</strong>
            {(data.unverified_filters ?? []).join("、")}
            —— 这些筛选条件因数据缺失未能验证，命中不代表真的满足。
          </p>
        ) : null}

        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <a
            href={`${GOOFISH_ITEM}${data.id}`}
            target="_blank"
            rel="noreferrer noopener"
            style={{
              display: "inline-flex",
              alignItems: "center",
              minHeight: 32,
              padding: "var(--space-1) var(--space-3)",
              borderRadius: "var(--radius-sm)",
              background: "var(--primary)",
              color: "var(--primary-on)",
              fontWeight: 500,
              textDecoration: "none",
            }}
          >
            去闲鱼查看
          </a>
          <button type="button" onClick={() => watch.mutate()} disabled={watch.isPending}>
            {watch.isPending ? "加入中…" : watch.isSuccess ? "已加入收藏" : "加入收藏并追踪降价"}
          </button>
        </div>
        {watch.isError ? <ErrorState title="加入收藏失败" error={watch.error} /> : null}
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 2fr) minmax(240px, 1fr)",
          gap: "var(--space-4)",
          alignItems: "start",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", minWidth: 0 }}>
          <RemoteImage src={cover} alt={data.title} width="100%" height={360} />
          {data.image_urls.length > 1 ? (
            <div style={{ display: "flex", gap: "var(--space-2)", overflowX: "auto" }}>
              {data.image_urls.map((url) => (
                <button
                  key={url}
                  type="button"
                  onClick={() => setHero(url)}
                  aria-label="查看这张图"
                  style={{ padding: 0, border: "none", background: "none", minHeight: 0 }}
                >
                  <RemoteImage src={url} alt={data.title} width={72} height={72} />
                </button>
              ))}
            </div>
          ) : null}

          {data.description ? (
            <p style={{ whiteSpace: "pre-wrap", fontSize: 13.5, wordBreak: "break-word" }}>
              {data.description}
            </p>
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

        <SellerProfile seller={data} />
      </div>
    </section>
  );
}
