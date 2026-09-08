import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import { itemsOptions, monitorsOptions, type Monitor } from "../api/queries";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import { formatPrice, formatRelativeTime, parseYuanToCents } from "../lib/format";
import {
  nextParams,
  readFilters,
  sellerLabel,
  SORTS,
  STATUSES,
} from "../lib/itemFilters";

const PAGE = 50;

export default function ItemsPage() {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const items = useQuery(itemsOptions({ ...filters, limit: PAGE }));
  // Needed for the monitor picker AND for the empty-state fork below: "nothing
  // matched" and "this rule is failing" must not look the same.
  const monitors = useQuery(monitorsOptions());

  function update(key: string, value: string | undefined, resetOffset = true) {
    setParams(nextParams(params, key, value, resetOffset), { replace: true });
  }

  const selected: Monitor | undefined = monitors.data?.find(
    (m) => m.id === filters.monitor_id,
  );
  // "Which filters were waived" is a property of one rule's match, so the API
  // only fills it in when a monitor is named. Showing the column regardless
  // would leave it blank and imply nothing was waived.
  const showsHitColumns = filters.monitor_id !== undefined;
  // The nick is read off the rows already on screen -- naming a seller is not
  // worth a request. An empty result set leaves it undefined; `sellerLabel`
  // then falls back to the id rather than pretending to know the name.
  const sellerNick = items.data?.find(
    (i) => i.seller_id === filters.seller_id,
  )?.seller_nick;

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <h1>命中商品</h1>

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
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          监控任务
          <select
            value={filters.monitor_id ?? ""}
            onChange={(e) => update("monitor_id", e.target.value)}
          >
            <option value="">全部</option>
            {(monitors.data ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          最低价（元）
          <input
            type="number"
            min={0}
            style={{ width: 110 }}
            defaultValue={
              filters.min_price_cents === undefined ? "" : filters.min_price_cents / 100
            }
            onBlur={(e) => {
              const cents = parseYuanToCents(e.target.value);
              update("min_price_cents", cents === null ? undefined : String(cents));
            }}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          最高价（元）
          <input
            type="number"
            min={0}
            style={{ width: 110 }}
            defaultValue={
              filters.max_price_cents === undefined ? "" : filters.max_price_cents / 100
            }
            onBlur={(e) => {
              const cents = parseYuanToCents(e.target.value);
              update("max_price_cents", cents === null ? undefined : String(cents));
            }}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          状态
          <select
            value={filters.status ?? ""}
            onChange={(e) => update("status", e.target.value)}
          >
            <option value="">全部</option>
            {STATUSES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          排序
          <select value={filters.sort} onChange={(e) => update("sort", e.target.value)}>
            {SORTS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>

        <button type="button" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
          清空筛选
        </button>

        {filters.seller_id === undefined ? null : (
          <p
            style={{
              margin: 0,
              flexBasis: "100%",
              display: "flex",
              alignItems: "center",
              gap: "var(--space-2)",
              fontSize: 12,
            }}
          >
            <span>只看卖家「{sellerLabel(sellerNick, filters.seller_id)}」的商品</span>
            <button type="button" onClick={() => update("seller_id", undefined)}>
              取消卖家筛选
            </button>
          </p>
        )}

        {!showsHitColumns ? (
          <p className="muted" style={{ margin: 0, fontSize: 11.5, flexBasis: "100%" }}>
            选中某个监控任务后，会显示该规则命中时「因数据缺失而保守放行」的筛选条件。
          </p>
        ) : null}
      </form>

      {items.isPending ? <Loading rows={5} /> : null}

      {items.isError ? (
        <ErrorState
          title="拉取命中列表失败"
          error={items.error}
          onRetry={() => void items.refetch()}
        />
      ) : null}

      {items.data?.length === 0 ? (
        // The fork the whole page exists for. A rule that stopped collecting
        // has an empty result set too, and calling that "nothing matched"
        // hides the only thing worth reporting.
        selected?.last_error ? (
          <ErrorState
            title={`「${selected.name}」没有命中，因为采集在报错`}
            error={selected.last_error}
            tone="warn"
          />
        ) : (
          <Empty
            message={
              selected
                ? `「${selected.name}」暂无符合条件的商品。采集正常，只是还没遇到。`
                : "还没有采集到商品。"
            }
          />
        )
      ) : null}

      {items.data && items.data.length > 0 ? (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 56 }} />
                  <th>商品</th>
                  <th style={{ textAlign: "right" }}>现价</th>
                  <th>卖家</th>
                  <th>地区</th>
                  <th>状态</th>
                  <th>入库</th>
                  {showsHitColumns ? <th>标注</th> : null}
                </tr>
              </thead>
              <tbody>
                {items.data.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <RemoteImage src={item.cover_url} alt={item.title} width={48} height={48} />
                    </td>
                    <td style={{ maxWidth: 360 }}>
                      <Link
                        to={`/items/${item.id}${
                          filters.monitor_id === undefined ? "" : `?monitor_id=${filters.monitor_id}`
                        }`}
                        title={item.title}
                        // Real merchant listings run to hundreds of
                        // characters -- one of them will otherwise take over
                        // the whole table. Clamped to two lines with the full
                        // text on hover and on the detail page.
                        style={{
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          overflow: "hidden",
                          wordBreak: "break-word",
                        }}
                      >
                        {item.title}
                      </Link>
                    </td>
                    <td className="num" style={{ fontSize: 15, fontWeight: 600 }}>
                      {formatPrice(item.price_cents)}
                    </td>
                    <td>
                      {item.seller_nick ? (
                        <button
                          type="button"
                          onClick={() => update("seller_id", item.seller_id)}
                          title="只看这个卖家的商品"
                          style={{
                            padding: 0,
                            minHeight: 0,
                            border: "none",
                            background: "none",
                            color: "var(--primary)",
                            textAlign: "left",
                            whiteSpace: "normal",
                          }}
                        >
                          {item.seller_nick}
                        </button>
                      ) : (
                        // 8 of the 253 sellers in the real db have an empty nick. A
                        // button labelled with it would be an invisible click
                        // target, so those degrade to plain text.
                        <span className="muted">未知卖家</span>
                      )}
                      {item.seller_is_shop ? (
                        <span className="muted" style={{ fontSize: 11 }}>
                          {" "}
                          · 商家
                        </span>
                      ) : null}
                    </td>
                    <td className="muted">{item.region ?? "未知"}</td>
                    <td>
                      {item.status === "on_sale" ? (
                        <span className="pill" data-tone="success">
                          在售
                        </span>
                      ) : (
                        <span className="pill" data-tone="danger">
                          {item.status === "sold" ? "已售" : "已下架"}
                        </span>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {formatRelativeTime(item.first_seen_at)}
                    </td>
                    {/* Conservatively-passed filters. Without showing them,
                        "let it through when the data is missing" becomes a
                        silent false positive. */}
                    {showsHitColumns ? (
                      <td>
                        {(item.unverified_filters ?? []).length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          (item.unverified_filters ?? []).map((label) => (
                            <span
                              key={label}
                              className="pill"
                              data-tone="warn"
                              style={{ marginRight: 4 }}
                            >
                              {label}
                            </span>
                          ))
                        )}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <nav style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
            <button
              type="button"
              disabled={(filters.offset ?? 0) === 0}
              onClick={() =>
                update("offset", String(Math.max(0, (filters.offset ?? 0) - PAGE)), false)
              }
            >
              上一页
            </button>
            <span className="muted mono" style={{ fontSize: 12 }}>
              {(filters.offset ?? 0) + 1} – {(filters.offset ?? 0) + items.data.length}
            </span>
            <button
              type="button"
              // No total count from the API, so a full page is the only signal
              // that another one might exist.
              disabled={items.data.length < PAGE}
              onClick={() => update("offset", String((filters.offset ?? 0) + PAGE), false)}
            >
              下一页
            </button>
          </nav>
        </>
      ) : null}
    </section>
  );
}
