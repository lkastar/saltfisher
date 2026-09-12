import { describe, expect, it } from "vitest";

import type { Monitor } from "../api/queries";
import {
  findSellerRule,
  sellerRuleName,
  keywordRuleLabel,
} from "./monitorRules";

/** Real seller ids, copied out of `tests/fixtures/search_real.json`. Opaque
 *  base64 with `+`, `/` and `=` in it — which is the whole reason a rule name
 *  may never be built out of one.
 */
const ID = "+5xYqEw3f9sETQuuxt2Emw==";
const OTHER_ID = "6C9UyAl5OsXgten6tYo/sQ==";

function rule(fields: Partial<Monitor>): Monitor {
  return {
    id: 1,
    name: "rule",
    keyword: null,
    seller_id: null,
    seller_nick: null,
    exclude_words: "",
    price_min_cents: null,
    price_max_cents: null,
    published_within_hours: null,
    region: null,
    condition: null,
    free_shipping: null,
    min_seller_credit: null,
    exclude_shop: false,
    interval_seconds: 300,
    enabled: true,
    baseline_done: false,
    last_run_at: null,
    last_error: null,
    last_collector: null,
    consecutive_failures: 0,
    hit_count: 0,
    channel_ids: [],
    ...fields,
  };
}

describe("sellerRuleName", () => {
  it("uses the nick", () => {
    expect(sellerRuleName("小顾数码", ID)).toBe("卖家：小顾数码");
  });

  it("never puts the whole base64 id in the name when the nick is missing", () => {
    for (const nick of [undefined, null, "", "   "]) {
      const name = sellerRuleName(nick, ID);
      expect(name).not.toContain(ID);
      expect(name).toBe("卖家：+5xYqEw3…");
    }
  });

  it("stays inside the backend's name max_length", () => {
    // Seller.nick has no length limit, so this is a real 422 the user would
    // get on a name they never typed.
    const name = sellerRuleName("长".repeat(200), ID);
    expect(name.length).toBe(100);
  });
});

describe("findSellerRule", () => {
  it("finds the rule watching that seller", () => {
    const existing = rule({
      id: 7,
      name: "卖家：小顾数码",
      seller_id: ID,
      seller_nick: "小顾数码",
    });
    expect(findSellerRule([existing], ID)?.id).toBe(7);
  });

  it("reports nothing when no rule watches that seller", () => {
    expect(findSellerRule([rule({ seller_id: OTHER_ID })], ID)).toBeUndefined();
  });

  it("never matches a keyword rule", () => {
    // The regression that matters: keyword rules have seller_id null and must
    // stay invisible to this, or the items page would refuse to create a
    // seller rule and point at an unrelated keyword rule instead.
    const keywordRules = [
      rule({ id: 1, keyword: "iPhone 13", seller_id: null }),
      rule({ id: 2, keyword: "Kindle", seller_id: null }),
    ];
    expect(findSellerRule(keywordRules, ID)).toBeUndefined();
    expect(
      findSellerRule([...keywordRules, rule({ id: 3, seller_id: ID })], ID)?.id,
    ).toBe(3);
  });
});

describe("keywordRuleLabel", () => {
  const rules = [
    { name: "捡漏 iPhone", keyword: "iPhone 15 128G" },
    { name: "备用机", keyword: "iPhone 15 128G" },
    { name: "相机", keyword: "索尼 a7c2" },
    { name: "盯卖家", keyword: null },
  ] as unknown as Monitor[];

  it("names the single rule that watches the keyword", () => {
    expect(keywordRuleLabel(rules, "索尼 a7c2")).toBe("相机");
  });

  it("counts them when several rules share one keyword", () => {
    // Analytics aggregates across all of them, so crediting only the first
    // would attribute the other rule's listings to it.
    expect(keywordRuleLabel(rules, "iPhone 15 128G")).toBe(
      "捡漏 iPhone 等 2 条规则",
    );
  });

  it("falls back to the keyword when the rule is gone", () => {
    // A shared link outliving its rule; there is no name left to show.
    expect(keywordRuleLabel(rules, "已删除的词")).toBe("已删除的词");
  });

  it("ignores seller rules, which have no keyword", () => {
    expect(keywordRuleLabel(rules, "")).toBe("");
  });
});
