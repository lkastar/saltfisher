import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type KeyboardEvent } from "react";
import { Link } from "react-router";

import {
  addWatch,
  deleteWatch,
  keys,
  updateWatch,
  watchlistOptions,
  type WatchEntry,
} from "../api/queries";
import { ConfirmInline } from "../components/ConfirmInline";
import { Icon } from "../components/Icon";
import { PageHero } from "../components/PageHero";
import RemoteImage from "../components/RemoteImage";
import { Empty, ErrorState, Loading } from "../components/States";
import { StatusPill } from "../components/StatusPill";
import {
  formatChangeRatio,
  formatPrice,
  formatRelativeTime,
  shortTitle,
} from "../lib/format";

const MIN_INTERVAL = 60;

function Change({ entry }: { entry: WatchEntry }) {
  const cheaper = entry.change_cents < 0;
  if (entry.change_cents === 0) return <span className="dim mono">持平</span>;
  return (
    <span
      className="mono"
      // Colour only accelerates reading this; the arrow and the number carry
      // the meaning. Green means cheaper here, the inverse of the red-up
      // convention a Chinese user brings with them.
      style={{
        color: cheaper ? "var(--green)" : "var(--red)",
        fontWeight: 600,
      }}
    >
      {formatChangeRatio(entry.change_ratio)}
      <span className="dim" style={{ fontWeight: 400 }}>
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
    <section className="card">
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
        }}
      >
        <div
          style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}
        >
          <input
            aria-label="闲鱼链接或分享文案"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="粘贴闲鱼商品链接 (https://www.goofish.com/item?id=…) 或 App 分享文案"
            style={{ flex: 1, minWidth: 260 }}
          />
          <button
            type="submit"
            data-variant="primary"
            disabled={add.isPending || !value.trim()}
          >
            <Icon name="plus" size={14} />
            {add.isPending ? "抓取中…" : "加入追踪"}
          </button>
        </div>
        <span className="field-hint">
          加入时会立刻抓一次，作为后续比价的基准价。短链和混着中文的分享文案都能识别。
        </span>
        {add.isError ? <ErrorState title="加入失败" error={add.error} /> : null}
      </form>
    </section>
  );
}

/* 意图备注 and 降价轮询 edit independently, one cell at a time. Editors are
 * mounted only while their cell edits, so draft state initializes fresh from
 * the entry. They live inside a <td> (no <form>), hence type="button" + a
 * manual min-interval guard mirroring the min={60} attribute. All controls sit
 * on one nowrap line (.cell-edit); a too-narrow cell scrolls in .table-scroll
 * instead of wrapping. */
type CellEditorProps = {
  entry: WatchEntry;
  saving: boolean;
  onSave: (body: Parameters<typeof updateWatch>[1]) => void;
  onCancel: () => void;
};

/* Icon-only 保存/取消 pair shared by both cell editors. Toned icon buttons:
 * green = commit this edit, dim = abandon it. */
function EditActions({
  saving,
  valid,
  onSave,
  onCancel,
}: {
  saving: boolean;
  valid: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <>
      <button
        type="button"
        className="icon-btn"
        data-tone="ok"
        aria-label="保存"
        title="保存 (Enter)"
        disabled={saving || !valid}
        onClick={onSave}
      >
        <Icon name="check" size={14} />
      </button>
      <button
        type="button"
        className="icon-btn"
        data-tone="dim"
        aria-label="取消"
        title="取消 (Esc)"
        disabled={saving}
        onClick={onCancel}
      >
        <Icon name="x" size={14} />
      </button>
    </>
  );
}

function NoteEditor({ entry, saving, onSave, onCancel }: CellEditorProps) {
  const [note, setNote] = useState(entry.note ?? "");
  const save = () => onSave({ note });
  return (
    <span
      className="cell-editing"
      onKeyDown={(event: KeyboardEvent) => {
        if (event.key === "Escape") onCancel();
      }}
    >
      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !saving) save();
        }}
        maxLength={500}
        placeholder="备注"
        autoFocus
        style={{ width: 180, fontSize: 12, minHeight: 26, padding: "2px 8px" }}
        aria-label={`${shortTitle(entry.title)} 的备注`}
      />
      <EditActions saving={saving} valid onSave={save} onCancel={onCancel} />
    </span>
  );
}

