import { useState } from "react";

/** A goofish CDN image.
 *
 *  Shared rather than three inline `<img onError>` copies: "never show a
 *  broken image" is a stated constraint, and one page forgetting it is the
 *  same as not having the rule.
 *
 *  `referrerPolicy="no-referrer"` is NOT needed for the images to load --
 *  measured against real img.alicdn.com URLs, they serve fine with any
 *  referer. It stays because it stops leaking this panel's URLs to Alibaba's
 *  CDN. Do not remove it thinking it was only there for hotlink protection.
 *
 *  `onError` alone is not enough on this CDN. A missing file comes back as a
 *  1x1 pixel with HTTP 200, so the load SUCCEEDS and only the dimensions give
 *  it away -- measured: a deliberately wrong img.alicdn.com path decoded to
 *  naturalWidth 1 and never fired onError. Hence the size check on load.
 */
export default function RemoteImage({
  src,
  alt,
  width,
  height,
  rounded = true,
}: {
  src: string | null | undefined;
  alt: string;
  width: number | string;
  height: number | string;
  rounded?: boolean;
}) {
  const [failed, setFailed] = useState(false);

  const frame = {
    width,
    height,
    borderRadius: rounded ? "var(--radius-sm)" : 0,
    border: "1px solid var(--border)",
    background: "var(--surface-2)",
    flex: "0 0 auto",
  } as const;

  if (!src || failed) {
    return (
      <div
        style={{
          ...frame,
          display: "grid",
          placeItems: "center",
          color: "var(--text-muted)",
          fontSize: 11,
          textAlign: "center",
          padding: 2,
        }}
        // The placeholder carries the alt text so a failed image is still
        // described rather than being an anonymous grey square.
        role="img"
        aria-label={`${alt}（图片未加载）`}
      >
        无图
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      referrerPolicy="no-referrer"
      loading="lazy"
      onError={() => setFailed(true)}
      onLoad={(event) => {
        // A 1x1 response is this CDN's way of saying "no such file". The
        // threshold stays at 2px: a real thumbnail is never that small, and
        // anything larger might be a legitimately tiny image.
        const img = event.currentTarget;
        if (img.naturalWidth <= 2 || img.naturalHeight <= 2) setFailed(true);
      }}
      style={{ ...frame, objectFit: "cover" }}
    />
  );
}
