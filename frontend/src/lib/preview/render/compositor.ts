// The engine's WebGL2 compositor (INSTANT_PREVIEW_SPEC §3.4, §9.1): one
// canvas, one texture per lane (laneA in Phase 1), the geometry pass of
// render/shaders/geometry.ts with the parameters of render/geometry.ts.
//
// * "Last good frame": the ENGINE decides when a draw may happen (every
//   texture for k ready); this module never clears the canvas on its own.
// * The texture remembers which content id it holds, so the engine can tell
//   a stale texture from the frame it needs.
// * On pause the engine asks for a 2D snapshot (≤ 2 ms), shown in place of
//   the GL canvas if WebKit drops the context (`webglcontextlost`); on
//   `webglcontextrestored` programs and textures are rebuilt and the engine
//   re-uploads.
// * `onDrawn` fires synchronously after every draw, while the drawing buffer
//   is still readable (preserveDrawingBuffer is false): the test pages read
//   pixels there.

import { apply, type Affine, type Bounds, type ClipGeometry, type Size } from './geometry'
import { GEOMETRY_FRAG, GEOMETRY_VERT } from './shaders/geometry'

export const NO_CONTENT = -3

export interface DrawSpec {
  geometry: ClipGeometry
  /** EDL canvas size the geometry is expressed in. */
  canvas: Size
  /** Source texture size (proxy px), for the mipmap decision. */
  texture?: Size
}

export interface CompositorOptions {
  canvas: HTMLCanvasElement
  /** 2D backing canvas shown while the GL context is lost. */
  snapshot?: HTMLCanvasElement
  onContextLost?(): void
  onContextRestored?(): void
  /** Trilinear sampling when a clip is shown smaller than its texture. */
  mipmaps?: boolean
}

export interface DrawnInfo { k: number; black: boolean; ms: number }

interface Locs {
  tex: WebGLUniformLocation | null
  dispToCanvas: WebGLUniformLocation | null
  dispH: WebGLUniformLocation | null
  toF2: WebGLUniformLocation | null
  f2Bounds: WebGLUniformLocation | null
  toF1: WebGLUniformLocation | null
  f1Bounds: WebGLUniformLocation | null
  f1Clamp: WebGLUniformLocation | null
  f1Extent: WebGLUniformLocation | null
  toUv: WebGLUniformLocation | null
  uvBounds: WebGLUniformLocation | null
  gain: WebGLUniformLocation | null
  black: WebGLUniformLocation | null
  lod: WebGLUniformLocation | null
}

const mat3 = (m: Affine) => new Float32Array([m.a, m.b, 0, m.c, m.d, 0, m.e, m.f, 1])
const vec4 = (b: Bounds) => new Float32Array([b.x0, b.y0, b.x1, b.y1])

/** Source texels per display pixel of `g` drawn at `disp` from `tex`. */
export function texelsPerPixel(g: ClipGeometry, canvas: Size, disp: Size, tex: Size): number {
  const sx = canvas.w / disp.w
  const sy = canvas.h / disp.h
  const at = (x: number, y: number) => {
    const [x2, y2] = apply(g.toF2, x * sx, y * sy)
    const [x1, y1] = apply(g.toF1, x2, y2)
    return apply(g.toUv, x1, y1)
  }
  const [u0, v0] = at(0, 0)
  const [u1, v1] = at(1, 0)
  const [u2, v2] = at(0, 1)
  const dx = Math.hypot((u1 - u0) * tex.w, (v1 - v0) * tex.h)
  const dy = Math.hypot((u2 - u0) * tex.w, (v2 - v0) * tex.h)
  return Math.max(dx, dy)
}

export class Compositor {
  readonly canvas: HTMLCanvasElement
  readonly snapshotCanvas: HTMLCanvasElement | null
  onDrawn: ((info: DrawnInfo, gl: WebGL2RenderingContext) => void) | null = null
  lost = false
  /** Output frame the 2D snapshot holds (−1: none, or cleared to black). */
  snapshotK = -1
  textureContent = NO_CONTENT
  textureSize: Size = { w: 0, h: 0 }
  readonly stats = { draws: 0, blackDraws: 0, uploads: 0, maxUploadMs: 0, maxDrawMs: 0, lost: 0, restored: 0, snapshots: 0, mipmaps: 0 }
  private gl: WebGL2RenderingContext | null = null
  private program: WebGLProgram | null = null
  private vao: WebGLVertexArrayObject | null = null
  private tex: WebGLTexture | null = null
  private loc: Locs | null = null
  private mipmapped = false
  private readonly opts: CompositorOptions
  private readonly onLost = (e: Event) => {
    e.preventDefault()
    this.lost = true
    this.stats.lost++
    this.textureContent = NO_CONTENT
    this.showSnapshot(true)
    this.opts.onContextLost?.()
  }
  private readonly onRestored = () => {
    this.lost = false
    this.stats.restored++
    this.build()
    this.opts.onContextRestored?.()
  }

