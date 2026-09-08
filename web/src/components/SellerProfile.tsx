/** Seller reputation, as far as it was actually fetched.
 *
 *  Every field renders "未知" when null and the real number when 0. That
 *  distinction is the whole point: a seller with 0 reviews is a warning sign,
 *  a seller whose reviews were never fetched is missing data, and showing the
 *  second as the first hides the signal that matters most.
 *
 *  Hence `??` and never `||` -- `0 || "未知"` is "未知".
 */

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

const ROW: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "var(--space-3)",
  fontSize: 13,
};

export default function SellerProfile({ seller }: { seller: Profile }) {
  const shop =
    seller.seller_is_shop === null || seller.seller_is_shop === undefined
      ? "未知"
      : seller.seller_is_shop
        ? "鱼小铺（商家）"
        : "个人卖家";

  return (
    <section
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--surface)",
        padding: "var(--space-3)",
      }}
    >
      <h2>卖家</h2>
      <div style={ROW}>
        <span className="muted">昵称</span>
        <span>{seller.seller_nick}</span>
      </div>
      <div style={ROW}>
        <span className="muted">类型</span>
        <span>{shop}</span>
      </div>
      <div style={ROW}>
        <span className="muted">信用等级</span>
        <span className="mono">{show(seller.seller_credit_level)}</span>
      </div>
      <div style={ROW}>
        <span className="muted">评价数</span>
        <span className="mono">{show(seller.seller_review_count)}</span>
      </div>
      <div style={ROW}>
        <span className="muted">好评率</span>
        <span className="mono">
          {seller.seller_positive_rate === null ||
          seller.seller_positive_rate === undefined
            ? "未知"
            : `${seller.seller_positive_rate}%`}
        </span>
      </div>
      <div style={ROW}>
        <span className="muted">已售</span>
        <span className="mono">{show(seller.seller_sold_count)}</span>
      </div>
      <p className="muted" style={{ fontSize: 11.5, marginTop: "var(--space-1)" }}>
        「未知」是没抓到，不是 0。0 条评价与未知评价是两种判断依据。
      </p>
    </section>
  );
}
