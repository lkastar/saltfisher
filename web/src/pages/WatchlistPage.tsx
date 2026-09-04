import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import {
  addWatch,
  deleteWatch,
  keys,
  updateWatch,
  watchlistOptions,
  type WatchEntry,
} from "../api/queries";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import {
  formatChangeRatio,
  formatPrice,
  formatRelativeTime,
} from "../lib/format";

const MIN_INTERVAL = 60;

function Change({ entry }: { entry: WatchEntry }) {
  const cheaper = entry.change_cents < 0;
  if (entry.change_cents === 0) return <span className="muted">持平</span>;
  return (
    <span
      // Colour only accelerates reading this; the arrow and the number carry
      // the meaning. Green means cheaper here, the inverse of the red-up
      // convention a Chinese user brings with them.
      style={{ color: cheaper ? "var(--success)" : "var(--danger)", fontWeight: 600 }}
    >
      {formatChangeRatio(entry.change_ratio)}
      <span className="muted" style={{ fontWeight: 400 }}>
        {" "}
        ({cheaper ? "-" : "+"}
        {formatPrice(Math.abs(entry.change_cents))})
      </span>
    </span>
  );
}

function AddByLink() {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const add = useMutation({
    mutationFn: (url: string) => addWatch({ url, interval_seconds: 300 }),
    onSuccess: () => {
      setValue("");
      void queryClient.invalidateQueries({ queryKey: keys.watchlist });
    },
  });

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        const url = value.trim();
        if (url) add.mutate(url);
      }}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "var(--space-3)",
      }}
    >
      <label htmlFor="watch-url">粘贴闲鱼链接或分享文案</label>
      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <input
          id="watch-url"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="https://www.goofish.com/item?id=… 或 App 里的分享文案"
          style={{ flex: 1, minWidth: 260 }}
        />
        <button type="submit" data-variant="primary" disabled={add.isPending || !value.trim()}>
          {add.isPending ? "抓取中…" : "加入收藏"}
        </button>
      </div>
      <span className="muted" style={{ fontSize: 11.5 }}>
        加入时会立刻抓一次，作为后续比价的基准价。短链和混着中文的分享文案都能识别。
      </span>
      {add.isError ? <ErrorState title="加入失败" error={add.error} /> : null}
    </form>
  );
}

/** Real merchant titles run past 250 characters. Putting one in an aria-label
 *  makes a screen reader read the entire listing before it says which control
 *  this is, so the label identifies the row with a short prefix instead.
 */
function shortTitle(title: string): string {
  return title.length > 18 ? `${title.slice(0, 18)}…` : title;
}

