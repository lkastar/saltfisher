/** queryOptions factories. Every query key in the app is produced here.
 *
 *  A hand-typed key that does not match its invalidation freezes the UI with
 *  no error anywhere, so the keys are not allowed to exist in two places.
 */

import { queryOptions } from "@tanstack/react-query";

import { request } from "./client";
import type { components, operations } from "./types";

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
export type MonitorTrend = Schemas["MonitorTrend"];
export type MonitorTrendDay = Schemas["MonitorTrendDay"];
export type SessionState = Schemas["SessionState"];
export type StatsOverview = Schemas["StatsOverview"];
export type SellerRefreshResult = Schemas["SellerRefreshResult"];
export type CookieImport = Schemas["CookieImport"];
export type ImportTicket = Schemas["ImportTicket"];
export type PriceDistribution = Schemas["PriceDistribution"];
export type PriceBucket = Schemas["PriceBucket"];
export type PriceQuantiles = Schemas["PriceQuantiles"];
export type PriceDrops = Schemas["PriceDrops"];
export type PriceDrop = Schemas["PriceDrop"];
export type SupplyTrend = Schemas["SupplyTrend"];
export type SupplyDay = Schemas["SupplyDay"];
export type ListingDuration = Schemas["ListingDuration"];
export type DurationBucket = Schemas["DurationBucket"];
export type LlmEndpoint = Schemas["LlmEndpointPublic"];
export type LlmEndpointCreate = Schemas["LlmEndpointCreate"];
export type LlmEndpointUpdate = Schemas["LlmEndpointUpdate"];
export type LlmModelList = Schemas["LlmModelList"];
export type LlmTestResult = Schemas["LlmTestResult"];
export type LlmScenarioConfig = Schemas["LlmScenarioPublic"];
export type LlmScenarioUpdate = Schemas["LlmScenarioUpdate"];
export type LlmDefaultPrompt = Schemas["LlmDefaultPrompt"];
export type LlmMarketAnalysis = Schemas["LlmMarketAnalysis"];
export type LlmMarketAnalyze = Schemas["LlmMarketAnalyze"];
export type LlmItemAnalysis = Schemas["LlmItemAnalysis"];
/** The two scenarios, from the backend's own `Scenario` literal.
 *
 *  Reached through `operations` rather than written out, because it is a path
 *  parameter enum and not a named schema -- there is no
 *  `components["schemas"]` entry to alias. Renaming the route or the literal
 *  breaks this line loudly, which is the point.
 */
export type Scenario =
  operations["read_scenario_api_llm_scenarios__scenario__get"]["parameters"]["path"]["scenario"];

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
  statsOverview: ["stats", "overview"] as const,
  monitorTrend: (monitorId: number, days: number) =>
    ["stats", "monitor-trend", monitorId, days] as const,
  /** Coarse -> fine, so invalidating ["analytics"] drops all four charts at
   *  once. The window is part of the key because two windows are two
   *  different answers, not two renderings of one. */
  priceDistribution: (q: AnalyticsQuery) =>
    ["analytics", "price-distribution", q] as const,
  priceDrops: (q: AnalyticsDropsQuery) => ["analytics", "price-drops", q] as const,
  supplyTrend: (q: AnalyticsQuery) => ["analytics", "supply-trend", q] as const,
  listingDuration: (q: AnalyticsQuery) => ["analytics", "listing-duration", q] as const,
  /** Coarse -> fine again, so deleting an endpoint can invalidate keys.llm and
   *  drop the scenario configs that just lost their endpoint with it. */
  llm: ["llm"] as const,
  llmEndpoints: ["llm", "endpoints"] as const,
  llmModels: (endpointId: number) => ["llm", "endpoints", endpointId, "models"] as const,
  llmScenario: (scenario: Scenario) => ["llm", "scenarios", scenario] as const,
  llmDefaultPrompt: (scenario: Scenario) =>
    ["llm", "scenarios", scenario, "default-prompt"] as const,
};

/** What the analytics endpoints are scoped by. `days` means something
 *  different in each of the four -- which listings count, what "before"
 *  means, how wide the chart is, which first sightings are in scope -- so the
 *  page states it per chart rather than pretending one window has one
 *  meaning.
 */
