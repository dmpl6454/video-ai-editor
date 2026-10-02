import { describe, expect, it } from 'vitest'
import { initialView } from './viewStore'
import { LAYOUT_PRESETS, MIN_COL_FR, createWorkspaceStore, gridColumns, resizeCols, resizeRows } from './workspaceLayout'
import { ASSET_TABS, RAIL_TO_TAB, SUBNAV } from './assetTabs'
import { durationPill, editedPhrase, filterProjects, sizeLabel } from './projectCards'
import { ratioCommand, ratioOf, sourceFileUrl, stageSize } from './playerControls'
import { exportSpec } from './exportSpec'
import { ctxPlacement } from './contextMenuPlace'
import { clipMenuRows, type MenuContext } from './clipContextMenu'
import { attributesOf, pasteCommands } from './clipAttributes'
import { grade } from './gradeScale'
import type { EDL, Track } from '../types'

describe('view store', () => {
  it('lands returning users in the editor and first runs on Home, with ?view= winning', () => {
    expect(initialView('', 's_1')).toBe('editor')
    expect(initialView('', null)).toBe('home')
    expect(initialView('?view=home', 's_1')).toBe('home')
    expect(initialView('?view=editor', null)).toBe('editor')
  })
})

describe('workspace layouts', () => {
  it('ships the four presets of the handoff', () => {
    expect(LAYOUT_PRESETS.Default.cols).toEqual([38, 32, 30])
    expect(LAYOUT_PRESETS.Vertical.rows).toEqual([62, 38])
    expect(gridColumns(LAYOUT_PRESETS.Media)).toBe('minmax(0, 48fr) 6px minmax(0, 30fr) 6px minmax(0, 22fr)')
  })
  it('a splitter moves fractions between neighbours and never below the floor', () => {
    expect(resizeCols([38, 32, 30], 0, 5)).toEqual([43, 27, 30])
    expect(resizeCols([38, 32, 30], 1, -100)[1]).toBe(MIN_COL_FR)
    expect(resizeRows([57, 43], 100)[1]).toBeGreaterThanOrEqual(22)
  })
  it('persists the choice and overrides, and reset drops only the current layout', () => {
    const kv = new Map<string, string>()
    const store = createWorkspaceStore({ getItem: (k) => kv.get(k) ?? null, setItem: (k, v) => kv.set(k, v), removeItem: (k) => kv.delete(k) })
    store.getState().setLayout('Media')
    store.getState().setSpec({ cols: [50, 30, 20], rows: [60, 40] })
    expect(store.getState().spec().cols).toEqual([50, 30, 20])
    const again = createWorkspaceStore({ getItem: (k) => kv.get(k) ?? null, setItem: () => undefined })
    expect(again.getState().layout).toBe('Media')
    expect(again.getState().spec().cols).toEqual([50, 30, 20])
    store.getState().resetCurrent()
    expect(store.getState().spec()).toEqual(LAYOUT_PRESETS.Media)
  })
})

describe('asset tabs', () => {
  it('keeps the reference order and sub-navigation exactly', () => {
    expect([...ASSET_TABS]).toEqual(['Media', 'Audio', 'Text', 'Stickers', 'Effects', 'Transitions', 'Captions', 'Filters', 'Adjustment', 'Templates', 'AI avatars'])
    expect([...SUBNAV.Media]).toEqual(['Import', 'Media', 'Subprojects', 'Yours', 'AI media', 'Spaces', 'Library'])
    expect([...SUBNAV.Captions]).toEqual(['Auto captions', 'Templates', 'AI packaging', 'Auto lyrics', 'Add captions'])
  })
  it('maps every rail panel onto a tab and a real sub-nav row', () => {
    for (const [, m] of Object.entries(RAIL_TO_TAB)) {
      expect(ASSET_TABS).toContain(m.tab)
      if (m.sub) expect(SUBNAV[m.tab]).toContain(m.sub)
    }
    expect(RAIL_TO_TAB.ai).toEqual({ tab: 'Media', sub: 'AI media' })
  })
})

