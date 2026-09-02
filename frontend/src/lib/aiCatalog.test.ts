// The catalog is the AI panel's contract with the backend: every name must be
// a real dispatch tool (tests/test_features_route.py regexes the literals out
// of aiCatalog.ts for that half), every gate must be a features.py key, and
// the pure helpers around it must fail in the direction that keeps a tool
// runnable when the truth is unknown.
import { describe, expect, it } from 'vitest'
import {
  AI_CATALOG, GATE_KEYS, GROUP_ORDER, clipRequirement, filterCatalog, gateFor,
  groupCatalog, motionTrackSeed, videoClipUnder, type CatalogEntry,
} from './aiCatalog'
import type { FeatureReport } from '../api'
import type { EDL } from '../types'

const entry = (tool: string): CatalogEntry => {
  const e = AI_CATALOG.find((c) => c.tool === tool)
  if (!e) throw new Error(`catalog has no ${tool}`)
  return e
}

const report = (unavailable: string[]): FeatureReport => ({
  packaged_app: false, python: '3.11', anthropic_key_set: true, summary: '',
  available: [],
  unavailable: unavailable.map((key) => ({
    key, feature: `Feature ${key}`, tools: [], fix: `uv sync --extra ${key}`,
  })),
})

// A timeline with one clip per lane kind: v1 video, v2 video, a text overlay,
// a sticker, and a music-lane media clip (the audio-lane trap).
const edl: EDL = {
  version: 2, duration: 20,
  canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: [
      { id: 'main', src: '/m.mp4', in: 0, out: 10, start: 0 },
      { id: 'second', src: '/n.mp4', in: 0, out: 5, start: 10 },
    ] },
    { id: 'v2', type: 'video', z: 1, clips: [{ id: 'pip', src: '/p.mp4', in: 0, out: 4, start: 2 }] },
    { id: 'text', type: 'text', z: 2, clips: [{ id: 'title', text: 'HELLO', start: 1, end: 4 }] },
    { id: 'stickers', type: 'sticker', z: 3, clips: [
      { id: 'emoji', src: '🔥', start: 12, end: 15 } as unknown as EDL['tracks'][number]['clips'][number],
    ] },
    { id: 'music', type: 'music', z: 4, clips: [{ id: 'song', src: '/s.mp3', in: 0, out: 20, start: 0 }] },
  ],
}

describe('AI_CATALOG', () => {
  it('has unique tool names', () => {
    const names = AI_CATALOG.map((e) => e.tool)
    expect(new Set(names).size).toBe(names.length)
  })

  it('leaves chat plumbing and already-buttoned tools out', () => {
    const banned = ['undo', 'redo', 'check_features', 'render_preview', 'save_show_template',
                    'pyannote_status', 'record_voiceover', 'add_music', 'reorder_clips', 'apply_hook_stack']
    for (const e of AI_CATALOG) {
      expect(banned, e.tool).not.toContain(e.tool)
      expect(e.tool, e.tool).not.toMatch(/^(get|list|repair)_/)
    }
  })

  it('only gates on real feature keys (never gpu_transcribe)', () => {
    for (const e of AI_CATALOG) {
      if (e.gate) expect(GATE_KEYS as readonly string[]).toContain(e.gate)
    }
    expect(GATE_KEYS as readonly string[]).not.toContain('gpu_transcribe')
  })

  it('requires a clip for exactly the per-clip tools, split by lane kind', () => {
    const media = ['noise_reduce', 'vocal_isolate', 'instrumental_isolate']
    const video = ['upscale', 'stabilize', 'smooth_slow_motion', 'remove_background', 'chroma_key', 'object_erase']
    for (const t of media) expect(entry(t).needsClip, t).toBe('media')
    for (const t of video) expect(entry(t).needsClip, t).toBe('video')
    const withClip = AI_CATALOG.filter((e) => e.needsClip).map((e) => e.tool).sort()
    expect(withClip).toEqual([...media, ...video].sort())
    // motion_track's gesture is selecting the OVERLAY — explicit selects instead.
    expect(entry('motion_track').needsClip).toBeUndefined()
  })

  it('hides every injected clip_id so buildArgs never sees it', () => {
    for (const e of AI_CATALOG) {
      if (e.needsClip) expect(e.hide, e.tool).toContain('clip_id')
    }
  })

  it('every group in use is in GROUP_ORDER', () => {
    for (const e of AI_CATALOG) expect(GROUP_ORDER).toContain(e.group)
  })
})

describe('gateFor', () => {
  it('greys out a tool whose feature is unavailable, with the fix verbatim', () => {
    const r = gateFor(entry('upscale'), report(['upscale']))
    expect(r).toEqual({ ok: false, feature: 'Feature upscale', fix: 'uv sync --extra upscale', packagedExcluded: false })
  })

  it('passes an available feature and an ungated tool', () => {
    expect(gateFor(entry('upscale'), report(['stems']))).toEqual({ ok: true, checking: false })
    expect(gateFor(entry('add_hook_overlay'), report(['upscale']))).toEqual({ ok: true, checking: false })
  })

  it('reports "checking" while features are unknown — never unavailable', () => {
    expect(gateFor(entry('upscale'), null)).toEqual({ ok: true, checking: true })
    expect(gateFor(entry('add_hook_overlay'), null)).toEqual({ ok: true, checking: false })
  })

  it('never lets gpu_transcribe (a speed tier) block auto_caption', () => {
    expect(gateFor(entry('auto_caption'), report(['gpu_transcribe']))).toEqual({ ok: true, checking: false })
  })

  it('lets search_media run transcript-only without the CLIP model', () => {
    const e = entry('search_media')
    expect(gateFor(e, report(['visual_search']), { scope: 'spoken' })).toEqual({ ok: true, checking: false })
    expect(gateFor(e, report(['visual_search']), { scope: 'both' }).ok).toBe(false)
    expect(gateFor(e, report(['visual_search'])).ok).toBe(false)
  })

  it('surfaces packaged_app_excluded', () => {
    const r = report(['stems'])
    r.unavailable[0].packaged_app_excluded = true
    expect(gateFor(entry('vocal_isolate'), r)).toMatchObject({ ok: false, packagedExcluded: true })
  })

  it('lets auto_reframe centre-crop without the tracker', () => {
    const e = entry('auto_reframe')
    expect(gateFor(e, report(['tracking']), { subject_track: false })).toEqual({ ok: true, checking: false })
    expect(gateFor(e, report(['tracking']), { subject_track: true }).ok).toBe(false)
  })

  it('never gates add_caption_track — it only reads a transcript that already exists', () => {
    expect(entry('add_caption_track').gate).toBeUndefined()
    expect(gateFor(entry('add_caption_track'), report(['captions']))).toEqual({ ok: true, checking: false })
  })
})

