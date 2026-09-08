/** Seller reputation, as far as it was actually fetched.
 *
 *  Every field renders "未知" when null and the real number when 0. That
 *  distinction is the whole point: a seller with 0 reviews is a warning sign,
 *  a seller whose reviews were never fetched is missing data, and showing the
 *  second as the first hides the signal that matters most.
 *
 *  Hence `??` and never `||` -- `0 || "未知"` is "未知".
 */

import { Icon } from "./Icon";

type Profile = {
  seller_nick: string;
  seller_is_shop?: boolean | null;
  seller_credit_level?: number | null;
  seller_review_count?: number | null;
  seller_positive_rate?: number | null;
  seller_sold_count?: number | null;
  seller_verified?: boolean | null;
};

function show(value: number | null | undefined, suffix = ""): string {
  return value === null || value === undefined ? "未知" : `${value}${suffix}`;
}

export default function SellerProfile({ seller }: { seller: Profile }) {
  const shopKnown = seller.seller_is_shop !== null && seller.seller_is_shop !== undefined;
  const shop = !shopKnown ? "类型未知" : seller.seller_is_shop ? "鱼小铺（商家）" : "个人卖家";

  return (
    <section className="card">
      <div className="card-h">
        <h2>
          <Icon name="user-check" size={15} />
          卖家画像
        </h2>
        {/* Accent only when the type is a fetched fact; "未知" stays neutral. */}
        <span className="pill" data-tone={shopKnown ? "acc" : undefined}>
          {shop}
        </span>
      </div>

      {seller.seller_positive_rate !== null && seller.seller_positive_rate !== undefined ? (
        <div
          style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 12 }}
        >
          <span
            className="mono"
            style={{ fontSize: 32, fontWeight: 700, color: "var(--acc2)", lineHeight: 1 }}
          >
            {seller.seller_positive_rate}%
          </span>
          <span className="dim mono" style={{ fontSize: 12 }}>
            好评率{seller.seller_review_count !== null &&
            seller.seller_review_count !== undefined
              ? ` · ${seller.seller_review_count} 评价`
              : ""}
          </span>
        </div>
      ) : null}

      <dl className="dl">
        <dt>卖家昵称</dt>
        <dd style={{ color: "var(--acc2)" }}>{seller.seller_nick.trim() || "未知"}</dd>
        <dt>卖家类型</dt>
        <dd>{shop}</dd>
        <dt>信用等级</dt>
        <dd>{show(seller.seller_credit_level)}</dd>
        <dt>历史评价</dt>
        <dd>{show(seller.seller_review_count, " 条")}</dd>
        {/* No green here even though the prototype tints it: --green means
            "cheaper / collecting normally" in this app, and a seller stat is
            not a system state. The number stands on its own. */}
        <dt>历史好评率</dt>
        <dd>
          {seller.seller_positive_rate === null || seller.seller_positive_rate === undefined
            ? "未知"
            : `${seller.seller_positive_rate}%`}
        </dd>
        <dt>已售出商品</dt>
        <dd>{show(seller.seller_sold_count, " 件")}</dd>
      </dl>

      <div
        className="dim mono"
        style={{
          marginTop: 14,
          paddingTop: 12,
          borderTop: "1px dashed var(--line2)",
          fontSize: 12,
        }}
      >
        「未知」是没抓到字段，不是 0 记录。0 条评价与未知评价是两种判断依据。
      </div>
    </section>
  );
}
