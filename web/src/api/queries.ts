/** queryOptions factories. Every query key in the app is produced here.
 *
 *  A hand-typed key that does not match its invalidation freezes the UI with
 *  no error anywhere, so the keys are not allowed to exist in two places.
 */

import { queryOptions } from "@tanstack/react-query";

import { request } from "./client";
import type { components } from "./types";

type Schemas = components["schemas"];

export type Monitor = Schemas["MonitorPublic"];
export type MonitorCreate = Schemas["MonitorCreate"];
export type MonitorUpdate = Schemas["MonitorUpdate"];
export type CycleResult = Schemas["CycleResult"];
export type Channel = Schemas["ChannelPublic"];
export type ChannelCreate = Schemas["ChannelCreate"];
export type ChannelUpdate = Schemas["ChannelUpdate"];
export type NotifyLog = Schemas["NotifyLogPublic"];
export type TestSendResult = Schemas["TestSendResult"];
export type WatchEntry = Schemas["WatchlistPublic"];
export type WatchlistCreate = Schemas["WatchlistCreate"];
export type WatchlistUpdate = Schemas["WatchlistUpdate"];
export type Item = Schemas["ItemPublic"];
export type PricePoint = Schemas["PricePoint"];
export type SessionState = Schemas["SessionState"];
export type CookieImport = Schemas["CookieImport"];

/** Key hierarchy. Invalidating a prefix invalidates everything under it. */
export const keys = {
  monitors: ["monitors"] as const,
  monitor: (id: number) => ["monitors", id] as const,
  channels: ["channels"] as const,
  notifyLogs: ["notify-logs"] as const,
  watchlist: ["watchlist"] as const,
  items: (filters: ItemFilters) => ["items", filters] as const,
  item: (id: string) => ["items", id] as const,
  itemPrices: (id: string) => ["items", id, "prices"] as const,
  session: ["session"] as const,
};

/** Exactly the filters that live in the URL. `offset` is here because paging
 *  belongs in the query key: two pages are two different results.
 */
export type ItemFilters = {
  monitor_id?: number;
  min_price_cents?: number;
  max_price_cents?: number;
  status?: "on_sale" | "sold" | "removed";
  sort?: "-first_seen" | "first_seen" | "-last_seen" | "price" | "-price";
  offset?: number;
  /** Page size. Not in the URL -- it is a paging implementation detail, not
   *  user intent -- but it is part of the request and therefore of the key. */
  limit?: number;
};

function itemQuery(filters: ItemFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function monitorsOptions() {
  return queryOptions({
    queryKey: keys.monitors,
    queryFn: () => request<Monitor[]>("/api/monitors"),
  });
}

export function monitorOptions(id: number) {
  return queryOptions({
    queryKey: keys.monitor(id),
    queryFn: () => request<Monitor>(`/api/monitors/${id}`),
  });
}

export function channelsOptions() {
  return queryOptions({
    queryKey: keys.channels,
    queryFn: () => request<Channel[]>("/api/channels"),
  });
}

export function notifyLogsOptions() {
  return queryOptions({
    queryKey: keys.notifyLogs,
    queryFn: () => request<NotifyLog[]>("/api/notify-logs"),
  });
}

export function itemsOptions(filters: ItemFilters) {
  return queryOptions({
    queryKey: keys.items(filters),
    queryFn: () => request<Item[]>(`/api/items${itemQuery(filters)}`),
  });
}

export function itemOptions(id: string, monitorId?: number) {
  return queryOptions({
    queryKey: keys.item(id),
    queryFn: () =>
      request<Item>(
        `/api/items/${id}${monitorId === undefined ? "" : `?monitor_id=${monitorId}`}`,
      ),
  });
}

export function itemPricesOptions(id: string) {
  return queryOptions({
    queryKey: keys.itemPrices(id),
    queryFn: () => request<PricePoint[]>(`/api/items/${id}/prices`),
  });
}

export function sessionOptions() {
  return queryOptions({
    queryKey: keys.session,
    queryFn: () => request<SessionState>("/api/session"),
  });
}

export function watchlistOptions() {
  return queryOptions({
    queryKey: keys.watchlist,
    queryFn: () => request<WatchEntry[]>("/api/watchlist"),
  });
}

/* ---------- mutations ----------
 * Plain functions, not hooks: the page calls useMutation with one of these as
 * its mutationFn and owns its own invalidation.
 */

export const createMonitor = (body: MonitorCreate) =>
  request<Monitor>("/api/monitors", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateMonitor = (id: number, body: MonitorUpdate) =>
  request<Monitor>(`/api/monitors/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteMonitor = (id: number) =>
  request<null>(`/api/monitors/${id}`, { method: "DELETE" });

export const runMonitor = (id: number) =>
  request<CycleResult>(`/api/monitors/${id}/run`, { method: "POST" });

export const createChannel = (body: ChannelCreate) =>
  request<Channel>("/api/channels", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateChannel = (id: number, body: ChannelUpdate) =>
  request<Channel>(`/api/channels/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteChannel = (id: number) =>
  request<null>(`/api/channels/${id}`, { method: "DELETE" });

export const testChannel = (id: number) =>
  request<TestSendResult>(`/api/channels/${id}/test`, { method: "POST" });

export const addWatch = (body: WatchlistCreate) =>
  request<WatchEntry>("/api/watchlist", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateWatch = (itemId: string, body: WatchlistUpdate) =>
  request<WatchEntry>(`/api/watchlist/${itemId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteWatch = (itemId: string) =>
  request<null>(`/api/watchlist/${itemId}`, { method: "DELETE" });

export const importCookies = (body: CookieImport) =>
  request<SessionState>("/api/session/cookies", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const clearCookies = () =>
  request<SessionState>("/api/session/cookies", { method: "DELETE" });
