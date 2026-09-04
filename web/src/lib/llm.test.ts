import { describe, expect, it } from "vitest";

import { isFailure, itemAdvice, marketReading, scenarioReady, showsRawText, billingNote } from "./llm";

// The two payloads the backend tests pin (`tests/test_llm_market.py`'s READING
// and `tests/test_llm_item.py`'s ADVICE), copied field for field. Inventing a
// shape here would test this file against itself: the whole failure mode being
// guarded is a field name that does not match the contract.
const READING = {
  level: "正常",
  trend: "平稳",
  advice: "观望",
  reasons: ["p50 ¥4045 与 p25 ¥3607 之间的差距不大"],
  summary: "样本 11 件，历史 12 天，只能说水位居中。",
};

const ADVICE = {
  verdict: "可考虑",
  fair_price_yuan: 7800,
  offer_price_yuan: 7400,
  risks: ["热靴有掉漆", "只观察到一次价格"],
  summary: "价格贴近该关键词的中位数，成色描述完整但有掉漆。",
};

describe("marketReading", () => {
  it("reads every field of the contract", () => {
    expect(marketReading(READING)).toEqual({
      level: "正常",
      trend: "平稳",
      advice: "观望",
      reasons: ["p50 ¥4045 与 p25 ¥3607 之间的差距不大"],
      summary: "样本 11 件，历史 12 天，只能说水位居中。",
    });
  });

  it("returns null when there is nothing to render", () => {
    // The failure states carry `reading: null`, and the panel falls back to
    // the server's own message instead of an empty card.
    expect(marketReading(null)).toBeNull();
    expect(marketReading(undefined)).toBeNull();
    expect(marketReading({})).toBeNull();
    expect(marketReading({ level: "偏低" })).toBeNull();
  });

  it("keeps a partial answer that still has a summary", () => {
    // A model that skipped `reasons` has still answered the question.
    const partial = marketReading({ summary: "数据不足", advice: "观望" });
    expect(partial?.summary).toBe("数据不足");
    expect(partial?.reasons).toEqual([]);
    expect(partial?.level).toBe("");
  });

  it("drops list entries that are not strings", () => {
    const reading = marketReading({ summary: "s", reasons: ["一条", 42, null] });
    expect(reading?.reasons).toEqual(["一条"]);
  });
});

describe("itemAdvice", () => {
  it("converts the model's yuan into cents", () => {
    // formatPrice is the only thing in the app that divides by 100, so the
    // conversion has to happen on the way in.
    expect(itemAdvice(ADVICE)).toEqual({
      verdict: "可考虑",
      fairPriceCents: 780000,
      offerPriceCents: 740000,
      risks: ["热靴有掉漆", "只观察到一次价格"],
      summary: "价格贴近该关键词的中位数，成色描述完整但有掉漆。",
    });
  });

  it("rounds a decimal price instead of truncating it", () => {
    // ItemAdvice.fair_price_yuan is a float on the backend on purpose; 8100.5
    // is a perfectly good answer and 8100.4999… is what * 100 produces.
    const advice = itemAdvice({ summary: "s", fair_price_yuan: 8100.5 });
    expect(advice?.fairPriceCents).toBe(810050);
  });

  it("gives null for a price the model did not answer with a number", () => {
    const advice = itemAdvice({ summary: "s", fair_price_yuan: "约 8000" });
    expect(advice?.fairPriceCents).toBeNull();
    expect(advice?.offerPriceCents).toBeNull();
  });
});

describe("isFailure", () => {
  it("does not call no_data a failure", () => {
    // No listing means no call was made and nothing was billed. Rendering it
    // in the danger colour sends the user hunting for a broken endpoint.
    expect(isFailure("no_data")).toBe(false);
    expect(isFailure("ok")).toBe(false);
  });

  it("calls the three HTTP-200 failures failures", () => {
    expect(isFailure("starved")).toBe(true);
    expect(isFailure("empty")).toBe(true);
    expect(isFailure("unparsable")).toBe(true);
  });
});

describe("scenarioReady", () => {
  const ready = { endpoint_id: 1, model: "deepseek-v4-pro", enabled: true };

  it("agrees with the backend's own four conditions", () => {
    expect(scenarioReady(ready)).toBe(true);
    expect(scenarioReady({ ...ready, enabled: false })).toBe(false);
    expect(scenarioReady({ ...ready, endpoint_id: null })).toBe(false);
    expect(scenarioReady({ ...ready, model: null })).toBe(false);
    // The scenario row exists but the model box was saved blank: `save_scenario`
    // stores "" as null, and an older row could still hold the empty string.
    expect(scenarioReady({ ...ready, model: "" })).toBe(false);
  });

  it("is not ready while the config is still loading", () => {
    expect(scenarioReady(undefined)).toBe(false);
  });
});

describe("showsRawText", () => {
  it("shows the model's own words when the template broke", () => {
    expect(showsRawText("unparsable", "这不是 JSON")).toBe(true);
  });

  it("does not show raw text for the states that have none", () => {
    // starved and empty come back with text "" -- printing an empty <pre>
    // next to "raise max_tokens" reads as a second, silent failure.
    expect(showsRawText("starved", "")).toBe(false);
    expect(showsRawText("empty", "")).toBe(false);
    expect(showsRawText("unparsable", "   ")).toBe(false);
    // ok has text too (the JSON it parsed); the parsed answer is what shows.
    expect(showsRawText("ok", '{"summary": "…"}')).toBe(false);
  });
});

describe("billingNote", () => {
  it("names the retry, because cached:false covers one call and two", () => {
    // Measured live: one market click billed 100s + 52s when the first draw
    // was not valid JSON. Nothing on screen said the user had paid twice.
    expect(billingNote(false, 2)).toContain("2 次");
    expect(billingNote(false, 2)).toContain("重试");
    expect(billingNote(false, 1)).toContain("1 次");
    expect(billingNote(false, 1)).not.toContain("重试");
  });

  it("says a cache hit cost nothing", () => {
    expect(billingNote(true, 0)).toContain("没有产生新的计费调用");
    // Even if the server ever reported a count on a hit, the hit wins: it did
    // not bill for THIS click.
    expect(billingNote(true, 2)).toContain("没有产生新的计费调用");
  });
});
