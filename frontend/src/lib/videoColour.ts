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

function setup(): boolean {
  if (gl || broken) return !broken
  try {
    glCanvas = document.createElement('canvas')
    gl = glCanvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false })
    if (!gl) throw new Error('no webgl2')
    const sh = (type: number, src: string) => {
      const s = gl!.createShader(type)!
      gl!.shaderSource(s, src)
      gl!.compileShader(s)
      if (!gl!.getShaderParameter(s, gl!.COMPILE_STATUS)) throw new Error(String(gl!.getShaderInfoLog(s)))
      return s
    }
    const p = gl.createProgram()!
    gl.attachShader(p, sh(gl.VERTEX_SHADER, `#version 300 es
uniform vec4 u_crop; out vec2 uv;
void main() {
  vec2 q = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  uv = u_crop.xy + vec2(q.x, 1.0 - q.y) * u_crop.zw;
  gl_Position = vec4(q * 2.0 - 1.0, 0.0, 1.0);
}`))
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, `#version 300 es
precision highp float; in vec2 uv; uniform sampler2D u_t; out vec4 o;
void main() { o = vec4(texture(u_t, uv).rgb, 1.0); }`))
    gl.linkProgram(p)
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(String(gl.getProgramInfoLog(p)))
    program = p
    uCrop = gl.getUniformLocation(p, 'u_crop')
    tex = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
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
  if (!setup() || !gl || !glCanvas || !program) return false
  const vw = v.videoWidth
  const vh = v.videoHeight
  if (!(vw > 0 && vh > 0)) return false
  if (glCanvas.width !== w || glCanvas.height !== h) { glCanvas.width = w; glCanvas.height = h }
  gl.viewport(0, 0, w, h)
  gl.bindTexture(gl.TEXTURE_2D, tex)
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, v)
  gl.useProgram(program)
  gl.uniform4f(uCrop, sx / vw, sy / vh, sw / vw, sh / vh)
  gl.drawArrays(gl.TRIANGLES, 0, 3)
  cg.drawImage(glCanvas, 0, 0, w, h)
  return true
}
