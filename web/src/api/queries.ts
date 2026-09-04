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

/** Key hierarchy. Invalidating a prefix invalidates everything under it. */
export const keys = {
  monitors: ["monitors"] as const,
  monitor: (id: number) => ["monitors", id] as const,
  channels: ["channels"] as const,
  notifyLogs: ["notify-logs"] as const,
  watchlist: ["watchlist"] as const,
};

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