  constructor(opts: CompositorOptions) {
    this.opts = opts
    this.canvas = opts.canvas
    this.snapshotCanvas = opts.snapshot ?? null
    this.canvas.addEventListener('webglcontextlost', this.onLost)
    this.canvas.addEventListener('webglcontextrestored', this.onRestored)
    this.build()
  }

  /** The GL context (tests: pixel reads inside onDrawn). */
  get context(): WebGL2RenderingContext | null {
    return this.gl
  }

  private build(): void {
    const gl = this.canvas.getContext('webgl2', {
      alpha: false, premultipliedAlpha: false, preserveDrawingBuffer: false, antialias: false,
      depth: false, stencil: false, powerPreference: 'default',
    }) as WebGL2RenderingContext | null
    if (!gl) throw new Error('no WebGL2')
    this.gl = gl
    if (gl.isContextLost()) {
      this.lost = true
      return
    }
    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!
      gl.shaderSource(s, src)
      gl.compileShader(s)
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
        throw new Error(`shader: ${gl.getShaderInfoLog(s)}`)
      }
      return s
    }
    const p = gl.createProgram()!
    gl.attachShader(p, compile(gl.VERTEX_SHADER, GEOMETRY_VERT))
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, GEOMETRY_FRAG))
    gl.linkProgram(p)
    if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(`link: ${gl.getProgramInfoLog(p)}`)
    this.program = p
    const u = (n: string) => gl.getUniformLocation(p, n)
    this.loc = {
      tex: u('u_tex'), dispToCanvas: u('u_dispToCanvas'), dispH: u('u_dispH'), toF2: u('u_toF2'),
      f2Bounds: u('u_f2Bounds'), toF1: u('u_toF1'), f1Bounds: u('u_f1Bounds'), f1Clamp: u('u_f1Clamp'),
      f1Extent: u('u_f1Extent'), toUv: u('u_toUv'), uvBounds: u('u_uvBounds'), gain: u('u_gain'), black: u('u_black'),
      lod: u('u_lod'),
    }
    this.vao = gl.createVertexArray()
    this.tex = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false)
    this.textureContent = NO_CONTENT
    this.textureSize = { w: 0, h: 0 }
    this.mipmapped = false
  }

  resize(w: number, h: number): void {
    const W = Math.max(2, Math.round(w))
    const H = Math.max(2, Math.round(h))
    if (this.canvas.width !== W) this.canvas.width = W
    if (this.canvas.height !== H) this.canvas.height = H
    if (this.snapshotCanvas) {
      if (this.snapshotCanvas.width !== W) this.snapshotCanvas.width = W
      if (this.snapshotCanvas.height !== H) this.snapshotCanvas.height = H
    }
  }

  /** Uploads the element's current frame as `contentId` (≤ 1-3 ms, §10). */
  upload(source: TexImageSource, contentId: number, size: Size): boolean {
    const gl = this.gl
    if (!gl || this.lost || !this.tex) return false
    const t0 = performance.now()
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source)
    this.textureContent = contentId
    this.textureSize = size
    this.mipmapped = false
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    this.stats.uploads++
    this.stats.maxUploadMs = Math.max(this.stats.maxUploadMs, performance.now() - t0)
    return true
  }

  /** Forget the texture (its frame was overwritten; do not draw from it). */
  invalidateTexture(): void {
    this.textureContent = NO_CONTENT
  }

  /** Draws one output frame (`spec` null: a gap, black). */
  draw(spec: DrawSpec | null, k: number): boolean {
    const gl = this.gl
    if (!gl || this.lost || !this.program || !this.loc) return false
    const t0 = performance.now()
    const L = this.loc
    const disp = { w: this.canvas.width, h: this.canvas.height }
    gl.viewport(0, 0, disp.w, disp.h)
    gl.useProgram(this.program)
    gl.bindVertexArray(this.vao)
    gl.uniform1i(L.black, spec ? 0 : 1)
    if (spec) {
      const g = spec.geometry
      gl.activeTexture(gl.TEXTURE0)
      gl.bindTexture(gl.TEXTURE_2D, this.tex)
      if (this.opts.mipmaps !== false && spec.texture && !this.mipmapped
        && Math.max(texelsPerPixel(g, spec.canvas, disp, spec.texture),
          g.f1Clamp ? Math.abs(g.toUv.a * spec.texture.w) : 0) > 1.5) {
        gl.generateMipmap(gl.TEXTURE_2D)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
        this.mipmapped = true
        this.stats.mipmaps++
      }
      gl.uniform1i(L.tex, 0)
      gl.uniform2f(L.dispToCanvas, spec.canvas.w / disp.w, spec.canvas.h / disp.h)
      gl.uniform1f(L.dispH, disp.h)
      gl.uniformMatrix3fv(L.toF2, false, mat3(g.toF2))
      gl.uniform4fv(L.f2Bounds, vec4(g.f2Bounds))
      gl.uniformMatrix3fv(L.toF1, false, mat3(g.toF1))
      gl.uniform4fv(L.f1Bounds, vec4(g.f1Bounds))
      gl.uniform1i(L.f1Clamp, g.f1Clamp ? 1 : 0)
      gl.uniform4f(L.f1Extent, 0, 0, spec.canvas.w, spec.canvas.h)
      gl.uniformMatrix3fv(L.toUv, false, mat3(g.toUv))
      gl.uniform4fv(L.uvBounds, vec4(g.uvBounds))
      gl.uniform1f(L.gain, g.gain)
      // the rotate path samples on the F1 pixel grid: its mip level is the
      // texture footprint of ONE F1 pixel (hardware derivatives across that
      // snapped grid are meaningless)
      const tex = spec.texture ?? this.textureSize
      const perF1 = Math.max(Math.abs(g.toUv.a * tex.w), Math.abs(g.toUv.d * tex.h))
      gl.uniform1f(L.lod, this.mipmapped ? Math.max(0, Math.log2(Math.max(1e-6, perF1))) : 0)
    } else {
      gl.uniform1f(L.dispH, disp.h)
    }
    gl.drawArrays(gl.TRIANGLES, 0, 3)
    const ms = performance.now() - t0
    this.stats.draws++
    if (!spec) this.stats.blackDraws++
    this.stats.maxDrawMs = Math.max(this.stats.maxDrawMs, ms)
    this.onDrawn?.({ k, black: !spec, ms }, gl)
    return true
  }

  /** Copies the frame just drawn to the 2D backing canvas (call right after
   *  draw, in the same task). */
  snapshot(k: number): void {
    const s = this.snapshotCanvas
    if (!s || this.lost) return
    const ctx = s.getContext('2d')
    if (!ctx) return
    ctx.drawImage(this.canvas, 0, 0)
    this.snapshotK = k
    this.stats.snapshots++
  }

  /** Paints the snapshot black: it must never show a frame other than the
   *  one the engine presents (context lost mid-play, §7). */
  clearSnapshot(): void {
    const s = this.snapshotCanvas
    const ctx = s?.getContext('2d')
    if (!s || !ctx) return
    ctx.fillStyle = '#000'
    ctx.fillRect(0, 0, s.width, s.height)
    this.snapshotK = -1
  }

  showSnapshot(on: boolean): void {
    if (!this.snapshotCanvas) return
    this.snapshotCanvas.style.visibility = on ? 'visible' : 'hidden'
    this.canvas.style.visibility = on ? 'hidden' : 'visible'
  }

  /** RGBA of a region of the drawing buffer, top-down rows. Valid inside
   *  onDrawn (or right after draw in the same task). */
  readPixels(x: number, y: number, w: number, h: number): Uint8Array {
    const gl = this.gl!
    const out = new Uint8Array(w * h * 4)
    gl.readPixels(x, this.canvas.height - y - h, w, h, gl.RGBA, gl.UNSIGNED_BYTE, out)
    // GL rows are bottom-up
    const row = w * 4
    const tmp = new Uint8Array(row)
    for (let i = 0; i < h >> 1; i++) {
      const a = i * row
      const b = (h - 1 - i) * row
      tmp.set(out.subarray(a, a + row))
      out.copyWithin(a, b, b + row)
      out.set(tmp, b)
    }
    return out
  }

  destroy(): void {
    this.canvas.removeEventListener('webglcontextlost', this.onLost)
    this.canvas.removeEventListener('webglcontextrestored', this.onRestored)
    const gl = this.gl
    if (gl && !gl.isContextLost()) {
      if (this.tex) gl.deleteTexture(this.tex)
      if (this.program) gl.deleteProgram(this.program)
      if (this.vao) gl.deleteVertexArray(this.vao)
    }
    this.gl = null
  }
}