function IntervalEditor({ entry, saving, onSave, onCancel }: CellEditorProps) {
  const [enabled, setEnabled] = useState(entry.price_watch_enabled);
  const [intervalStr, setIntervalStr] = useState(
    String(entry.interval_seconds),
  );
  const seconds = Number(intervalStr);
  const valid = Number.isInteger(seconds) && seconds >= MIN_INTERVAL;
  const save = () =>
    onSave({ price_watch_enabled: enabled, interval_seconds: seconds });
  return (
    <span
      className="cell-editing"
      onKeyDown={(event: KeyboardEvent) => {
        if (event.key === "Escape") onCancel();
      }}
    >
      <label
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
        }}
      >
        <input
          type="checkbox"
          data-switch
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        监控
      </label>
      <input
        type="number"
        min={MIN_INTERVAL}
        className="mono"
        value={intervalStr}
        onChange={(event) => setIntervalStr(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !saving && valid) save();
        }}
        autoFocus
        style={{ width: 72, fontSize: 12, minHeight: 26, padding: "2px 8px" }}
        aria-label={`${shortTitle(entry.title)} 的检查间隔（秒，下限 ${MIN_INTERVAL}）`}
      />
      <span className="dim mono" style={{ fontSize: 11 }}>
        s
      </span>
      <EditActions
        saving={saving}
        valid={valid}
        onSave={save}
        onCancel={onCancel}
      />
    </span>
  );
}

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const query = useQuery(watchlistOptions());
  const [confirming, setConfirming] = useState<string | null>(null);
  const [editing, setEditing] = useState<{
    id: string;
    field: "note" | "interval";
  } | null>(null);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: keys.watchlist });
  const patch = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof updateWatch>[1];
    }) => updateWatch(id, body),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: deleteWatch,
    onSuccess: () => {
      setConfirming(null);
      void invalidate();
    },
  });

  // Hero facts, computed only from what is already on screen. The deepest
  // drop only renders once something actually dropped -- an empty boast is
  // worse than none.
  const entries = query.data ?? [];
  const watching = entries.filter((e) => e.price_watch_enabled).length;
  const deepest = entries.reduce<number | null>(
    (acc, e) =>
      e.change_ratio < 0 && (acc === null || e.change_ratio < acc)
        ? e.change_ratio
        : acc,
    null,
  );

  return (
    <>
      <PageHero
        eyebrow="ITEM WATCHLIST · PER-ITEM PRICE TRACKER"
        ghost="TRACK"
        title="收藏与降价追踪"
        meta={
          query.data === undefined ? undefined : (
            <>
              <span>
                <Icon name="bookmark" size={12} />
                {entries.length} 件追踪中
              </span>
              <span>
                <Icon name="bell-ring" size={12} />
                {watching} 件开着降价监控
              </span>
              {deepest === null ? null : (
                <span>
                  <Icon name="tag" size={12} />
                  <span className="ok">
                    已捕获最大跌幅 {formatChangeRatio(deepest)}
                  </span>
                </span>
              )}
            </>
          )
        }
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <AddByLink />

        {query.isPending ? <Loading rows={4} /> : null}
        {query.isError ? (
          <ErrorState
            title="拉取收藏夹失败"
            error={query.error}
            onRetry={() => void query.refetch()}
          />
        ) : null}
        {patch.isError ? (
          <ErrorState title="保存失败" error={patch.error} />
        ) : null}
        {remove.isError ? (
          <ErrorState title="移出失败" error={remove.error} />
        ) : null}

        {query.data?.length === 0 ? (
          <Empty message="收藏夹是空的。贴一个链接就能开始追踪它的价格。" />
        ) : null}

        {query.data && query.data.length > 0 ? (
          <section className="card">
            <div className="card-h">
              <h2>
                <Icon name="list" size={15} />
                追踪列表
              </h2>
              <span className="note">{entries.length} ITEMS TRACKING</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 56 }}>封面</th>
                    <th>商品 / 卖家</th>
                    <th style={{ textAlign: "right" }}>现价</th>
                    <th style={{ textAlign: "right" }}>基准价</th>
                    <th style={{ textAlign: "right" }}>涨跌幅</th>
                    <th>状态</th>
                    <th style={{ textAlign: "right" }}>在售时长</th>
                    <th>意图备注</th>
                    <th>降价轮询</th>
                    <th>最近检查</th>
                    <th>操作</th>
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
                      <td>
                        <Link
                          className="t2l"
                          to={`/items/${entry.item_id}`}
                          title={entry.title}
                          /* 420, not 240: real captured titles run long, and a
                           * tight clamp left a dead gutter between this column
                           * and 现价 that read as an empty column. */
                          style={{ maxWidth: 420, fontSize: 13 }}
                        >
                          {shortTitle(entry.title)}
                        </Link>
                        <div className="dim mono" style={{ fontSize: 11 }}>
                          {entry.seller_nick}
                          {entry.seller_is_shop ? " · 商家" : ""}
                        </div>
                      </td>
                      <td
                        className="num"
                        style={{ fontSize: 15, fontWeight: 600 }}
                      >
                        {formatPrice(entry.price_cents)}
                      </td>
                      <td className="num dim">
                        {formatPrice(entry.added_price_cents)}
                      </td>
                      <td className="num">
                        <Change entry={entry} />
                      </td>
                      <td>
                        <StatusPill status={entry.status} />
                      </td>
                      <td className="num">{entry.listed_days.toFixed(1)} 天</td>
                      <td>
                        {editing?.id === entry.item_id &&
                        editing.field === "note" ? (
                          <NoteEditor
                            entry={entry}
                            saving={patch.isPending}
                            onSave={(body) =>
                              patch.mutate(
                                { id: entry.item_id, body },
                                { onSuccess: () => setEditing(null) },
                              )
                            }
                            onCancel={() => setEditing(null)}
                          />
                        ) : (
                          <span className="cell-edit">
                            {entry.note ? (
                              <span
                                title={entry.note}
                                style={{
                                  fontSize: 12,
                                  maxWidth: 140,
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                }}
                              >
                                {entry.note}
                              </span>
                            ) : (
                              <span className="dim" style={{ fontSize: 12 }}>
                                未填写
                              </span>
                            )}
                            <button
                              type="button"
                              className="btn-text row-edit-btn"
                              onClick={() =>
                                setEditing({ id: entry.item_id, field: "note" })
                              }
                              aria-label={`编辑备注：${shortTitle(entry.title)}`}
                            >
                              <Icon name="edit" size={13} />
                            </button>
                          </span>
                        )}
                      </td>
                      <td>
                        {editing?.id === entry.item_id &&
                        editing.field === "interval" ? (
                          <IntervalEditor
                            entry={entry}
                            saving={patch.isPending}
                            onSave={(body) =>
                              patch.mutate(
                                { id: entry.item_id, body },
                                { onSuccess: () => setEditing(null) },
                              )
                            }
                            onCancel={() => setEditing(null)}
                          />
                        ) : (
                          <span className="cell-edit">
                            <span className="mono" style={{ fontSize: 12 }}>
                              {entry.price_watch_enabled
                                ? `${entry.interval_seconds}s`
                                : "关"}
                            </span>
                            <button
                              type="button"
                              className="btn-text row-edit-btn"
                              onClick={() =>
                                setEditing({
                                  id: entry.item_id,
                                  field: "interval",
                                })
                              }
                              aria-label={`编辑降价轮询：${shortTitle(entry.title)}`}
                            >
                              <Icon name="edit" size={13} />
                            </button>
                          </span>
                        )}
                      </td>
                      <td
                        className="mono muted"
                        style={{ fontSize: 12, whiteSpace: "nowrap" }}
                      >
                        {formatRelativeTime(entry.last_run_at)}
                        {entry.last_error ? (
                          <div>
                            <span
                              className="pill"
                              data-tone="warn"
                              title={entry.last_error}
                            >
                              <Icon name="alert-triangle" size={11} />
                              检查报错
                            </span>
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {confirming === entry.item_id ? (
                          <ConfirmInline
                            verb="移出"
                            pending={remove.isPending}
                            onConfirm={() => remove.mutate(entry.item_id)}
                            onCancel={() => setConfirming(null)}
                          />
                        ) : (
                          <button
                            type="button"
                            className="btn-text"
                            data-tone="danger"
                            onClick={() => setConfirming(entry.item_id)}
                          >
                            <Icon name="trash-2" size={13} />
                            移出
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p
              className="dim mono"
              style={{
                fontSize: 11.5,
                margin: "var(--space-3) 0 0",
                borderTop: "1px dashed var(--line2)",
                paddingTop: "var(--space-3)",
              }}
            >
              备注会作为「个人意图」喂给单品 AI 建议（如「预算
              2600，急用」）；降价监控轮询间隔最小 {MIN_INTERVAL}s。
            </p>
          </section>
        ) : null}
      </div>
    </>
  );
}
