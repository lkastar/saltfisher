import type { ReactNode } from "react";

type PageHeroProps = {
  /** Uppercased mono line above the title, e.g. "SYSTEM OVERVIEW". */
  eyebrow: string;
  /** May carry a <span className="thin"> suffix per the prototype. */
  title: ReactNode;
  /** Oversized outlined background word. Purely decorative. */
  ghost?: string;
  /** Row of <span> facts under the title; page supplies the data. */
  meta?: ReactNode;
};

/** SIGNAL DECK giant page header. Presentational only — no fetching here. */
export function PageHero({ eyebrow, title, ghost, meta }: PageHeroProps) {
  return (
    <div className="hero">
      {ghost === undefined ? null : (
        <span className="hero-ghost" aria-hidden="true">
          {ghost}
        </span>
      )}
      <div className="hero-eyebrow">{eyebrow}</div>
      <h1>{title}</h1>
      {meta === undefined ? null : <div className="hero-meta">{meta}</div>}
    </div>
  );
}
