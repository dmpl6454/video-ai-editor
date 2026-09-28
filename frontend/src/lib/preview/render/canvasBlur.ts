// The engine's CapCut Canvas BLUR (wave E, lane F2): three WebGL2 passes that
// mirror render/canvas_bg.py's blur branch —
//
//   1. cover: the clip's texture scaled to COVER the small frame (1/4 of the
//      canvas, `canvasBg.blurDims`) with ffmpeg's crop rule, taken through
//      the texture's mipmaps at the downscale's footprint (the export's
//      bicubic downscale is area-like; trilinear is its GPU twin);
//   2. + 3. a separable Gaussian of the export's sigma (edges clamped; the
//      export's gblur steps=4 is ≥ 43 dB from a true Gaussian, measured);
//
// into an RGBA8 texture the geometry pass then samples bilinearly — the
// export's `scale=W:H:flags=bilinear` — wherever the fitted picture does not
// cover the canvas. Rows are stored top-down (row 0 = the top of the frame),
// the same convention as the clip texture, so the geometry pass reads it with
// the same y-down uv.

import { GEOMETRY_VERT } from './shaders/geometry'
import type { Affine, Size } from './geometry'

const COVER_FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_src;
uniform mat3 u_coverToUv;   // small px -> source uv (y down)
uniform float u_lod;
out vec4 outColor;
void main() {
  vec2 uv = (u_coverToUv * vec3(gl_FragCoord.xy, 1.0)).xy;
  outColor = vec4(textureLod(u_src, uv, u_lod).rgb, 1.0);
}
`

/** Largest kernel half-width (3.5 sigma of the heaviest level on a 4K canvas). */
export const MAX_RADIUS = 96

const BLUR_FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_in;
uniform ivec2 u_dir;
uniform ivec2 u_size;
uniform float u_sigma;
uniform int u_radius;
out vec4 outColor;
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  vec3 acc = vec3(0.0);
  float ws = 0.0;
  float k = -0.5 / (u_sigma * u_sigma);
  for (int i = -${MAX_RADIUS}; i <= ${MAX_RADIUS}; i++) {
    if (i < -u_radius || i > u_radius) continue;
    float w = exp(k * float(i * i));
    ivec2 q = clamp(p + u_dir * i, ivec2(0), u_size - 1);
    acc += w * texelFetch(u_in, q, 0).rgb;
    ws += w;
  }
  outColor = vec4(acc / ws, 1.0);
}
`

interface Pass { prog: WebGLProgram; loc: (n: string) => WebGLUniformLocation | null }

export class CanvasBlur {
  private readonly gl: WebGL2RenderingContext
  private cover: Pass | null = null
  private blur: Pass | null = null
  private vao: WebGLVertexArrayObject | null = null
  private tex: [WebGLTexture | null, WebGLTexture | null] = [null, null]
  private fb: [WebGLFramebuffer | null, WebGLFramebuffer | null] = [null, null]
  private size: Size = { w: 0, h: 0 }
  /** What the output texture holds (content id + parameters), to skip work. */
  private holds = ''

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl
  }

  private program(frag: string): Pass {
    const gl = this.gl
    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!
      gl.shaderSource(s, src)
      gl.compileShader(s)
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
        throw new Error(`canvas blur shader: ${gl.getShaderInfoLog(s)}`)
      }
      return s
    }
    const p = gl.createProgram()!
    gl.attachShader(p, compile(gl.VERTEX_SHADER, GEOMETRY_VERT))
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, frag))
    gl.linkProgram(p)
    if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) {
      throw new Error(`canvas blur link: ${gl.getProgramInfoLog(p)}`)
    }
    return { prog: p, loc: (n) => gl.getUniformLocation(p, n) }
  }

  private ensure(size: Size): void {
    const gl = this.gl
    if (!this.cover) this.cover = this.program(COVER_FRAG)
    if (!this.blur) this.blur = this.program(BLUR_FRAG)
    if (!this.vao) this.vao = gl.createVertexArray()
    if (this.size.w === size.w && this.size.h === size.h && this.tex[0]) return
    for (let i = 0; i < 2; i++) {
      if (this.tex[i]) gl.deleteTexture(this.tex[i])
      if (this.fb[i]) gl.deleteFramebuffer(this.fb[i])
      const t = gl.createTexture()!
      gl.bindTexture(gl.TEXTURE_2D, t)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, size.w, size.h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
      const f = gl.createFramebuffer()!
      gl.bindFramebuffer(gl.FRAMEBUFFER, f)
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0)
      this.tex[i] = t
      this.fb[i] = f
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
    this.size = { ...size }
    this.holds = ''
  }

  /** Blurs `src` (bound as-is; its mipmaps already built when `lod` > 0)
   *  into the output texture, unless it already holds `key`. Leaves the
   *  default framebuffer bound; the caller resets viewport and program. */
  run(src: WebGLTexture, key: string, small: Size, coverToUv: Affine, lod: number, sigma: number): WebGLTexture | null {
    const gl = this.gl
    this.ensure(small)
    if (this.holds === key) return this.tex[0]
    gl.bindVertexArray(this.vao)
    gl.viewport(0, 0, small.w, small.h)
    // 1. cover → tex[0]
    const c = this.cover!
    gl.useProgram(c.prog)
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fb[0])
    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, src)
    gl.uniform1i(c.loc('u_src'), 0)
    const m = coverToUv
    gl.uniformMatrix3fv(c.loc('u_coverToUv'), false, new Float32Array([m.a, m.b, 0, m.c, m.d, 0, m.e, m.f, 1]))
    gl.uniform1f(c.loc('u_lod'), lod)
    gl.drawArrays(gl.TRIANGLES, 0, 3)
    // 2. horizontal tex[0] → tex[1]; 3. vertical tex[1] → tex[0]
    const b = this.blur!
    gl.useProgram(b.prog)
    gl.uniform2i(b.loc('u_size'), small.w, small.h)
    gl.uniform1f(b.loc('u_sigma'), Math.max(1e-3, sigma))
    gl.uniform1i(b.loc('u_radius'), Math.min(MAX_RADIUS, Math.ceil(3.5 * sigma)))
    gl.uniform1i(b.loc('u_in'), 0)
    for (const [from, to, dir] of [[0, 1, [1, 0]], [1, 0, [0, 1]]] as const) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fb[to])
      gl.bindTexture(gl.TEXTURE_2D, this.tex[from])
      gl.uniform2i(b.loc('u_dir'), dir[0], dir[1])
      gl.drawArrays(gl.TRIANGLES, 0, 3)
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
    this.holds = key
    return this.tex[0]
  }

  /** Forget what the output holds (its source frame was replaced). */
  invalidate(): void {
    this.holds = ''
  }

  destroy(): void {
    const gl = this.gl
    if (gl.isContextLost()) return
    for (let i = 0; i < 2; i++) {
      if (this.tex[i]) gl.deleteTexture(this.tex[i])
      if (this.fb[i]) gl.deleteFramebuffer(this.fb[i])
    }
    if (this.cover) gl.deleteProgram(this.cover.prog)
    if (this.blur) gl.deleteProgram(this.blur.prog)
    if (this.vao) gl.deleteVertexArray(this.vao)
  }
}