describe('project cards', () => {
  it('formats the pill, the size and the edited phrase', () => {
    expect(durationPill(646)).toBe('10:46')
    expect(durationPill(3725)).toBe('1:02:05')
    expect(durationPill(null)).toBe('')
    expect(sizeLabel(1.8e9)).toBe('1.8 GB')
    expect(sizeLabel(412e6)).toBe('412 MB')
    expect(sizeLabel(0)).toBe('No media')
    const now = Date.UTC(2026, 9, 2, 12)
    expect(editedPhrase(now / 1000 - 3600, now)).toBe('Edited today')
    expect(editedPhrase(now / 1000 - 86400 * 3, now)).toBe('Edited 3 days ago')
  })
  it('filters by name or id, case-insensitively', () => {
    const rows = [{ id: 's_1', name: 'Interview cut 03' }, { id: 's_2', name: 'Podcast' }]
    expect(filterProjects(rows, 'INTER').map((r) => r.id)).toEqual(['s_1'])
    expect(filterProjects(rows, '').length).toBe(2)
  })
})

describe('player controls', () => {
  it('names the canvas ratio and maps a pick onto the engine tools', () => {
    expect(ratioOf({ w: 1080, h: 1920 })).toBe('9:16')
    expect(ratioOf({ w: 1440, h: 1080 })).toBe('4:3')
    expect(ratioOf({ w: 1000, h: 777 })).toBe('Custom')
    expect(ratioCommand('16:9', { w: 1080, h: 1920 })).toEqual({ tool: 'set_aspect_ratio', args: { ratio: '16:9' } })
    expect(ratioCommand('2.35:1', { w: 1080, h: 1920 })).toEqual({ tool: 'set_canvas', args: { w: 2538, h: 1080 } })
    expect(ratioCommand('9:16', { w: 1080, h: 1920 })).toBeNull()
    expect(ratioCommand('Custom', { w: 1080, h: 1920 })).toBeNull()
  })
  it('fits the stage at Fit and scales the canvas at a percentage', () => {
    expect(stageSize({ w: 1080, h: 1920 }, { w: 1000, h: 500 }, 'Fit')).toEqual({ w: 258, h: 460 })
    expect(stageSize({ w: 1080, h: 1920 }, { w: 1000, h: 500 }, '50%')).toEqual({ w: 540, h: 960 })
  })
  it('streams a session upload and refuses anything else', () => {
    expect(sourceFileUrl('s_1', '/work/s_1/uploads/take 1/a.mp4')).toBe('/api/sessions/s_1/files/uploads/take%201/a.mp4')
    expect(sourceFileUrl('s_1', '/Volumes/ext/a.mp4')).toBeNull()
    expect(sourceFileUrl('s_1', '/work/s_1/uploads/../snapshots/x')).toBeNull()
  })
})

describe('export spec', () => {
  it('keeps the project size when the name matches and takes a custom bitrate', () => {
    const c = { w: 1920, h: 1080, fps: 30 }
    expect(exportSpec(c, '1080P', 'Higher', 0)).toEqual({ shortSide: 0, crf: 18, bitrateKbps: null })
    expect(exportSpec(c, '720P', 'Lower', 0)).toEqual({ shortSide: 720, crf: 28, bitrateKbps: null })
    expect(exportSpec(c, '4K', 'Custom', 24000).bitrateKbps).toBe(24000)
    expect(exportSpec(c, '4K', 'Custom', 5).bitrateKbps).toBeNull()
  })
})

describe('context menu placement', () => {
  it('flips above the cursor when the menu would overflow the window', () => {
    expect(ctxPlacement(100, 100, 300, { w: 1440, h: 900 }).top).toBe(100)
    expect(ctxPlacement(100, 800, 300, { w: 1440, h: 900 }).top).toBe(500)
    expect(ctxPlacement(1400, 100, 300, { w: 1440, h: 900 }).left).toBe(1440 - 280 - 8)
  })
})

