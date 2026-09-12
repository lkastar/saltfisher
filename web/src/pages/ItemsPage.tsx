import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import {
  channelsOptions,
  createMonitor,
  itemsOptions,
  keys,
  monitorsOptions,
  type Monitor,
} from "../api/queries";
import { Icon } from "../components/Icon";
import { PageHero } from "../components/PageHero";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import { StatusPill } from "../components/StatusPill";
import {
  formatPriceRange,
  formatRelativeTime,
  formatPrice,
  parseUtc,
  parseYuanToCents,
} from "../lib/format";
import {
  nextParams,
  readFilters,
  sellerLabel,
  SORTS,
  STATUSES,
} from "../lib/itemFilters";
import { findSellerRule, sellerRuleName } from "../lib/monitorRules";

const PAGE = 50;

/** The hero title has clamp(38px…) type; an unabridged scope (monitor names
 *  run to 100 chars, real titles far past that) would wrap it into a wall.
 *  Full text stays in the filterbar select / seller row below.
 */
function shortScope(name: string): string {
  return name.length > 24 ? `${name.slice(0, 24)}…` : name;
}

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
  // Bound to a const so the narrowing survives into the click handler below.
  const sellerId = filters.seller_id;

  // "Watch this seller" lives here rather than in the rule form: the id and
  // the nick are already on this page, so no seller picker and no
  // `GET /api/sellers` are needed. See the task prd.
  const queryClient = useQueryClient();
  // Only to give the new rule the same default channels the form's checkboxes
  // have. A rule with no channel collects and then tells nobody, and there is
  // no edit form to attach one afterwards.
  const channels = useQuery(channelsOptions());
  const watch = useMutation({
    mutationFn: createMonitor,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.monitors }),
  });
  // One rule per seller. The backend constrains keyword-xor-seller, not this,
  // so the check is here — off the data the page already has.
  const sellerRule =
    sellerId === undefined ? undefined : findSellerRule(monitors.data ?? [], sellerId);
  // Success and failure belong to the seller they were for: the mutation state
  // outlives a change of filter, and "已创建" left over from the previous
  // seller would be a lie about this one.
  const watched = watch.variables?.seller_id === sellerId;

  // Hero facts, computed only from what is already on screen. 最近入库 is the
  // max over the current page regardless of sort; the unverified count is a
  // page-window fact and says so.
  const rows = items.data ?? [];
  const latestSeen = rows.reduce<string | null>(
    (acc, i) =>
      acc === null || parseUtc(i.first_seen_at).getTime() > parseUtc(acc).getTime()
        ? i.first_seen_at
        : acc,
    null,
  );
  const unverifiedOnPage = showsHitColumns
    ? rows.filter((i) => (i.unverified_filters ?? []).length > 0).length
    : 0;
  const scope =
    selected?.name ??
    (sellerId === undefined ? null : sellerLabel(sellerNick, sellerId));

  return (
    <>
      <PageHero
        eyebrow="MONITOR HIT FEED · REAL-TIME INGEST"
        ghost="HITS"
        title={
          <>
            命中商品
            {scope === null ? null : <span className="thin"> / {shortScope(scope)}</span>}
          </>
        }
        meta={
          <>
            {selected ? (
              <span>
                <Icon name="scan-search" size={12} />
                规则价格区间: {formatPriceRange(selected.price_min_cents, selected.price_max_cents)}
              </span>
            ) : null}
            {selected ? (
              <span>
                <Icon name="check-circle-2" size={12} />
                <span className="ok">命中 {selected.hit_count} 件</span>
              </span>
            ) : null}
            {latestSeen === null ? null : (
              <span>
                <Icon name="clock" size={12} />
                最近入库: {formatRelativeTime(latestSeen)}
              </span>
            )}
            {unverifiedOnPage > 0 ? (
              <span>
                <Icon name="shield-alert" size={12} />
                <span className="warn">本页 {unverifiedOnPage} 件带保守放行标注</span>
              </span>
            ) : null}
          </>
        }
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <form className="filterbar" onSubmit={(e) => e.preventDefault()}>
          <label className="field">
            <span className="field-label">监控任务</span>
            <select
              value={filters.monitor_id ?? ""}
              onChange={(e) => update("monitor_id", e.target.value)}
            >
              <option value="">全部任务</option>
              {(monitors.data ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>

          <div className="field">
            <span className="field-label">价格区间（元）</span>
            <div className="join-pair">
              <input
                type="number"
                min={0}
                className="mono"
                placeholder="下限"
                aria-label="价格下限（元）"
                defaultValue={
                  filters.min_price_cents === undefined ? "" : filters.min_price_cents / 100
                }
                onBlur={(e) => {
                  const cents = parseYuanToCents(e.target.value);
                  update("min_price_cents", cents === null ? undefined : String(cents));
                }}
              />
              <span className="join-sep" aria-hidden="true">
                –
              </span>
              <input
                type="number"
                min={0}
                className="mono"
                placeholder="上限"
                aria-label="价格上限（元）"
                defaultValue={
                  filters.max_price_cents === undefined ? "" : filters.max_price_cents / 100
                }
                onBlur={(e) => {
                  const cents = parseYuanToCents(e.target.value);
                  update("max_price_cents", cents === null ? undefined : String(cents));
                }}
              />
            </div>
          </div>

          <label className="field">
            <span className="field-label">状态</span>
            <select
              value={filters.status ?? ""}
              onChange={(e) => update("status", e.target.value)}
            >
              <option value="">全部状态</option>
              {STATUSES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="field-label">排序</span>
            <select value={filters.sort} onChange={(e) => update("sort", e.target.value)}>
              {SORTS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>

          <div className="spacer" />

          <button
            type="button"
            className="btn-ghost"
            onClick={() => setParams(new URLSearchParams(), { replace: true })}
          >
            <Icon name="rotate-ccw" size={14} />
            清空筛选
          </button>

          {sellerId === undefined ? null : (
            <p
              style={{
                margin: 0,
                flexBasis: "100%",
                display: "flex",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "var(--space-2)",
                fontSize: 12,
              }}
            >
              <span>只看卖家「{sellerLabel(sellerNick, sellerId)}」的商品</span>
              <button type="button" onClick={() => update("seller_id", undefined)}>
                取消卖家筛选
              </button>

              {/* Inline, because this project has no modal and no toast by
                  design (spec/frontend/). */}
              {watched && watch.isSuccess ? (
                <span role="status">
                  {/* Says only what is true. The first draft sent the user to
                      「监控任务」to add a price range and channels -- that page
                      has no edit form at all (MonitorForm is create-only, and
                      the SPA's one updateMonitor call toggles `enabled`), so a
                      seller rule cannot be changed after it is made. Promising
                      it here would send them looking for a control that is not
                      there. */}
                  已创建规则「{watch.data.name}」，已带上当前启用的推送渠道，下个周期开始盯。
                  <Link to="/monitors">去监控任务</Link>
                  看它。<strong>规则建好后改不了</strong>
                  ——要换筛选条件就删掉重建。
                </span>
              ) : sellerRule ? (
                <span>
                  已有规则「{sellerRule.name}」在盯这个卖家，不再重复创建。
                  <Link to="/monitors">去监控任务</Link>
                </span>
              ) : (
                <button
                  type="button"
                  data-variant="primary"
                  // Without the rule list there is no way to tell a duplicate
                  // from a first rule, and guessing creates the second one.
                  disabled={watch.isPending || !monitors.data}
                  title={
                    monitors.data
                      ? "为这个卖家建一条监控规则：盯他的全部在售商品，不按关键词"
                      : "监控任务还没加载出来"
                  }
                  onClick={() =>
                    watch.mutate({
                      name: sellerRuleName(sellerNick, sellerId),
                      seller_id: sellerId,
                      exclude_words: "",
                      exclude_shop: false,
                      // Same default the rule form starts from.
                      interval_seconds: 300,
                      channel_ids: (channels.data ?? [])
                        .filter((ch) => ch.enabled)
                        .map((ch) => ch.id),
                    })
                  }
                >
                  {watch.isPending ? "创建中…" : "盯住这个卖家"}
                </button>
              )}
              {watched && watch.isError ? (
                // The backend's own reason, including the 422 that explains the
                // keyword/seller exclusion. Re-implementing the rule here would
                // give us a second one to keep in sync.
                <span role="alert" style={{ color: "var(--danger)" }}>
                  创建失败：{watch.error.message}
                </span>
              ) : null}
            </p>
          )}

          {!showsHitColumns ? (
            <p className="filterbar-note">
              <Icon name="info" size={13} />
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
              action={
                <Link className="btn-text" to="/monitors">
                  查看任务
                  <Icon name="external-link" size={13} />
                </Link>
              }
            />
          ) : (
            <Empty
              message={
                selected
                  ? `「${selected.name}」暂无符合条件的商品。采集正常，只是还没遇到。`
                  : "还没有采集到商品。"
              }
              action={
                params.toString() === "" ? undefined : (
                  <button
                    type="button"
                    onClick={() => setParams(new URLSearchParams(), { replace: true })}
                  >
                    <Icon name="sliders-horizontal" size={14} />
                    清空筛选
                  </button>
                )
              }
            />
          )
        ) : null}

        {items.data && items.data.length > 0 ? (
          <section className="card">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 56 }}>封面</th>
                    <th>商品标题</th>
                    <th style={{ textAlign: "right" }}>价格</th>
                    <th>卖家</th>
                    <th>地区</th>
                    <th>状态</th>
                    <th>入库时间</th>
                    {showsHitColumns ? <th>标注</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {items.data.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <RemoteImage src={item.cover_url} alt={item.title} width={48} height={48} />
                      </td>
                      <td>
                        <Link
                          className="t2l"
                          to={`/items/${item.id}${
                            filters.monitor_id === undefined ? "" : `?monitor_id=${filters.monitor_id}`
                          }`}
                          title={item.title}
                        >
                          {item.title}
                        </Link>
                      </td>
                      <td className="num" style={{ fontSize: 15, fontWeight: 600 }}>
                        {formatPrice(item.price_cents)}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {item.seller_nick ? (
                          <button
                            type="button"
                            className="btn-text"
                            onClick={() => update("seller_id", item.seller_id)}
                            title="只看这个卖家的商品"
                            style={{ textAlign: "left", whiteSpace: "normal" }}
                          >
                            {item.seller_nick}
                          </button>
                        ) : (
                          // 8 of the 359 sellers in the real db have an empty nick. A
                          // button labelled with it would be an invisible click
                          // target, so those degrade to plain text.
                          <span className="muted">未知卖家</span>
                        )}
                        {item.seller_is_shop ? (
                          <span
                            className="pill"
                            data-tone="acc"
                            style={{ fontSize: 10, padding: "0 6px", marginLeft: 6 }}
                          >
                            商家
                          </span>
                        ) : null}
                      </td>
                      <td className="dim">{item.region ?? "未知"}</td>
                      <td>
                        <StatusPill status={item.status} />
                      </td>
                      <td className="mono muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                        {formatRelativeTime(item.first_seen_at)}
                      </td>
                      {/* Conservatively-passed filters. Without showing them,
                          "let it through when the data is missing" becomes a
                          silent false positive. */}
                      {showsHitColumns ? (
                        <td>
                          {(item.unverified_filters ?? []).length === 0 ? (
                            <span className="dim">–</span>
                          ) : (
                            (item.unverified_filters ?? []).map((label) => (
                              <span
                                key={label}
                                className="pill"
                                data-tone="warn"
                                style={{ marginRight: 4 }}
                              >
                                <Icon name="alert-triangle" size={11} />
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

            <nav className="pager" aria-label="分页">
              <button
                type="button"
                className="pager-btn"
                disabled={(filters.offset ?? 0) === 0}
                onClick={() =>
                  update("offset", String(Math.max(0, (filters.offset ?? 0) - PAGE)), false)
                }
              >
                <Icon name="chevron-left" size={13} />
                上一页
              </button>
              <span className="pager-range">
                {(filters.offset ?? 0) + 1} – {(filters.offset ?? 0) + items.data.length}
              </span>
              <button
                type="button"
                className="pager-btn"
                // No total count from the API, so a full page is the only signal
                // that another one might exist.
                disabled={items.data.length < PAGE}
                onClick={() => update("offset", String((filters.offset ?? 0) + PAGE), false)}
              >
                下一页
                <Icon name="chevron-right" size={13} />
              </button>
            </nav>
          </section>
        ) : null}
      </div>
    </>
  );
}
