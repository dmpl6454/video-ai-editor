// The 2-D draw of a clip animation (overlays and stickers) and the
// Inspector's tile previews — both from the plan, never a hand-made copy.
import { describe, expect, it } from 'vitest'
import { applyPose, drawWithBlur } from './animDraw'
import { ANIM_TABLE, planOf, poseAt, REST_POSE, type AnimPose } from './clipAnim'
import { previewKeyframes } from '../../components/anim/AnimationSection'

/** A context that records what is done to it, with a real 2-D affine. */
function fakeCtx() {
  const calls: string[] = []
  let m = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 }
  const ctx = {
    globalAlpha: 1,
    canvas: { width: 100, height: 100, clientWidth: 100 },
    translate(x: number, y: number) { calls.push(`t ${x.toFixed(3)} ${y.toFixed(3)}`); m = { ...m, e: m.a * x + m.c * y + m.e, f: m.b * x + m.d * y + m.f } },
    rotate(r: number) {
      calls.push(`r ${r.toFixed(4)}`)
      const cos = Math.cos(r), sin = Math.sin(r)
      m = { a: m.a * cos + m.c * sin, b: m.b * cos + m.d * sin, c: -m.a * sin + m.c * cos, d: -m.b * sin + m.d * cos, e: m.e, f: m.f }
    },
    scale(x: number, y: number) { calls.push(`s ${x.toFixed(4)} ${y.toFixed(4)}`); m = { ...m, a: m.a * x, b: m.b * x, c: m.c * y, d: m.d * y } },
    getTransform: () => m,
  }
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls, matrix: () => m }
}

describe('applyPose', () => {
  it('travels in canvas axes, then turns and zooms about the element centre, and fades', () => {
    const { ctx, matrix } = fakeCtx()
    ctx.translate(50, 40)                       // the element's centre
    const own = Math.PI / 6
    ctx.rotate(own)                             // its own rotation
    ctx.globalAlpha = 0.8
    const pose: AnimPose = { dx: 0.1, dy: -0.05, scale: 0.5, rotation: 90, alpha: 0.5, blur: 0 }
    applyPose(ctx, pose, own, { w: 200, h: 100 })
    const m = matrix()
    // the element's centre moved by (0.1·200, −0.05·100) in CANVAS axes
    expect(m.e).toBeCloseTo(50 + 20, 9)
    expect(m.f).toBeCloseTo(40 - 5, 9)
    // total turn = own + 90°, total zoom 0.5
    expect(Math.atan2(m.b, m.a)).toBeCloseTo(own + Math.PI / 2, 9)
    expect(Math.hypot(m.a, m.b)).toBeCloseTo(0.5, 9)
    expect(ctx.globalAlpha).toBeCloseTo(0.4, 12)
  })

  it('the rest pose leaves the transform and the alpha alone', () => {
    const { ctx, matrix } = fakeCtx()
    ctx.translate(10, 20)
    applyPose(ctx, REST_POSE, 0.3, { w: 100, h: 100 })
    const m = matrix()
    expect([m.a, m.b, m.c, m.d, m.e, m.f].map((v) => +v.toFixed(12))).toEqual([1, 0, 0, 1, 10, 20])
    expect(ctx.globalAlpha).toBe(1)
  })
})

describe('drawWithBlur', () => {
  it('draws straight onto the canvas when there is no blur', () => {
    const { ctx } = fakeCtx()
    let n = 0
    drawWithBlur(ctx, REST_POSE, 360, (c) => { n++; expect(c).toBe(ctx) })
    expect(n).toBe(1)
  })
})

describe('Inspector tile previews', () => {
  it('each tile loops the preset\'s own plan, ending an In and starting an Out at rest', () => {
    for (const p of [...ANIM_TABLE.in, ...ANIM_TABLE.out, ...ANIM_TABLE.combo]) {
      const kf = previewKeyframes(p)
      expect(kf[0].offset).toBe(0)
      expect(kf[kf.length - 1].offset).toBe(1)
      const rest = 'translate(0.00px, 0.00px) rotate(0.00deg) scale(1.0000)'
      if (p.kind === 'in') expect(kf[kf.length - 1].transform).toBe(rest)
      if (p.kind === 'out') expect(kf[0].transform).toBe(rest)
    }
    const slide = previewKeyframes(ANIM_TABLE.in.find((p) => p.id === 'slide_left')!)
    expect(slide[0].transform).toBe('translate(40.00px, 0.00px) rotate(0.00deg) scale(1.0000)')
    const fade = previewKeyframes(ANIM_TABLE.out.find((p) => p.id === 'fade_out')!)
    expect(Number(fade[fade.length - 1].opacity)).toBe(0)
  })

  it('a combo tile shows the same motion the renderers draw', () => {
    const p = ANIM_TABLE.combo.find((q) => q.id === 'rock')!
    const kf = previewKeyframes(p)
    const plan = planOf({ anim_combo: 'rock' }, 2)
    const mid = poseAt(plan, 0.25)
    const i = kf.findIndex((k) => Math.abs((k.offset as number) * 2 - 0.25) < 1e-9)
    expect(i).toBeGreaterThan(0)
    expect(kf[i].transform).toContain(`rotate(${mid.rotation.toFixed(2)}deg)`)
  })
})
