// Live blend modes for PiP clips (wave E, lane F2): one transparent canvas per
// PiP, stacked in z order UNDER StickerLayer's own canvas, each composited
// onto everything beneath it with CSS `mix-blend-mode` — the W3C formula
// render/pip.py bakes on export (edl/canvas_blend.py; lib/canvasBlend).
//
// WHY NOT `globalCompositeOperation` on StickerLayer's canvas: that canvas is
// a TRANSPARENT layer over the picture — the base video (the server
// preview's <video>, or the engine's WebGL canvas) is a different element, so
// a composite operation there blends the PiP with transparent pixels, i.e.
// draws it Normal. Only the compositor that stacks the elements has both
// sides, and `mix-blend-mode` is how a page asks it to blend them: the same
// operator names, the same W3C maths, and it follows every CSS transform the
// base picture is under (a live pan/zoom of v1, the frozen-frame cover, the
// engine's snapshot canvas).
//
// The host is a plain positioned <div> with NO z-index, opacity, filter or
// transform — any of those would make it a stacking context and isolate the
// blend from the video behind it (each PiP would then blend with nothing).
// A PiP drawn Normal in a stack that has blended ones gets its own layer too,
// so the z order is kept: layer i holds exactly the i-th active PiP.
//
// Layers exist only while a blended PiP is on screen; otherwise every PiP is
// painted into StickerLayer's canvas as before and the layers are hidden.

export class PipBlendLayers {
  private readonly host: Pick<HTMLElement, 'appendChild'>
  private readonly make: () => HTMLCanvasElement
  private readonly pool: HTMLCanvasElement[] = []
  private used = 0
  private w = 0
  private h = 0
  private dpr = 1

  constructor(host: Pick<HTMLElement, 'appendChild'>,
              make: () => HTMLCanvasElement = () => document.createElement('canvas')) {
    this.host = host
    this.make = make
  }

  /** Starts a frame of `width`×`height` CSS px at `dpr`. */
  begin(width: number, height: number, dpr: number): void {
    this.used = 0
    this.w = width
    this.h = height
    this.dpr = dpr
  }

  /** The context the next PiP (in z order) draws into, composited with CSS
   *  blend `css`, cleared in DEVICE pixels (StickerLayer's rule) and scaled
   *  to CSS px. Null when no 2D context is available. */
  next(css: string): CanvasRenderingContext2D | null {
    let cv = this.pool[this.used]
    if (!cv) {
      cv = this.make()
      cv.setAttribute('aria-hidden', 'true')
      cv.dataset.pipBlendLayer = String(this.pool.length)
      cv.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;'
      this.host.appendChild(cv)
      this.pool.push(cv)
    }
    this.used++
    const bw = Math.max(1, Math.round(this.w * this.dpr))
    const bh = Math.max(1, Math.round(this.h * this.dpr))
    if (cv.width !== bw || cv.height !== bh) {
      cv.width = bw
      cv.height = bh
      cv.style.width = `${this.w}px`
      cv.style.height = `${this.h}px`
    }
    if (cv.style.mixBlendMode !== css) cv.style.mixBlendMode = css
    cv.dataset.blend = css
    cv.style.display = 'block'
    const ctx = cv.getContext('2d')
    if (!ctx) return null
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, cv.width, cv.height)
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
    return ctx
  }

  /** Hides (and clears) every layer this frame did not use. */
  end(): void {
    for (let i = this.used; i < this.pool.length; i++) {
      const cv = this.pool[i]
      if (cv.style.display === 'none') continue
      cv.getContext('2d')?.clearRect(0, 0, cv.width, cv.height)
      cv.style.display = 'none'
    }
  }

  /** How many layers the last frame drew (tests). */
  get active(): number {
    return this.used
  }

  destroy(): void {
    for (const cv of this.pool) cv.remove()
    this.pool.length = 0
    this.used = 0
  }
}
