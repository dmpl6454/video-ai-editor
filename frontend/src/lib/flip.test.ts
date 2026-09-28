import { describe, expect, it } from 'vitest'
import { flipState, toggled } from './flip'

describe('flip state (wave E, F4b)', () => {
  it('reads flip_h / flip_v, and an EDL without them as not flipped', () => {
    expect(flipState({ flip_h: true, flip_v: false })).toEqual({ h: true, v: false })
    expect(flipState({ x: 0 })).toEqual({ h: false, v: false })
    expect(flipState(undefined)).toEqual({ h: false, v: false })
    // only a real boolean counts (a keyframe object or a string is not "on")
    expect(flipState({ flip_h: 'true', flip_v: 1 })).toEqual({ h: false, v: false })
  })
  it('counts the Effects panel Flip H / V as the mirror it draws (review RE)', () => {
    // a clip mirrored by the legacy effect reads as flipped, so the Inspector
    // button shows pressed and its press UNflips (flip_clip folds the effect)
    expect(flipState({ flip_h: false }, [{ type: 'vignette' }, { type: 'hflip' }])).toEqual({ h: true, v: false })
    expect(flipState({ flip_h: true }, [{ type: 'hflip' }])).toEqual({ h: false, v: false })
    expect(flipState({}, [{ type: 'vflip' }, { type: 'vflip' }])).toEqual({ h: false, v: false })
    expect(flipState({}, [{ type: 'vflip' }])).toEqual({ h: false, v: true })
  })
  it('a press flips only its own axis', () => {
    expect(toggled({ h: false, v: true }, 'horizontal')).toEqual({ h: true, v: true })
    expect(toggled({ h: false, v: true }, 'vertical')).toEqual({ h: false, v: false })
  })
})
