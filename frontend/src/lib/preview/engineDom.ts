// The engine's DOM (INSTANT_PREVIEW_SPEC §3.2, §3.4): one muted laneA
// <video> UNDER an opaque WebGL2 canvas, and a 2D snapshot canvas shown only
// while WebKit has dropped the GL context. Styles are inline (no stylesheet
// is shared with the app).

import type { Size } from './render/geometry'

export interface EngineDom {
  root: HTMLDivElement
  video: HTMLVideoElement
  canvas: HTMLCanvasElement
  snap: HTMLCanvasElement
}

/** Builds the engine's elements inside `host` (the ONE media element the
 *  engine creates: §3.2 allows two). */
export function mountEngineDom(host: HTMLElement): EngineDom {
  const root = document.createElement('div')
  root.dataset.previewEngine = ''
  root.style.cssText = 'position:absolute;inset:0;overflow:hidden;background:#000;'
  // laneA's element sits UNDER the opaque canvas: in the layout and "on
  // screen" as far as WebKit's visibility checks go (a display:none or
  // off-screen muted video may be paused by WebKit's media policy), never
  // seen.
  const video = document.createElement('video')
  video.muted = true
  video.playsInline = true
  video.disableRemotePlayback = true
  video.preload = 'auto'
  video.setAttribute('aria-hidden', 'true')
  video.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;pointer-events:none;'
  const canvas = document.createElement('canvas')
  canvas.style.cssText = 'position:absolute;pointer-events:none;'
  const snap = document.createElement('canvas')
  snap.style.cssText = 'position:absolute;pointer-events:none;visibility:hidden;'
  root.append(video, canvas, snap)
  if (getComputedStyle(host).position === 'static') host.style.position = 'relative'
  host.appendChild(root)
  return { root, video, canvas, snap }
}

/** Positions `els` as the largest box of the EDL canvas's aspect inside
 *  `host`, centred, and returns the backing size: the box × DPR with the
 *  short edge capped (§3.4: 1080), or `fixed` when given (tests). */
export function layoutCanvas(host: HTMLElement, els: HTMLElement[], canvas: Size, fixed: Size | undefined, maxShortEdge: number): Size {
  const hw = Math.max(1, host.clientWidth)
  const hh = Math.max(1, host.clientHeight)
  const aspect = canvas.w / canvas.h
  const cssW = Math.min(hw, hh * aspect)
  const cssH = cssW / aspect
  for (const el of els) {
    el.style.width = `${cssW}px`
    el.style.height = `${cssH}px`
    el.style.left = `${(hw - cssW) / 2}px`
    el.style.top = `${(hh - cssH) / 2}px`
  }
  if (fixed) return { w: fixed.w, h: fixed.h }
  const dpr = window.devicePixelRatio || 1
  let w = cssW * dpr
  let h = cssH * dpr
  const short = Math.min(w, h)
  if (short > maxShortEdge) {
    w *= maxShortEdge / short
    h *= maxShortEdge / short
  }
  return { w, h }
}
