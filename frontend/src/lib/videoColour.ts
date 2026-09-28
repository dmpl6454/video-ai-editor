// A video frame drawn into a 2D canvas COLOUR-EXACT in WebKit (review RE).
//
// WebKit's `drawImage(<video>)` into a 2D canvas colour-manages an untagged
// video through a ~1.96 gamma to sRGB: a 64/128/192 grey-band overlay read
// back 64/142/211 there, 56/130/205 in Chromium and in the export's BT.709
// decode — every PiP came out brighter in the midtones than the export, and
// live blend parity in the app's WebKit engine was ~27-30 dB instead of the
// 36-45 dB measured elsewhere. A WebGL texture upload of the same frame is
// NOT colour-managed that way (measured 56/130/205 in both browsers, and the
// engine's v1 already draws through WebGL), so in WebKit the PiP frame is
// copied through a small WebGL canvas first; Chromium keeps `drawImage`.
import { isWebKitCompositor } from './canvasBlend/catalog'

let gl: WebGL2RenderingContext | null = null
let glCanvas: HTMLCanvasElement | null = null
let program: WebGLProgram | null = null
let tex: WebGLTexture | null = null
let uCrop: WebGLUniformLocation | null = null
let broken = false
let forced: boolean | null = null

/** Tests: force the WebGL path on (true), off (false) or back to detection. */
export function forceExactVideoCopy(on: boolean | null): void {
  forced = on
}

/** Whether PiP frames go through the WebGL copy here (WebKit). */
export function exactVideoCopyWanted(): boolean {
  if (forced !== null) return forced
  return !broken && isWebKitCompositor()
}

const VERT = `#version 300 es
uniform vec4 u_crop; out vec2 uv;
void main() {
  vec2 q = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  uv = u_crop.xy + vec2(q.x, 1.0 - q.y) * u_crop.zw;
  gl_Position = vec4(q * 2.0 - 1.0, 0.0, 1.0);
}`
const FRAG = `#version 300 es
precision highp float; in vec2 uv; uniform sampler2D u_t; out vec4 o;
void main() { o = vec4(texture(u_t, uv).rgb, 1.0); }`

interface CopyProgram { program: WebGLProgram; uCrop: WebGLUniformLocation | null; tex: WebGLTexture }

/** The copy program + texture on `g` (throws when the context cannot build it). */
function buildCopy(g: WebGL2RenderingContext): CopyProgram {
  const sh = (type: number, src: string) => {
    const s = g.createShader(type)!
    g.shaderSource(s, src)
    g.compileShader(s)
    if (!g.getShaderParameter(s, g.COMPILE_STATUS)) throw new Error(String(g.getShaderInfoLog(s)))
    return s
  }
  const p = g.createProgram()!
  g.attachShader(p, sh(g.VERTEX_SHADER, VERT))
  g.attachShader(p, sh(g.FRAGMENT_SHADER, FRAG))
  g.linkProgram(p)
  if (!g.getProgramParameter(p, g.LINK_STATUS)) throw new Error(String(g.getProgramInfoLog(p)))
  const t = g.createTexture()!
  g.bindTexture(g.TEXTURE_2D, t)
  g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MIN_FILTER, g.LINEAR)
  g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MAG_FILTER, g.LINEAR)
  g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_S, g.CLAMP_TO_EDGE)
  g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_T, g.CLAMP_TO_EDGE)
  return { program: p, uCrop: g.getUniformLocation(p, 'u_crop'), tex: t }
}

/** Upload `v`'s current frame and draw its `[sx, sy, sw, sh]` over the whole
 *  viewport of `g` — a WebGL texture upload, which WebKit does NOT
 *  colour-manage (see the header). */
function copyFrame(g: WebGL2RenderingContext, cp: CopyProgram, v: HTMLVideoElement,
  sx: number, sy: number, sw: number, sh: number, w: number, h: number): void {
  const vw = v.videoWidth
  const vh = v.videoHeight
  g.viewport(0, 0, w, h)
  g.bindTexture(g.TEXTURE_2D, cp.tex)
  g.pixelStorei(g.UNPACK_FLIP_Y_WEBGL, false)
  g.texImage2D(g.TEXTURE_2D, 0, g.RGBA, g.RGBA, g.UNSIGNED_BYTE, v)
  g.useProgram(cp.program)
  g.uniform4f(cp.uCrop, sx / vw, sy / vh, sw / vw, sh / vh)
  g.drawArrays(g.TRIANGLES, 0, 3)
}

function setup(): boolean {
  if (gl || broken) return !broken
  try {
    glCanvas = document.createElement('canvas')
    gl = glCanvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false })
    if (!gl) throw new Error('no webgl2')
    const cp = buildCopy(gl)
    program = cp.program
    uCrop = cp.uCrop
    tex = cp.tex
    return true
  } catch {
    broken = true
    gl = null
    return false
  }
}

/** `cg.drawImage(v, sx, sy, sw, sh, 0, 0, w, h)`, colour-exact in WebKit.
 *  Returns false when the WebGL copy is unavailable (the caller draws the
 *  plain way). */
export function drawVideoExact(cg: CanvasRenderingContext2D, v: HTMLVideoElement,
  sx: number, sy: number, sw: number, sh: number, w: number, h: number): boolean {
  if (!setup() || !gl || !glCanvas || !program || !tex) return false
  const vw = v.videoWidth
  const vh = v.videoHeight
  if (!(vw > 0 && vh > 0)) return false
  if (glCanvas.width !== w || glCanvas.height !== h) { glCanvas.width = w; glCanvas.height = h }
  copyFrame(gl, { program, uCrop, tex }, v, sx, sy, sw, sh, w, h)
  cg.drawImage(glCanvas, 0, 0, w, h)
  return true
}

// ---------------------------------------------------------------------------
// The server preview's picture, presented colour-exact (final QA, WebKit).
//
// The default (server) preview shows the composited render in a plain
// <video>, and WebKit presents an untagged video colour-managed through the
// same ~1.96 gamma: a flat 100-grey clip read 109 on screen (98 in the export
// and in Chromium) while the same grey as a PiP — drawn through `drawVideoExact`
// — read 98, so identical footage looked like two different greys side by
// side. In WebKit the <video>'s frames are therefore drawn onto a WebGL canvas
// laid exactly over it (Preview.tsx), and the <video> is hidden under it once
// the canvas has painted: v1, PiPs and CSS blend modes then share one decode.
// ---------------------------------------------------------------------------

export interface ExactPresenter {
  /** Draw `v`'s current frame over the whole canvas; false when it has none. */
  draw: (v: HTMLVideoElement) => boolean
}

/** A presenter on `cv` (its backing size is the caller's), or null when
 *  WebGL2 is unavailable — the caller then leaves the <video> showing. */
export function createExactPresenter(cv: HTMLCanvasElement): ExactPresenter | null {
  try {
    const g = cv.getContext('webgl2', {
      premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false, alpha: false,
    })
    if (!g) return null
    const cp = buildCopy(g)
    return {
      draw: (v) => {
        if (!(v.videoWidth > 0 && v.videoHeight > 0) || v.readyState < 2) return false
        try {
          copyFrame(g, cp, v, 0, 0, v.videoWidth, v.videoHeight, cv.width, cv.height)
          return true
        } catch {
          return false
        }
      },
    }
  } catch {
    return null
  }
}
