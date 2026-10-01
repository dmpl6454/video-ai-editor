// Editor Brain (EB1): History rows for lane B's three tools speak editor
// language — "Cut {n} ranges", "Camera plan: {n} switches", "Dialogue lane
// synced" — and a restored version reads as one.
//
// The summaries below are the EXACT strings the handlers in
// agent/dispatch.py emit (cut_source_ranges, apply_camera_plan,
// sync_dialogue_lane) — every optional clause present once and absent once.
// The detail drops what the title already says and names lanes as the
// editor does (V1, A1).
import { describe, expect, it } from 'vitest'
import { brainOpLabel, opLabel, toolTitle } from './opLabels'

const CUT_FULL = 'Cut 14 ranges on v1 (12.40 s removed; 2 skipped) — silences and fillers'
const CUT_BARE = 'Cut 1 ranges on v1 (0.80 s removed)'
const CAM_FULL = "Camera plan: 6 switches (13 pieces, 1 skipped, 2 clamped to the angle's length; derived_from camA.mp4, camB.mp4)"
const CAM_BARE = 'Camera plan: 1 switches (3 pieces; derived_from camA.mp4)'
const DLG_FULL = 'Dialogue lane synced: 12 pieces of recorder.wav on a1, 11 camera clips muted, 1 gap; the recorder was on the Music lane; it is now the dialogue'
const DLG_BARE = 'Dialogue lane synced: 3 pieces of talk.mp4 on a1'

describe('brain op labels (EB1-B)', () => {
  it('Cut {n} ranges — from the args, else the summary', () => {
    const l = opLabel({ tool: 'cut_source_ranges', summary: CUT_FULL,
      args: { track: 'v1', ranges: Array.from({ length: 14 }, () => ({ src: '/x/a.mp4', start: 1, end: 2 })) } })
    expect(l.title).toBe('Cut 14 ranges')
    expect(l.detail).toBe('on V1 (12.40 s removed; 2 skipped) — silences and fillers')
    expect(l.raw).toBe(`cut_source_ranges — ${CUT_FULL}`)
    expect(opLabel({ tool: 'cut_source_ranges', summary: CUT_FULL }).title).toBe('Cut 14 ranges')
    const one = opLabel({ tool: 'cut_source_ranges', summary: CUT_BARE })
    expect(one.title).toBe('Cut 1 range')
    expect(one.detail).toBe('on V1 (0.80 s removed)')
  })
  it('Camera plan: {n} switches', () => {
    const l = opLabel({ tool: 'apply_camera_plan', summary: CAM_FULL,
      args: { switches: [{}, {}, {}, {}, {}, {}], offsets: {} } })
    expect(l.title).toBe('Camera plan: 6 switches')
    expect(l.detail).toBe("13 pieces, 1 skipped, 2 clamped to the angle's length; from camA.mp4, camB.mp4")
    expect(opLabel({ tool: 'apply_camera_plan', summary: CAM_FULL }).title).toBe('Camera plan: 6 switches')
    const one = opLabel({ tool: 'apply_camera_plan', summary: CAM_BARE })
    expect(one.title).toBe('Camera plan: 1 switch')
    expect(one.detail).toBe('3 pieces; from camA.mp4')
  })
  it('Dialogue lane synced', () => {
    const l = opLabel({ tool: 'sync_dialogue_lane', summary: DLG_FULL, args: { src: '/x/recorder.wav', lane: 'a1' } })
    expect(l.title).toBe('Dialogue lane synced')
    expect(l.detail).toBe('12 pieces of recorder.wav on A1, 11 camera clips muted, 1 gap; the recorder was on the Music lane; it is now the dialogue')
    expect(l.raw).toBe(`sync_dialogue_lane — ${DLG_FULL}`)
    expect(opLabel({ tool: 'sync_dialogue_lane', summary: DLG_BARE }).detail).toBe('3 pieces of talk.mp4 on A1')
  })
  it('is only for the brain tools; a restored version has a plain title', () => {
    expect(brainOpLabel({ tool: 'split_at', summary: 'Split at 5.00s on v1' })).toBeNull()
    expect(toolTitle('restore_version')).toBe('Restore version')
    expect(opLabel({ tool: 'restore_version', summary: 'Restored V1 Reel' })).toMatchObject({
      title: 'Restore version', detail: 'Restored V1 Reel' })
  })
})
