// The top bar's width budget (docs/design/LEFT_RAIL_SPEC.md §1.4, critique H6).
//
// The bar is a `minmax(0,1fr) auto minmax(max-content,1fr)` grid: the left
// group (brand · wordmark · project · Applying · activity) is the only part
// that can shrink, and inside it only the project name truncates. When even a
// fully truncated name does not make room, the bar steps up a DENSITY: each
// step hides words, never controls (every name stays in the markup):
//
//   0  everything (≥ 1440)
//   1  wordmark visually hidden (still the page's h1); ratio facts hidden;
//      export error text ≤ 180 px                                (1280–1439)
//   2  "(outdated)" becomes a warn dot, the words stay in the link's name;
//      export error ≤ 140 px                                     (1100–1279)
//   3  Save / Open icon-only; the error chip icon-only (message in its name
//      and tooltip); "Applying" a spinner with sr-only text; project ≤ 160 px
//                                                                (< 1100)
//   4  the activity chip's words ("Rec", "Captions") hide — content-driven
//
// The step is a pure function (`nextDensity`) so it is unit-tested; the hook
// measures in a layout effect, before paint, so nothing flickers. It starts
// from the viewport's baseline every time — on resize and whenever the bar's
// content changes — which is its hysteresis: a density is never "stuck" from a
// wider moment. The loop is bounded (at most MAX_DENSITY + 1 measurements).
//
// Every step is CSS on the header's classes ONLY — nothing a step hides may
// depend on React state (a facts span rendered only at density 0 made a
// re-fit from a denser step measure a narrower centre than the one it then
// rendered, and settle on a step that overflowed).
//
// Density is written to the header twice, deliberately: imperatively while
// measuring (a measurement needs the classes applied NOW, not after a React
// render), and as React state so the rendered className agrees with what was
// measured — both are the same function of `d` (`densityClass`).

import { useCallback, useEffect, useLayoutEffect, useState, type RefObject } from 'react'

export const MAX_DENSITY = 4
export type Density = 0 | 1 | 2 | 3 | 4

/** The viewport's starting step (§1.4 "Baseline at"). */
export function baselineFor(viewportWidth: number): Density {
  if (viewportWidth >= 1440) return 0
  if (viewportWidth >= 1280) return 1
  if (viewportWidth >= 1100) return 2
  return 3
}

/** One step of the fit: stay where it fits, else one step denser, never past 4. */
export function nextDensity(current: Density, fits: boolean): Density {
  if (fits) return current
  return Math.min(MAX_DENSITY, current + 1) as Density
}

/** Step from `base` until `fitsAt` says the bar fits (or density 4). Bounded:
 *  `fitsAt` is called at most MAX_DENSITY + 1 times. */
export function fitDensity(base: Density, fitsAt: (d: Density) => boolean): Density {
  let d = base
  for (let i = 0; i <= MAX_DENSITY; i++) {
    const next = nextDensity(d, fitsAt(d))
    if (next === d) return d
    d = next
  }
  return d
}

/** The header's cumulative density classes: step 3 is `tb-d1 tb-d2 tb-d3`. */
export function densityClass(d: Density): string {
  const out: string[] = []
  for (let k = 1; k <= d; k++) out.push(`tb-d${k}`)
  return out.join(' ')
}

function applyDensity(bar: HTMLElement, d: Density): void {
  for (let k = 1; k <= MAX_DENSITY; k++) bar.classList.toggle(`tb-d${k}`, k <= d)
  bar.dataset.density = String(d)
}

/** Nothing clips: the left group does not overflow its column and the bar
 *  does not overflow the window. scrollWidth/clientWidth are whole pixels. */
function fitsNow(bar: HTMLElement, left: HTMLElement): boolean {
  return left.scrollWidth <= left.clientWidth && bar.scrollWidth <= bar.clientWidth
}

const useIsoLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

/**
 * Keeps `data-density` / `tb-dN` on the header at the lowest step where the
 * bar fits. `contentKey` is everything React renders into the bar that can
 * change its width (activity, stale flags, the error, the project name,
 * pending ops, exporting); a change re-fits before paint. Width that changes
 * without a React input here (the recording clock gaining a digit, the Export
 * button's own elapsed counter) or a box that resizes without a window resize
 * is caught by a ResizeObserver on the bar and its two growing groups,
 * re-fitted on the next frame.
 */
export function useTopBarFit(
  barRef: RefObject<HTMLElement | null>,
  leftRef: RefObject<HTMLElement | null>,
  contentKey: string,
): Density {
  const [density, setDensity] = useState<Density>(() =>
    typeof window === 'undefined' ? 0 : baselineFor(window.innerWidth))

  const refit = useCallback(() => {
    const bar = barRef.current
    const left = leftRef.current
    if (!bar || !left) return
    // The bar spans the window (its 900 px floor aside, where #root scrolls
    // and the bar stays 900 wide), so its own width IS the viewport width the
    // baselines are written for — and a page that hosts the bar in a narrower
    // box (the WKWebView test page) gets that box's baseline.
    const d = fitDensity(baselineFor(bar.clientWidth), (k) => {
      applyDensity(bar, k)
      return fitsNow(bar, left)
    })
    applyDensity(bar, d)
    setDensity(d)
  }, [barRef, leftRef])

  // Content changed: re-fit before paint.
  useIsoLayoutEffect(() => { refit() }, [refit, contentKey])

  useEffect(() => {
    // Resize: re-fit synchronously in the handler (it runs before the frame
    // paints), from the new width's baseline.
    window.addEventListener('resize', refit)
    let raf = 0
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      // Deferred a frame: re-fitting inside the callback would resize the
      // observed elements during delivery (the "ResizeObserver loop" error).
      // It converges — a re-fit that lands on the same step changes no size.
      window.cancelAnimationFrame(raf)
      raf = window.requestAnimationFrame(refit)
    })
    const bar = barRef.current
    if (ro && bar) {
      ro.observe(bar)
      for (const el of bar.querySelectorAll<HTMLElement>('[data-activity], .tb-right')) ro.observe(el)
    }
    return () => {
      window.removeEventListener('resize', refit)
      window.cancelAnimationFrame(raf)
      ro?.disconnect()
    }
  }, [refit, barRef])

  return density
}