function ctx(trackType: string, clip: Record<string, unknown>, extra: Partial<MenuContext> = {}): MenuContext {
  const track = { id: trackType === 'video' ? 'v1' : 'a1', type: trackType, z: 0, clips: [clip] } as unknown as Track
  const edl = { version: 3, duration: 10, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' }, tracks: [track] } as EDL
  const noop = () => undefined
  return {
    edl, track, clip: clip as never, selectedCount: 1, clipboardCount: 0, hasAttributes: false, playhead: 1, key: (c) => c,
    do: { copy: noop, cut: noop, copyAttributes: noop, pasteAttributes: noop, remove: noop, split: noop, splitScenes: noop, transcript: noop,
      isolateVoice: noop, extractAudio: noop, recoverAudio: noop, deactivate: noop, trim: noop, replace: noop, openFile: noop, editEffects: noop,
      freeze: noop, duplicate: noop, muteTrack: noop, lockTrack: noop },
    ...extra,
  }
}

describe('clip context menu', () => {
  const video = { id: 'c1', src: '/a.mp4', in: 0, out: 4, start: 0, audio: {} }
  it('keeps the observed order for a video clip', () => {
    const labels = clipMenuRows(ctx('video', video)).filter((r) => !r.sep).map((r) => r.label)
    expect(labels).toEqual(['Copy', 'Cut', 'Copy attributes', 'Paste attributes', 'Delete', 'Edit', 'Split scenes', 'Transcript', 'Isolate voice',
      'Extract audio', 'Sync video and audio', 'Create compound clip (subproject)', 'Create multi-camera clip', 'Save preset', 'Group', 'Ungroup',
      'Deactivate clip', 'Trim clip', 'Replace clip', 'Link to media', 'Open file location', 'Edit effects', 'Show variable speed animation', 'Range', 'Render'])
  })
  it('enables rows from state, not from a screenshot', () => {
    const rows = clipMenuRows(ctx('video', video))
    const by = (l: string) => rows.find((r) => r.label === l)!
    expect(by('Paste attributes').disabled).toBe(true)
    expect(clipMenuRows(ctx('video', video, { hasAttributes: true })).find((r) => r.label === 'Paste attributes')!.disabled).toBeFalsy()
    expect(by('Extract audio').disabled).toBeFalsy()
    expect(clipMenuRows(ctx('video', { ...video, audio: { mute: true } })).find((r) => r.label === 'Extract audio')!.disabled).toBe(true)
    expect(by('Show variable speed animation').disabled).toBe(true)
    expect(clipMenuRows(ctx('video', { ...video, speed: null })).find((r) => r.label === 'Show variable speed animation')!.disabled).toBe(true)
    expect(clipMenuRows(ctx('video', { ...video, speed: { name: 'hero', points: [] } })).find((r) => r.label === 'Show variable speed animation')!.disabled).toBe(false)
    expect(by('Group').title).toContain('Select two or more')
  })
  it('an audio clip gets the shorter menu and Recover audio only when it was extracted', () => {
    const a = { id: 'a1', src: '/a.mp4', in: 0, out: 4, start: 0, audio: {}, linked_to: 'c1' }
    const edlWithVideo = (c: MenuContext) => ({ ...c, edl: { ...c.edl, tracks: [...c.edl.tracks, { id: 'v1', type: 'video', z: 0, clips: [video] } as unknown as Track] } })
    const rows = clipMenuRows(edlWithVideo(ctx('music', a)))
    expect(rows.filter((r) => !r.sep).map((r) => r.label)).not.toContain('Extract audio')
    expect(rows.find((r) => r.label === 'Recover audio')!.disabled).toBeFalsy()
    expect(clipMenuRows(ctx('music', { ...a, linked_to: null })).find((r) => r.label === 'Recover audio')!.disabled).toBe(true)
  })
})

describe('copy / paste attributes', () => {
  it('carries transform, grade, LUT, speed, animation and audio through the inspector tools', () => {
    const clip = { id: 'c1', src: '/a.mp4', in: 0, out: 4, start: 0, speed: 2, anim_in: 'fade', transform: { scale: 1.5, rotation: 10, x: 0, y: 0, opacity: 1 },
      effects: [{ type: 'color', params: { brightness: 0.1 } }, { type: 'lut', params: { src: 'warm.cube', intensity: 0.5 } }], audio: { gain_db: -3, fade_in: 0.5 } }
    const cmds = pasteCommands('c2', attributesOf(clip as never))
    expect(cmds.map((c) => c.tool)).toEqual(['set_clip_transform', 'color_grade', 'apply_lut', 'set_speed', 'set_animation', 'set_volume', 'add_fade'])
    expect(cmds[0].args).toMatchObject({ clip_id: 'c2', scale: 1.5, rotation: 10 })
    expect(cmds[3].args).toEqual({ clip_id: 'c2', factor: 2 })
  })
})

describe('grade scale', () => {
  it('round-trips the engine ranges through −100…100', () => {
    expect(grade.fromSlider.contrast(grade.toSlider.contrast(1.5))).toBeCloseTo(1.5)
    expect(grade.fromSlider.contrast(grade.toSlider.contrast(0.75))).toBeCloseTo(0.75)
    expect(grade.fromSlider.saturation(100)).toBe(3)
    expect(grade.fromSlider.saturation(-100)).toBe(0)
    expect(grade.toSlider.brightness(0.5)).toBe(100)
  })
})
