import {
  Children,
  cloneElement,
  isValidElement,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from "react";

import { useHeroDrift } from "../lib/fx";

type PageHeroProps = {
  /** Uppercased mono line above the title, e.g. "SYSTEM OVERVIEW". */
  eyebrow: string;
  /** May carry a `<span className="thin"> / …</span>` suffix, but ONLY when
   *  that suffix is data: the keyword a chart is scoped to, the filter a list
   *  is narrowed by. A suffix that just renames the page ("设置 / 系统控制台")
   *  is decoration wearing a slash and was removed on 2026-09-13. */
  title: ReactNode;
  /** Oversized outlined background word. Purely decorative. */
  ghost?: string;
  /** Row of <span> facts under the title; page supplies the data. */
  meta?: ReactNode;
};

type SplitState = { i: number; text: string[] };

/** Walks the title tree wrapping every glyph in `.ch > .ch-i` (the masked
 *  rise-in, staggered by --i) while collecting the plain text for the h1's
 *  aria-label — split spans are aria-hidden so screen readers get one clean
 *  string, not spelled-out characters. Whitespace stays unwrapped so word
 *  spacing and line breaking are untouched. Array.from splits by code point,
 *  so CJK and surrogate pairs survive.
 */
function splitTitle(node: ReactNode, state: SplitState): ReactNode {
  return Children.map(node, (child) => {
    if (typeof child === "string" || typeof child === "number") {
      const s = String(child);
      state.text.push(s);
      return Array.from(s).map((glyph) => {
        if (glyph.trim() === "") return glyph;
        const i = state.i++;
        return (
          <span className="ch" aria-hidden="true" key={i}>
            <span className="ch-i" style={{ "--i": i } as CSSProperties}>
              {glyph}
            </span>
          </span>
        );
      });
    }
    if (isValidElement(child)) {
      const el = child as ReactElement<{ children?: ReactNode }>;
      return cloneElement(el, undefined, splitTitle(el.props.children, state));
    }
    return child;
  });
}

/** SIGNAL DECK giant page header. Presentational only — no fetching here.
 *  The title rises in per character (CSS, zeroed under reduced motion) and
 *  the ghost word drifts on scroll via useHeroDrift.
 */
export function PageHero({ eyebrow, title, ghost, meta }: PageHeroProps) {
  const drift = useHeroDrift();
  const state: SplitState = { i: 0, text: [] };
  const split = splitTitle(title, state);
  return (
    <div className="hero">
      {ghost === undefined ? null : (
        <span className="hero-ghost" aria-hidden="true" ref={drift}>
          {ghost}
        </span>
      )}
      <div className="hero-eyebrow">{eyebrow}</div>
      <h1 aria-label={state.text.join("")}>{split}</h1>
      {meta === undefined ? null : <div className="hero-meta">{meta}</div>}
    </div>
  );
}
