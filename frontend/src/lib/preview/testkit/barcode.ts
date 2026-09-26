// Frame identity bar for engine tests (INSTANT_PREVIEW_SPEC §13): the fixture
// sources burn 15 cells of BAR_CELL px into rows 0..39 — bits 0..10 are the
// source frame index, bits 11..14 the source id (1..15, never 0, so a real
// frame always has a white cell). Mirrors tests/wk/proxy_fixture.py.

export const BAR_CELL = 48
export const FRAME_BITS = 11
export const SRC_BITS = 4
export const BAR_BITS = FRAME_BITS + SRC_BITS
export const BAR_ROW = 20
export const BAR_WIDTH = BAR_BITS * BAR_CELL

/** No picture: every cell read dark (a black texture, or nothing decoded). */
export const NO_PICTURE = -1

export const barCode = (srcId: number, frame: number): number => (srcId << FRAME_BITS) | frame
export const barSrc = (code: number): number => code >> FRAME_BITS
export const barFrame = (code: number): number => code & ((1 << FRAME_BITS) - 1)

/** Decodes one RGBA row (at least BAR_WIDTH px) read from the bar. */
export function decodeBarRow(rgba: Uint8Array): number {
  if (rgba.length < BAR_WIDTH * 4) throw new Error(`bar row needs ${BAR_WIDTH} px`)
  let code = 0
  for (let bit = 0; bit < BAR_BITS; bit++) {
    if (rgba[(bit * BAR_CELL + BAR_CELL / 2) * 4 + 1] > 128) code |= 1 << bit
  }
  return barSrc(code) === 0 ? NO_PICTURE : code
}

/** Reads the bar of whatever frame a <video> would paint now, through
 *  `texImage2D(video)` — the same upload the compositor uses. */
export class BarReader {
  private readonly gl: WebGL2RenderingContext
  private readonly tex: WebGLTexture
  private readonly fb: WebGLFramebuffer
  private readonly px = new Uint8Array(BAR_WIDTH * 4)

  constructor() {
    const canvas = document.createElement('canvas')
    canvas.width = 4
    canvas.height = 4
    const gl = canvas.getContext('webgl2')
    if (!gl) throw new Error('no WebGL2')
    this.gl = gl
    this.tex = gl.createTexture()!
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST)
    this.fb = gl.createFramebuffer()!
  }

  read(video: HTMLVideoElement): number {
    const gl = this.gl
    gl.bindTexture(gl.TEXTURE_2D, this.tex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video)
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fb)
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex, 0)
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) return NO_PICTURE
    gl.readPixels(0, BAR_ROW, BAR_WIDTH, 1, gl.RGBA, gl.UNSIGNED_BYTE, this.px)
    return decodeBarRow(this.px)
  }
}