describe('runAsJob', () => {
  it('is reserved for tools that genuinely run for seconds — instant edits stay synchronous', () => {
    // chroma_key sets four fields and commits; find_broll is a filename scan.
    // Routing either through the two-worker job pool would queue a 5 ms edit
    // behind an export.
    expect(entry('chroma_key').runAsJob).toBeUndefined()
    expect(entry('find_broll').runAsJob).toBeUndefined()
    for (const t of ['remove_silences', 'auto_reframe', 'translate_captions', 'find_moments']) {
      expect(entry(t).runAsJob, t).toBe(true)
    }
  })
})

describe('filterCatalog / groupCatalog', () => {
  it('matches on label, tool name and description, case-insensitively', () => {
    const hit = filterCatalog(AI_CATALOG, 'silen').map((e) => e.tool)
    expect(hit).toContain('remove_silences')
    expect(hit).not.toContain('upscale')
    expect(filterCatalog(AI_CATALOG, 'ESRGAN').map((e) => e.tool)).toEqual(['upscale'])
    expect(filterCatalog(AI_CATALOG, '   ')).toHaveLength(AI_CATALOG.length)
  })

  it('groups in GROUP_ORDER and drops empty groups', () => {
    const groups = groupCatalog(AI_CATALOG).map((g) => g.group)
    expect(groups).toEqual(GROUP_ORDER.filter((g) => AI_CATALOG.some((e) => e.group === g)))
    const only = groupCatalog(filterCatalog(AI_CATALOG, 'upscale'))
    expect(only).toHaveLength(1)
    expect(only[0].group).toBe('Enhance')
  })
})

describe('clipRequirement', () => {
  it('passes tools that need no clip', () => {
    expect(clipRequirement(entry('add_hook_overlay'), edl, null)).toEqual({ ok: true, clipId: null })
  })

  it('rejects a text clip and nothing selected', () => {
    expect(clipRequirement(entry('upscale'), edl, 'title')).toEqual({ ok: false, reason: 'Select a video clip on the timeline' })
    expect(clipRequirement(entry('upscale'), edl, null).ok).toBe(false)
    expect(clipRequirement(entry('noise_reduce'), null, 'main').ok).toBe(false)
  })

  it('checks the TRACK type for video tools — a music-lane clip is a 422 waiting to happen', () => {
    expect(clipRequirement(entry('upscale'), edl, 'song')).toEqual({ ok: false, reason: 'Select a video clip on the timeline' })
    expect(clipRequirement(entry('upscale'), edl, 'pip')).toEqual({ ok: true, clipId: 'pip' })
    expect(clipRequirement(entry('stabilize'), edl, 'main')).toEqual({ ok: true, clipId: 'main' })
  })

  it('accepts any media clip, audio lanes included, for media tools', () => {
    expect(clipRequirement(entry('noise_reduce'), edl, 'song')).toEqual({ ok: true, clipId: 'song' })
    expect(clipRequirement(entry('noise_reduce'), edl, 'title')).toEqual({ ok: false, reason: 'Select a clip with audio on the timeline' })
  })
})

describe('videoClipUnder / motionTrackSeed', () => {
  it('finds the v1 clip covering a time, ignoring v2', () => {
    expect(videoClipUnder(edl, 3)).toBe('main')
    expect(videoClipUnder(edl, 10)).toBe('second')
    expect(videoClipUnder(edl, 19)).toBeNull()
  })

  it('seeds motion_track from a selected sticker: target, source clip and its box', () => {
    const seed = motionTrackSeed(edl, 'emoji')
    expect(seed.target_id).toBe('emoji')
    expect(seed.clip_id).toBe('second')
    // Default sticker: centred, 22% of the long side (lib/overlay.ts).
    expect(seed.bbox).toHaveLength(4)
    const [x, y, w, h] = seed.bbox!
    expect(x).toBeGreaterThanOrEqual(0)
    expect(y).toBeGreaterThanOrEqual(0)
    expect(x + w).toBeLessThanOrEqual(1)
    expect(y + h).toBeLessThanOrEqual(1)
    expect(w).toBeCloseTo((1920 * 0.22) / 1080, 2)
    expect(h).toBeCloseTo(0.22, 2)
  })

  it('seeds a text overlay without a box and nothing for a video or missing selection', () => {
    expect(motionTrackSeed(edl, 'title')).toEqual({ target_id: 'title', clip_id: 'main' })
    expect(motionTrackSeed(edl, 'main')).toEqual({})
    expect(motionTrackSeed(edl, null)).toEqual({})
    expect(motionTrackSeed(null, 'emoji')).toEqual({})
  })
})
