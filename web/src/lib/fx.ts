import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** Decorative layer (task step 8 + round-2 item 2). Everything here is
 *  cuttable: deleting this file plus the `data-reveal` attributes, `<CountUp>`
 *  and the App backdrop leaves the panel fully functional. Under
 *  `prefers-reduced-motion` every helper renders the final state immediately
 *  and no rAF loop ever starts.
 *
 *  Canvas bgfx and cursor glow were cut in step 8, then explicitly requested
 *  back in round-2 feedback (prd.md item 2) — ported from
 *  research/prototype/ui.js. Still not ported: magnetic buttons (pure
 *  ornament, nobody asked); the ticker clone already lives in
 *  `components/Ticker.tsx` since step 4.
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

/** Ambient signal-field canvas (prototype ui.js `bgfx`): drifting outlined
 *  circles with centre dots plus a sweeping scanline. Colours come from the
 *  theme tokens via getComputedStyle — never hardcoded — re-resolved whenever
 *  `data-theme` changes, cached in between. Under `prefers-reduced-motion` no
 *  loop starts and the canvas stays empty; a hidden tab cancels the loop and
 *  resumes on return.
 */
export function useBgfx(ref: RefObject<HTMLCanvasElement | null>): void {
  useEffect(() => {
    const cv = ref.current;
    if (cv === null || prefersReducedMotion()) return;
    const ctx = cv.getContext("2d");
    if (ctx === null) return;

    type Blip = {
      x: number;
      y: number;
      r: number;
      max: number;
      life: number;
      decay: number;
      c: string; // "r, g, b" — alpha is applied per frame
    };
    const blips: Blip[] = [];
    let W = 0;
    let H = 0;
    let DPR = 1;
    let raf = 0;

    const resize = () => {
      DPR = Math.min(window.devicePixelRatio || 1, 2);
      W = cv.width = window.innerWidth * DPR;
      H = cv.height = window.innerHeight * DPR;
      cv.style.width = `${window.innerWidth}px`;
      cv.style.height = `${window.innerHeight}px`;
    };

    let paletteTheme: string | null = null;
    let palette = { acc: "0, 0, 0", pool: [] as string[] };
    const readPalette = () => {
      const theme = document.documentElement.dataset.theme ?? "dark";
      if (theme === paletteTheme) return;
      paletteTheme = theme;
      const cs = getComputedStyle(document.documentElement);
      const rgb = (name: string) => {
        const hex = cs.getPropertyValue(name).trim();
        return `${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}`;
      };
      // Prototype weighting: mostly accent, an occasional green/red blip.
      // Blips already in flight keep their colour until they fade (<3s).
      const acc = rgb("--acc");
      palette = { acc, pool: [acc, acc, acc, rgb("--green"), rgb("--red")] };
    };

    const frame = () => {
      readPalette();
      const light = paletteTheme === "light";
      ctx.clearRect(0, 0, W, H);

      // Scanline sweep removed (round 2): a full-width band travelling down
      // reads as the whole backdrop sliding — hycai flagged it. Blips only.

      // Signal blips.
      if (Math.random() < 0.02 && blips.length < 9) {
        blips.push({
          x: Math.random() * W,
          y: Math.random() * H * 0.85,
          r: 0,
          max: (40 + Math.random() * 90) * DPR,
          life: 1,
          decay: 0.006 + Math.random() * 0.008,
          c: palette.pool[Math.floor(Math.random() * palette.pool.length)] ?? palette.acc,
        });
      }
      for (let i = blips.length - 1; i >= 0; i--) {
        const b = blips[i];
        if (b === undefined) continue; // noUncheckedIndexedAccess; can't happen
        b.r += (b.max - b.r) * 0.03;
        b.life -= b.decay;
        if (b.life <= 0) {
          blips.splice(i, 1);
          continue;
        }
        const a = b.life * (light ? 0.35 : 0.5);
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${b.c}, ${a * 0.35})`;
        ctx.lineWidth = DPR;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(b.x, b.y, 1.6 * DPR, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${b.c}, ${a})`;
        ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    };

    const onVisibility = () => {
      cancelAnimationFrame(raf);
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };

    resize();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", onVisibility);
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [ref]);
}

/** Pointer-follow glow (prototype ui.js `cursorGlow`), lerp-eased via
 *  `translate3d`. The rAF loop only runs while the glow is still catching up
 *  to the pointer, so an idle or hidden tab (no mousemove events) spends
 *  nothing; it never starts under `prefers-reduced-motion` or on coarse
 *  pointers. The element itself is styled in base.css (`.cursor-glow`, hidden
 *  in the light theme like the prototype).
 */
export function useCursorGlow(ref: RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    const el = ref.current;
    if (
      el === null ||
      prefersReducedMotion() ||
      !window.matchMedia("(pointer: fine)").matches
    ) {
      return;
    }
    let tx = 0;
    let ty = 0;
    let x = 0;
    let y = 0;
    let raf = 0;
    let running = false;

    // 260 = half the 520px glow, centring it on the pointer.
    const step = () => {
      x += (tx - x) * 0.08;
      y += (ty - y) * 0.08;
      el.style.transform = `translate3d(${x - 260}px, ${y - 260}px, 0)`;
      if (Math.abs(tx - x) + Math.abs(ty - y) > 0.5) {
        raf = requestAnimationFrame(step);
      } else {
        running = false;
      }
    };
    const onMove = (e: MouseEvent) => {
      tx = e.clientX;
      ty = e.clientY;
      if (el.style.opacity !== "1") {
        // First movement: appear at the pointer instead of gliding across.
        x = tx;
        y = ty;
        el.style.opacity = "1";
      }
      if (!running) {
        running = true;
        raf = requestAnimationFrame(step);
      }
    };
    window.addEventListener("mousemove", onMove, { passive: true });
    return () => {
      window.removeEventListener("mousemove", onMove);
      cancelAnimationFrame(raf);
    };
  }, [ref]);
}
