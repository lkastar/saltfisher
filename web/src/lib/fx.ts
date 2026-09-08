import { useCallback, useEffect, useRef, useState } from "react";

/** Decorative layer (task step 8). Everything here is cuttable: deleting this
 *  file plus the `data-reveal` attributes and `<CountUp>` leaves the panel
 *  fully functional. Under `prefers-reduced-motion` both helpers render the
 *  final state immediately.
 *
 *  Not ported from the prototype, on purpose: canvas bgfx, cursor glow and
 *  magnetic buttons (pure ornament, not <20 lines); the ticker clone already
 *  lives in `components/Ticker.tsx` since step 4.
 */

function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** One-shot reveal-on-view. Returns a callback ref to spread over any number
 *  of `data-reveal` elements in the calling component; each gets `.in` when it
 *  first intersects (CSS in base.css does the fade). Attach only to wrappers
 *  that mount once and keep a constant className -- `.in` lives outside
 *  React's props, so a className swap would clobber it and hide the card.
 */
export function useReveal(): (el: HTMLElement | null) => void {
  const io = useRef<IntersectionObserver | null>(null);

  useEffect(() => () => io.current?.disconnect(), []);

  // useCallback for identity, not speed: an unstable callback ref is detached
  // and re-attached every render, re-observing already-revealed nodes.
  return useCallback((el: HTMLElement | null) => {
    if (el === null) return;
    if (prefersReducedMotion() || !("IntersectionObserver" in window)) {
      el.classList.add("in");
      return;
    }
    io.current ??= new IntersectionObserver(
      (entries, observer) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add("in");
            observer.unobserve(entry.target);
          }
        }
      },
      // Prototype's trigger band, but the 350ms duration is ours: the spec's
      // anti-pattern is a slow dashboard, so no stagger delays either.
      { threshold: 0.1, rootMargin: "0px 0px -6% 0px" },
    );
    io.current.observe(el);
  }, []);
}

/** Counts from 0 up to `target` (~700ms, ease-out) on mount, and between
 *  targets when a poll changes the number. The text settles to the exact
 *  value well inside a second, so screen readers and any automated check read
 *  the real number -- no aria games needed. Call it from a tiny leaf
 *  component so the per-frame setState re-renders one <span>, not the page.
 */
export function useCountUp(target: number): number {
  const [shown, setShown] = useState(prefersReducedMotion() ? target : 0);
  const shownRef = useRef(shown);

  useEffect(() => {
    const from = shownRef.current;
    if (from === target || prefersReducedMotion()) {
      shownRef.current = target;
      setShown(target);
      return;
    }
    const t0 = performance.now();
    let raf = requestAnimationFrame(function tick(now: number) {
      const p = Math.min((now - t0) / 700, 1);
      const eased = 1 - (1 - p) ** 4;
      const value = Math.round(from + (target - from) * eased);
      shownRef.current = value;
      setShown(value);
      if (p < 1) raf = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(raf);
  }, [target]);

  return shown;
}
