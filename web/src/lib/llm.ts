/** Reading the two LLM answer objects, and the one branch that must not be
 *  got backwards.
 *
 *  `LlmMarketAnalysis.reading` and `LlmItemAnalysis.data` are `dict` on the
 *  backend on purpose (`schemas.py`: the JSON field names are already stated
 *  in the prompt and in the Pydantic model, and a third copy would be a third
 *  place to drift), so they arrive here as bags of `unknown`. Narrowing them
 *  is real logic with a real failure mode -- a wrong field name drops the
 *  answer and shows an empty card -- so it lives here with a test rather than
 *  inline in a page. There is no jsdom in this project, so a `lib` function is
 *  the only thing that can carry a check at all.
 */

/** The five states the two analyze routes answer with. `no_data` only comes
 *  from `market`; the item route has no equivalent because a listing always
 *  has itself as input.
 */
export type LlmKind = "ok" | "starved" | "empty" | "unparsable" | "no_data";

/** The subset of both analyze responses that `LlmPanel` renders. Both real
 *  responses are structurally assignable to it.
 *
 *  `message` is the SERVER's wording and the panel prints it as given. The
 *  three HTTP-200 failures have three different fixes -- raise max_tokens,
 *  check the model id, fix the template -- and any wording invented on this
 *  side would eventually tell one of those users to go and edit the thing
 *  that was never wrong.
 */
export type LlmResult = {
  kind: LlmKind;
  message?: string | null;
  text: string;
  disclaimer: string;
};

/** What the generated types give for a `dict` response field. */
type Fields = { [key: string]: unknown } | null | undefined;

export type MarketReading = {
  level: string;
  trend: string;
  advice: string;
  reasons: string[];
  summary: string;
};

export type ItemAdvice = {
  verdict: string;
  /** Cents, so `formatPrice` stays the only thing that renders money. The
   *  model answers in yuan and may answer with a decimal. */
  fairPriceCents: number | null;
  offerPriceCents: number | null;
  risks: string[];
  summary: string;
};

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** String list, dropping anything that is not one. A model that puts an
 *  object in `risks` should cost that bullet, not the whole answer.
 */
function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

/** Yuan from the model to cents for `formatPrice`. Null for anything that is
 *  not a finite number, which renders as "—" rather than as "¥NaN".
 */
function yuanToCents(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.round(value * 100);
}

/** `summary` is the field that decides whether there is an answer at all.
 *
 *  Both Pydantic models require it, so a validated payload always has one; a
 *  payload without one is a shape this page does not understand, and the
 *  honest fallback is the model's own raw `text` rather than a card of empty
 *  rows. Callers get null and show the text.
 */
export function marketReading(reading: Fields): MarketReading | null {
  const summary = str(reading?.summary);
  if (!reading || !summary) return null;
  return {
    level: str(reading.level),
    trend: str(reading.trend),
    advice: str(reading.advice),
    reasons: strings(reading.reasons),
    summary,
  };
}

export function itemAdvice(data: Fields): ItemAdvice | null {
  const summary = str(data?.summary);
  if (!data || !summary) return null;
  return {
    verdict: str(data.verdict),
    fairPriceCents: yuanToCents(data.fair_price_yuan),
    offerPriceCents: yuanToCents(data.offer_price_yuan),
    risks: strings(data.risks),
    summary,
  };
}

/** Whether a state should read as something going wrong.
 *
 *  `no_data` must answer false: the keyword had no priced listing, so no call
 *  was made and nothing was billed (`api/llm.py`'s NO_DATA_MESSAGE). Painting
 *  it red sends the user looking for a broken endpoint when the fix is to let
 *  the collector run. `ok` is obviously false; the other three are the model
 *  or the endpoint failing us and are worth the danger colour.
 */
export function isFailure(kind: LlmKind): boolean {
  return kind !== "ok" && kind !== "no_data";
}

/** Whether a scenario can actually be run.
 *
 *  The same four conditions `api/llm.py` checks before it will call anything:
 *  a row exists, it is enabled, it names an endpoint and it names a model.
 *  Duplicated on this side on purpose -- the alternative is a button that
 *  looks live, spends a round trip and comes back 409 with the same sentence
 *  the page could have shown before it was pressed. Getting it backwards
 *  greys out a working button, which is why it lives here with a test.
 */
export function scenarioReady(
  config: { endpoint_id: number | null; model: string | null; enabled: boolean } | undefined,
): boolean {
  if (config === undefined) return false;
  return config.enabled && config.endpoint_id !== null && Boolean(config.model);
}

/** Whether the model's raw output is the useful thing to show.
 *
 *  Only `unparsable`, and it is not optional there: a user breaking their own
 *  template is a normal event, and the text is the only place the mistake is
 *  visible. `ok` renders the parsed answer instead, and the two empty states
 *  have no text by definition.
 */
export function showsRawText(kind: LlmKind, text: string): boolean {
  return kind === "unparsable" && text.trim().length > 0;
}


/** What one click actually cost, in words.
 *
 *  `cached` alone cannot say it: a first draw that fails validation is retried
 *  once, so `cached: false` covers both one call and two. Measured live, a
 *  single market click billed 100s + 52s because the first answer was not
 *  valid JSON — the user paid twice and nothing on screen said so.
 */
export function billingNote(cached: boolean, calls: number): string {
  if (cached) return "命中缓存，本次没有产生新的计费调用";
  if (calls > 1) return `本次调用了 ${calls} 次接口：第一次的回答格式不合要求，自动重试了一次`;
  return "本次调用了 1 次接口";
}