export type AnalyticsQuery = { keyword: string; days: number };
export type AnalyticsDropsQuery = AnalyticsQuery & { limit: number };

/** Exactly the filters that live in the URL. `offset` is here because paging
 *  belongs in the query key: two pages are two different results.
 */
export type ItemFilters = {
  monitor_id?: number;
  /** Opaque base64 from the upstream, `+` `/` `=` and all. It only ever
   *  travels through URLSearchParams, which escapes it. */
  seller_id?: string;
  min_price_cents?: number;
  max_price_cents?: number;
  status?: "on_sale" | "sold" | "removed";
  sort?: "-first_seen" | "first_seen" | "-last_seen" | "price" | "-price";
  offset?: number;
  /** Page size. Not in the URL -- it is a paging implementation detail, not
   *  user intent -- but it is part of the request and therefore of the key. */
  limit?: number;
};

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
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

/** `limit` is part of the key: 50 rows and 200 rows are two different
 *  answers. `keys.notifyLogs` stays the invalidation prefix, so a send still
 *  drops every window at once. The backend caps limit at 200.
 */
export function notifyLogsOptions(limit = 50) {
  return queryOptions({
    queryKey: [...keys.notifyLogs, limit] as const,
    queryFn: () => request<NotifyLog[]>(`/api/notify-logs${queryString({ limit })}`),
  });
}

