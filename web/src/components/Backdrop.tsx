import { useRef } from "react";

import { useBgfx, useCursorGlow } from "../lib/fx";

/** Decorative backdrop: static grid/vignette, aurora wash and film grain,
 *  plus the signal-field canvas and pointer glow from lib/fx.ts. All fixed,
 *  pointer-events none, below `.page` (z-index 1); the animated layers are
 *  fully inert under prefers-reduced-motion and pause on a hidden tab.
 *  Shared by the app shell and the login page.
 */
export function Backdrop() {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const glow = useRef<HTMLDivElement | null>(null);
  useBgfx(canvas);
  useCursorGlow(glow);
  return (
    <>
      <div className="bg-grid" />
      <div className="bg-aurora" aria-hidden="true" />
      <div className="bg-vignette" />
      <canvas ref={canvas} className="bgfx" aria-hidden="true" />
      <div className="bg-noise" aria-hidden="true" />
      <div ref={glow} className="cursor-glow" aria-hidden="true" />
    </>
  );
}