function NoteEditor({ entry }: { entry: WatchEntry }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState(entry.note ?? "");
  const save = useMutation({
    mutationFn: (value: string) => updateWatch(entry.item_id, { note: value }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.watchlist }),
  });
  const dirty = note !== (entry.note ?? "");

  return (
    <div style={{ display: "flex", gap: "var(--space-1)", alignItems: "center" }}>
      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        maxLength={500}
        placeholder="备注"
        style={{ width: 160 }}
        aria-label={`${shortTitle(entry.title)} 的备注`}
      />
      {dirty ? (
        <button type="button" onClick={() => save.mutate(note)} disabled={save.isPending}>
          保存
        </button>
      ) : null}
    </div>
  );
}

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const query = useQuery(watchlistOptions());
  const [confirming, setConfirming] = useState<string | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.watchlist });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof updateWatch>[1] }) =>
      updateWatch(id, body),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: deleteWatch,
    onSuccess: () => {
      setConfirming(null);
      void invalidate();
    },
  });

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <h1>收藏与降价追踪</h1>
      <AddByLink />

      {query.isPending ? <Loading rows={4} /> : null}
      {query.isError ? (
        <ErrorState
          title="拉取收藏夹失败"
          error={query.error}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {patch.isError ? <ErrorState title="保存失败" error={patch.error} /> : null}
      {remove.isError ? <ErrorState title="移出失败" error={remove.error} /> : null}

      {query.data?.length === 0 ? (
        <Empty message="收藏夹是空的。贴一个链接就能开始追踪它的价格。" />
      ) : null}

      {query.data && query.data.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th style={{ width: 56 }} />
                <th>商品</th>
                <th style={{ textAlign: "right" }}>现价</th>
                <th style={{ textAlign: "right" }}>收藏价</th>
                <th style={{ textAlign: "right" }}>变化</th>
                <th>状态</th>
                <th style={{ textAlign: "right" }}>在售</th>
                <th>备注</th>
                <th>降价监控</th>
                <th>上次检查</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {query.data.map((entry) => (
                <tr key={entry.item_id}>
                  <td>
                    <RemoteImage
                      src={entry.cover_url}
                      alt={entry.title}
                      width={48}
                      height={48}
                    />
                  </td>
                  <td style={{ maxWidth: 300 }}>
                    <Link
                      to={`/items/${entry.item_id}`}
                      title={entry.title}
                      style={{
                        display: "-webkit-box",
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: "vertical",
                        overflow: "hidden",
                        wordBreak: "break-word",
                      }}
                    >
                      {entry.title}
                    </Link>
                    <div className="muted" style={{ fontSize: 11 }}>
                      {entry.seller_nick}
                      {entry.seller_is_shop ? " · 商家" : ""}
                    </div>
                  </td>
                  <td className="num" style={{ fontSize: 15, fontWeight: 600 }}>
                    {formatPrice(entry.price_cents)}
                  </td>
                  <td className="num muted">{formatPrice(entry.added_price_cents)}</td>
                  <td className="num">
                    <Change entry={entry} />
                  </td>
                  <td>
                    {entry.status === "on_sale" ? (
                      <span className="pill" data-tone="success">
                        在售
                      </span>
                    ) : (
                      <span className="pill" data-tone="danger">
                        {entry.status === "sold" ? "已售" : "已下架"}
                      </span>
                    )}
                  </td>
                  <td className="num">{entry.listed_days.toFixed(1)}天</td>
                  <td>
                    <NoteEditor entry={entry} />
                  </td>
                  <td>
                    <label
                      style={{ display: "flex", gap: "var(--space-1)", alignItems: "center" }}
                    >
                      <input
                        type="checkbox"
                        checked={entry.price_watch_enabled}
                        onChange={(event) =>
                          patch.mutate({
                            id: entry.item_id,
                            body: { price_watch_enabled: event.target.checked },
                          })
                        }
                      />
                      <input
                        type="number"
                        min={MIN_INTERVAL}
                        value={entry.interval_seconds}
                        onChange={(event) => {
                          const seconds = Number(event.target.value);
                          if (seconds >= MIN_INTERVAL) {
                            patch.mutate({
                              id: entry.item_id,
                              body: { interval_seconds: seconds },
                            });
                          }
                        }}
                        style={{ width: 82 }}
                        aria-label={`${shortTitle(entry.title)} 的检查间隔（秒，下限 ${MIN_INTERVAL}）`}
                      />
                    </label>
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {formatRelativeTime(entry.last_run_at)}
                    {entry.last_error ? (
                      <div>
                        <span className="pill" data-tone="warn" title={entry.last_error}>
                          检查报错
                        </span>
                      </div>
                    ) : null}
                  </td>
                  <td>
                    {confirming === entry.item_id ? (
                      <div style={{ display: "flex", gap: "var(--space-1)" }}>
                        <button
                          type="button"
                          data-variant="danger"
                          onClick={() => remove.mutate(entry.item_id)}
                          disabled={remove.isPending}
                        >
                          确认
                        </button>
                        <button type="button" onClick={() => setConfirming(null)}>
                          取消
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        data-variant="danger"
                        onClick={() => setConfirming(entry.item_id)}
                      >
                        移出
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