export function itemsOptions(filters: ItemFilters) {
  return queryOptions({
    queryKey: keys.items(filters),
    queryFn: () => request<Item[]>(`/api/items${queryString(filters)}`),
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

/** The overview KPI row in one request. The 30s poll is the caller's:
 *  dashboard cadence belongs to the dashboard, not the factory.
 */
export function statsOverviewOptions() {
  return queryOptions({
    queryKey: keys.statsOverview,
    queryFn: () => request<StatsOverview>("/api/stats/overview"),
  });
}

/** One rule's daily price level. The window is in the key because two windows
 *  are two different answers -- same rule as the analytics factories.
 */
export function monitorTrendOptions(monitorId: number, days = 30) {
  return queryOptions({
    queryKey: keys.monitorTrend(monitorId, days),
    queryFn: () =>
      request<MonitorTrend>(`/api/stats/monitor-trend?monitor_id=${monitorId}&days=${days}`),
  });
}

export function sessionOptions() {
  return queryOptions({
    queryKey: keys.session,
    queryFn: () => request<SessionState>("/api/session"),
  });
}

export function priceDistributionOptions(q: AnalyticsQuery) {
  return queryOptions({
    queryKey: keys.priceDistribution(q),
    queryFn: () =>
      request<PriceDistribution>(`/api/analytics/price-distribution${queryString(q)}`),
  });
}

export function priceDropsOptions(q: AnalyticsDropsQuery) {
  return queryOptions({
    queryKey: keys.priceDrops(q),
    queryFn: () => request<PriceDrops>(`/api/analytics/price-drops${queryString(q)}`),
  });
}

export function supplyTrendOptions(q: AnalyticsQuery) {
  return queryOptions({
    queryKey: keys.supplyTrend(q),
    queryFn: () => request<SupplyTrend>(`/api/analytics/supply-trend${queryString(q)}`),
  });
}

export function listingDurationOptions(q: AnalyticsQuery) {
  return queryOptions({
    queryKey: keys.listingDuration(q),
    queryFn: () =>
      request<ListingDuration>(`/api/analytics/listing-duration${queryString(q)}`),
  });
}

export function watchlistOptions() {
  return queryOptions({
    queryKey: keys.watchlist,
    queryFn: () => request<WatchEntry[]>("/api/watchlist"),
  });
}

export function llmEndpointsOptions() {
  return queryOptions({
    queryKey: keys.llmEndpoints,
    queryFn: () => request<LlmEndpoint[]>("/api/llm/endpoints"),
  });
}

/** Available models for one endpoint.
 *
 *  A query and not a mutation even though the page fires it from a button: it
 *  is a GET, it caches, and the scenario form below reads the same key the
 *  endpoint card filled. The caller gates it with `enabled` -- nothing should
 *  poll a third party's models route on mount.
 *
 *  It answers 200 with `{models: [], error}` when the gateway does not
 *  implement the route, so `isError` here means OUR request failed, not
 *  theirs. Both have to render, and they render differently.
 */
export function llmModelsOptions(endpointId: number) {
  return queryOptions({
    queryKey: keys.llmModels(endpointId),
    queryFn: () => request<LlmModelList>(`/api/llm/endpoints/${endpointId}/models`),
    // A model list is a fact about someone else's server; re-asking it on
    // every window focus spends their rate limit for nothing.
    staleTime: Infinity,
  });
}

export function llmScenarioOptions(scenario: Scenario) {
  return queryOptions({
    queryKey: keys.llmScenario(scenario),
    queryFn: () => request<LlmScenarioConfig>(`/api/llm/scenarios/${scenario}`),
  });
}

/** The built-in template plus the placeholder contract. Both come from one
 *  response because the page needs both at once: "restore default" writes the
 *  template, and the list beside the editor is the only place a user can
 *  learn which `{names}` mean anything.
 */
export function llmDefaultPromptOptions(scenario: Scenario) {
  return queryOptions({
    queryKey: keys.llmDefaultPrompt(scenario),
    queryFn: () => request<LlmDefaultPrompt>(`/api/llm/scenarios/${scenario}/default-prompt`),
    // Compiled into the backend; it cannot change without a redeploy.
    staleTime: Infinity,
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

/** On-demand seller profile fetch. encodeURIComponent because a seller id is
 *  opaque base64 (`+` `/` `=` and all) and here it travels in the PATH, where
 *  nothing escapes it for us the way URLSearchParams does in queries.
 */
export const refreshSeller = (sellerId: string) =>
  request<SellerRefreshResult>(`/api/sellers/${encodeURIComponent(sellerId)}/refresh`, {
    method: "POST",
  });

export const importCookies = (body: CookieImport) =>
  request<SessionState>("/api/session/cookies", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const clearCookies = () =>
  request<SessionState>("/api/session/cookies", { method: "DELETE" });

/** Mints the single-use ticket the bookmarklet carries. Never `SFD_API_TOKEN`:
 *  that script runs inside a page goofish serves.
 */
export const mintImportTicket = () =>
  request<ImportTicket>("/api/session/import-ticket", { method: "POST" });

export const createLlmEndpoint = (body: LlmEndpointCreate) =>
  request<LlmEndpoint>("/api/llm/endpoints", {
    method: "POST",
    body: JSON.stringify(body),
  });

/** An omitted `api_key` keeps the stored one; `""` clears it. Nothing ever
 *  hands the page a key to send back, so there is no redacted placeholder to
 *  guard against here (unlike `updateChannel`).
 */
export const updateLlmEndpoint = (id: number, body: LlmEndpointUpdate) =>
  request<LlmEndpoint>(`/api/llm/endpoints/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteLlmEndpoint = (id: number) =>
  request<null>(`/api/llm/endpoints/${id}`, { method: "DELETE" });

/** One real completion against the endpoint. `model` is required by the route
 *  because a connection is only testable through the model it will use.
 */
export const testLlmEndpoint = (id: number, model: string) =>
  request<LlmTestResult>(`/api/llm/endpoints/${id}/test${queryString({ model })}`, {
    method: "POST",
  });

export const saveLlmScenario = (scenario: Scenario, body: LlmScenarioUpdate) =>
  request<LlmScenarioConfig>(`/api/llm/scenarios/${scenario}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

/* The two analyze calls are POSTs that cost money and take 30-60 seconds
 * (measured: 59.2s for market, 4.4s for item). They are mutations, never
 * queries: a query would be refetched on window focus and on mount, and each
 * refetch is a billed call. Nothing here caches on this side either -- the
 * market route has its own server-side cache keyed on the rendered prompt. */

export const analyzeMarket = (body: LlmMarketAnalyze) =>
  request<LlmMarketAnalysis>("/api/llm/analyze/market", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const analyzeItem = (itemId: string) =>
  request<LlmItemAnalysis>(`/api/llm/analyze/item/${itemId}`, { method: "POST" });
