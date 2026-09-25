// The AI panel's catalog: which dispatch tools get a card, how they group,
// what each form needs beyond the raw /api/tools schema, and which optional
// feature (ai/features.py key) each one depends on.
//
// Pure: runtime imports only from types.ts and lib/overlay.ts (sticker
// geometry for the motion_track seed) — never store.ts — so vitest can load it
// under node and the backend can regex the `tool: '…'` literals to assert every
// entry is actually advertised (tests/test_features_route.py).
//
// Deliberately NOT here: `add_music` (MediaBin's "Add music…" does upload+add),
// `reorder_clips` (timeline drag), `apply_hook_stack` (not in /api/tools), and
// every get_*/list_*/repair_*/undo/redo/check_features/render_preview/
// save_show_template/pyannote_status/record_voiceover — those are chat plumbing,
// not things an editor clicks.
//
// Cancel / % are NOT catalog data: /api/tools reports `cancellable` and
// `reports_progress` per tool from the handler signature, and the card reads
// those. Today only auto_caption has either.

import type { FeatureReport } from '../api'
import { clipEnd, isMediaClip, type Canvas, type EDL } from '../types'
import { isSticker, stickerGeom, type StickerClip } from './overlay'

export type AiGroup =
  | 'Auto edit' | 'Captions & speech' | 'Audio' | 'Enhance'
  | 'Cutout & effects' | 'Text & brand' | 'Find & search' | 'Export'

// features.py keys minus `gpu_transcribe`, which is a speed tier rather than a
// capability — it must never grey out auto_caption.
export const GATE_KEYS = [
  'captions', 'noise_reduce', 'stems', 'bg_remove', 'visual_search', 'diarize',
  'tracking', 'object_erase', 'beats', 'tts', 'translate', 'stabilize',
  'upscale', 'interpolate',
] as const
export type GateKey = typeof GATE_KEYS[number]

export type Widget =
  | 'number' | 'text' | 'select' | 'checkbox' | 'time' | 'list' | 'bbox'
  | 'mapping' | 'path' | 'clipSelect' | 'file'

export interface FieldOverride {
  widget?: Widget; default?: unknown; options?: (string | number)[]; label?: string; hidden?: boolean
  defaultFrom?: 'playhead' | 'inMark' | 'outMark' | 'playheadPlus3' | 'captionTargetPref' | 'captionSpeedPref'
  nullable?: { label: string }; clipFilter?: 'overlay' | 'video'; accept?: string
}

export interface CatalogEntry {
  tool: string; group: AiGroup; label: string; description: string
  gate?: GateKey; gateUnless?: { field: string; equals: unknown }
  keyHint?: string                       // informational badge when !anthropic_key_set; never disables
  needsClip?: 'video' | 'media'          // 'video' = media clip on a track whose type === 'video'; 'media' = any media clip (audio lanes too)
  readOnly?: boolean                     // no timeline mutation — the card renders the result view
  runAsJob?: boolean                     // force runDispatchJob even when not in ASYNC_DISPATCH_TOOLS
  advanced?: boolean                     // path-typing tools: badge "advanced · source install"
  /** Search-only terms (the engine names a pro may type — "ESRGAN", "Demucs"),
   *  never displayed: the card's copy speaks editor language (QA-101). */
  keywords?: string
  fields?: Record<string, FieldOverride>; hide?: string[]; order?: string[]
}

export const GROUP_ORDER: readonly AiGroup[] = [
  'Auto edit', 'Captions & speech', 'Audio', 'Enhance',
  'Cutout & effects', 'Text & brand', 'Find & search', 'Export',
]

const NO_KEY_HINT_VISION = 'vision verify needs ANTHROPIC_API_KEY — transcript-only until then'
const NO_KEY_HINT_HOOK = 'uses a transcript heuristic until ANTHROPIC_API_KEY is set'
const TRACK_CHOICES = ['v1', 'v2']
// The handlers' own bbox default (dispatch.py::motion_track) — a visible,
// draggable starting box beats four blank inputs the user can't picture.
const BBOX_SEED = [0.4, 0.4, 0.2, 0.2]

export const AI_CATALOG: readonly CatalogEntry[] = [
  // ---- Auto edit --------------------------------------------------------
  { tool: 'remove_silences', keywords: 'ripple', group: 'Auto edit', label: 'Remove silences',
    description: 'Find the pauses in a track, cut them out and close the gaps. The defaults suit talking-head speech.',
    runAsJob: true,
    fields: { track: { widget: 'select', options: TRACK_CHOICES, default: 'v1' } } },
  { tool: 'remove_fillers', group: 'Auto edit', label: 'Remove filler words',
    description: 'Cut “um”, “uh”, “like” and “you know” and close the gaps.',
    runAsJob: true,
    fields: { words: { label: 'Words (blank = the built-in list)' },
              track: { widget: 'select', options: TRACK_CHOICES, default: 'v1' } } },
  { tool: 'auto_cut_to_beats', group: 'Auto edit', label: 'Cut to the beat',
    description: 'Cut the main video on every Nth beat of the music. Add music to the timeline first.',
    gate: 'beats', runAsJob: true },
  { tool: 'auto_reframe', group: 'Auto edit', label: 'Auto-reframe',
    description: 'Change the aspect ratio and reframe every clip to keep the subject in shot.',
    // The handler only imports ai.reframe when subject_track is on; the
    // centre-crop path needs no tracker, so an untracked run stays available
    // on a packaged app / no-opencv install.
    gate: 'tracking', gateUnless: { field: 'subject_track', equals: false }, runAsJob: true,
    fields: { ratio: { default: '9:16' },
              subject_track: { widget: 'checkbox', default: true, label: 'Track the subject (else centre-crop)' } } },
  { tool: 'make_shorts', group: 'Auto edit', label: 'Find shorts',
    description: 'Pick the strongest moments from the main video. Optionally save each one as its own project.',
    readOnly: true, runAsJob: true },
  { tool: 'cut_range', keywords: 'ripple', group: 'Auto edit', label: 'Cut range',
    description: 'Remove the range between your In and Out marks and close the gap.',
    hide: ['dry_run'],
    fields: { track: { widget: 'select', options: TRACK_CHOICES, default: 'v1' },
              start: { widget: 'time', defaultFrom: 'inMark', label: 'Start (In mark)' },
              end: { widget: 'time', defaultFrom: 'outMark', label: 'End (Out mark)' } } },
  { tool: 'multicam', group: 'Auto edit', label: 'Multicam switch',
    description: 'Sync several camera angles by their audio and cut between them on the main video.',
    advanced: true,
    fields: { srcs: { widget: 'list', label: 'Angle files — absolute paths, first = sync reference' } } },

  // ---- Captions & speech -----------------------------------------------
  { tool: 'auto_caption', keywords: 'Whisper', group: 'Captions & speech', label: 'Auto captions',
    description: 'Transcribe the speech again at the highest accuracy and lay the captions on the captions track.',
    gate: 'captions',
    fields: { target: { defaultFrom: 'captionTargetPref', label: 'Caption language (blank = as spoken)' },
              language: { label: 'Spoken language (blank = auto-detect)' },
              model: { widget: 'select', options: ['large-v3-turbo'], defaultFrom: 'captionSpeedPref',
                       label: 'Model (blank = large-v3, most accurate)' } } },
  // Ungated on purpose: the handler only reads a transcript that already
  // exists (Whisper's, or an imported subtitle file) — it never touches the
  // ASR stack, so a packaged Mac without faster-whisper can still run it.
  { tool: 'add_caption_track', group: 'Captions & speech', label: 'Captions from transcript',
    description: 'Put the existing transcript (or an imported subtitle file) on the captions track, without transcribing again. Choosing another style keeps captions you edited by hand.',
    // QA-074: a restyle keeps hand-edited cues; this opts into re-laying them.
    fields: { rebuild: { label: 'Re-lay every caption from the transcript (discards hand edits)' } } },
  { tool: 'translate_captions', keywords: 'MADLAD', group: 'Captions & speech', label: 'Translate captions',
    description: 'Translate the captions on this Mac. The first run downloads a translation model (about 3 GB).',
    gate: 'translate', runAsJob: true,
    fields: { target_lang: { widget: 'select', options: ['hi', 'en', 'es', 'fr', 'de', 'pt', 'ja', 'ko', 'zh'], default: 'hi' },
              source_lang: { label: 'Source language (blank = detected)' } } },
  { tool: 'diarize', keywords: 'diarization pyannote', group: 'Captions & speech', label: 'Detect speakers',
    description: 'Find who speaks when, so captions can be coloured by speaker. Doesn’t change the timeline.',
    gate: 'diarize', readOnly: true, runAsJob: true },
  { tool: 'assign_caption_speakers', keywords: 'diarization', group: 'Captions & speech', label: 'Colour captions by speaker',
    description: 'Detect the speakers and colour each one’s captions from the brand palette.',
    gate: 'diarize', runAsJob: true, hide: ['turns'] },
  { tool: 'name_speakers', group: 'Captions & speech', label: 'Name speakers',
    description: 'Give the detected speakers names for lower thirds (for example Speaker 1 = Host).',
    fields: { mapping: { label: 'Mapping — one SPEAKER_XX=Name per line' } } },
  { tool: 'import_srt', group: 'Captions & speech', label: 'Import subtitles',
    description: 'Replace the transcript with a .srt / .vtt / .ass file, then run “Captions from transcript” to lay it on the timeline.',
    fields: { path: { widget: 'file', accept: '.srt,.vtt,.ass', label: 'Subtitle file' } } },
  { tool: 'export_srt', group: 'Captions & speech', label: 'Export .srt',
    description: 'Save the captions as a .srt subtitle file. Leave the destination blank to save it with the project.',
    readOnly: true, fields: { path: { label: 'Destination (blank = session folder)' } } },
  { tool: 'export_vtt', group: 'Captions & speech', label: 'Export .vtt',
    description: 'Save the captions as a .vtt subtitle file. Leave the destination blank to save it with the project.',
    readOnly: true, fields: { path: { label: 'Destination (blank = session folder)' } } },
  { tool: 'export_ass', group: 'Captions & speech', label: 'Export .ass',
    description: 'Save the captions as a styled .ass subtitle file. Leave the destination blank to save it with the project.',
    readOnly: true, fields: { path: { label: 'Destination (blank = session folder)' } } },

  // ---- Audio ------------------------------------------------------------
  { tool: 'noise_reduce', group: 'Audio', label: 'Reduce noise',
    description: 'Spectrally denoise the selected clip’s audio (hiss, fans, room tone).',
    gate: 'noise_reduce', needsClip: 'media', runAsJob: true, hide: ['clip_id'] },
  { tool: 'vocal_isolate', keywords: 'Demucs stems', group: 'Audio', label: 'Isolate vocals',
    description: 'Separate the voice onto the voiceover track and mute the clip’s own audio.',
    gate: 'stems', needsClip: 'media', hide: ['clip_id'] },
  { tool: 'instrumental_isolate', keywords: 'Demucs stems', group: 'Audio', label: 'Isolate instrumental',
    description: 'Separate everything except the voice onto the music track and mute the clip’s own audio.',
    gate: 'stems', needsClip: 'media', hide: ['clip_id'] },
  { tool: 'tts_voiceover', keywords: 'Piper tts', group: 'Audio', label: 'AI voiceover',
    description: 'Read your text aloud onto the voiceover track. The voice downloads on first use (about 60 MB).',
    gate: 'tts', runAsJob: true,
    fields: { start: { widget: 'time', defaultFrom: 'playhead' } } },

  // ---- Enhance ----------------------------------------------------------
  { tool: 'upscale', keywords: 'Real-ESRGAN esrgan', group: 'Enhance', label: 'AI upscale',
    description: 'Sharpen the selected clip to 2× or 4× its resolution. Takes about a second per frame.',
    gate: 'upscale', needsClip: 'video', hide: ['clip_id'] },
  { tool: 'stabilize', keywords: 'vidstab', group: 'Enhance', label: 'Stabilize',
    description: 'Smooth out camera shake on the selected clip. Slow: it reads the footage twice.',
    gate: 'stabilize', needsClip: 'video', hide: ['clip_id'] },
  { tool: 'smooth_slow_motion', keywords: 'RIFE interpolation', group: 'Enhance', label: 'Smooth slow-mo',
    description: 'Slow the selected clip down 2× or 4× with generated in-between frames.',
    gate: 'interpolate', needsClip: 'video', hide: ['clip_id'],
    fields: { factor: { widget: 'select', options: [2, 4], default: 2 } } },

  // ---- Cutout & effects -------------------------------------------------
  { tool: 'remove_background', keywords: 'rembg', group: 'Cutout & effects', label: 'Remove background',
    description: 'Cut the subject out of the selected clip, onto green for keying or onto transparency.',
    gate: 'bg_remove', needsClip: 'video', hide: ['clip_id'],
    fields: { bg_color: { label: 'Background colour', nullable: { label: 'True alpha (no fill colour)' } } } },
  { tool: 'chroma_key', group: 'Cutout & effects', label: 'Chroma key',
    description: 'Key out a green or blue screen on the selected clip.',
    // Sets four fields and commits — instant. Not a job: it would queue
    // behind an export in api/jobs.py's two-worker pool for a 5 ms edit.
    needsClip: 'video', hide: ['clip_id'],
    fields: { color: { label: 'Key colour', nullable: { label: 'Auto (clear an existing key)' } } } },
  { tool: 'object_erase', keywords: 'LaMa inpaint', group: 'Cutout & effects', label: 'Erase object',
    description: 'Paint out an object inside a box on the selected clip for a stretch of time. Draw the box on the preview.',
    gate: 'object_erase', needsClip: 'video', hide: ['clip_id'],
    fields: { bbox: { default: BBOX_SEED, label: 'Box (x, y, w, h as fractions of the frame)' },
              t_end: { label: 'End (blank = clip end)' } } },
  { tool: 'motion_track', group: 'Cutout & effects', label: 'Motion-track an overlay',
    description: 'Follow a box through the video and write its path as x/y keyframes on a sticker or text overlay.',
    gate: 'tracking',
    order: ['target_id', 'clip_id', 'bbox', 'method', 'sample_every'],
    fields: { target_id: { widget: 'clipSelect', clipFilter: 'overlay', label: 'Overlay to animate' },
              clip_id: { widget: 'clipSelect', clipFilter: 'video', label: 'Source video clip' },
              bbox: { default: BBOX_SEED, label: 'Box to follow (x, y, w, h as fractions of the frame)' } } },

  // ---- Text & brand -----------------------------------------------------
  { tool: 'add_hook_overlay', group: 'Text & brand', label: 'Hook overlay',
    description: 'A bold hook line over the first seconds.' },
  { tool: 'add_super_text', group: 'Text & brand', label: 'Super text',
    description: 'Bold on-screen text between two times. Replaces an overlapping overlay of the same role.',
    fields: { start: { widget: 'time', defaultFrom: 'playhead' },
              end: { widget: 'time', defaultFrom: 'playheadPlus3' } } },
  { tool: 'add_lower_third', group: 'Text & brand', label: 'Lower third',
    description: 'Guest name + handle graphic.',
    fields: { start: { widget: 'time', defaultFrom: 'playhead' },
              end: { widget: 'time', label: 'End (blank = default length)' } } },
  { tool: 'generate_hook', group: 'Text & brand', label: 'Suggest hooks',
    description: 'Draft three hook lines from the transcript. Pick one to drop it in as a hook overlay.',
    readOnly: true, keyHint: NO_KEY_HINT_HOOK },
  { tool: 'apply_brand_kit', group: 'Text & brand', label: 'Brand kit',
    description: 'Your handle, hashtags, colours, font and end card, applied as a watermark and an end card.',
    fields: { end_card: { label: 'End-card image (absolute path)' } } },
  { tool: 'apply_template', group: 'Text & brand', label: 'Show template',
    description: 'Lay down a built-in show’s hook, caption style and labels; refine afterwards.',
    fields: { inputs: { label: 'Inputs — one key=value per line (hook=BUY NOW)' } } },
  { tool: 'apply_show_template', group: 'Text & brand', label: 'Saved show template',
    description: 'Apply a show template you saved earlier (brand kit, canvas, captions, music).' },

  // ---- Find & search ----------------------------------------------------
  { tool: 'find_moments', group: 'Find & search', label: 'Find moments',
    description: 'Describe a moment in words and find it in the footage.',
    readOnly: true, runAsJob: true, keyHint: NO_KEY_HINT_VISION },
  { tool: 'search_media', keywords: 'CLIP', group: 'Find & search', label: 'Search footage',
    description: 'Find frames that match a phrase, search what was said, or both.',
    gate: 'visual_search', gateUnless: { field: 'scope', equals: 'spoken' },
    readOnly: true, runAsJob: true },
  { tool: 'find_broll', group: 'Find & search', label: 'Find b-roll',
    description: 'Keyword-search your local b-roll folder (filenames, folders, sidecar tags).',
    readOnly: true,   // a filename/sidecar scan, not ML — not a job, see chroma_key
    fields: { bin: { label: 'B-roll folder (blank = configured default)' } } },
  { tool: 'match_style', group: 'Find & search', label: 'Match a reference',
    description: 'Measure a reference video’s pacing, shot length, tempo and colours.',
    readOnly: true, runAsJob: true, advanced: true },
  { tool: 'audit_aesthetic', group: 'Find & search', label: 'Style audit',
    description: 'Score the edit from 0 to 100 on hook, pacing and captions, with suggested fixes.',
    readOnly: true },

  // ---- Export -----------------------------------------------------------
  { tool: 'apply_export_preset', group: 'Export', label: 'Platform preset',
    description: 'Set the size, frame rate, quality and loudness a platform expects.' },
  { tool: 'set_loudness_target', group: 'Export', label: 'Export loudness',
    description: 'How loud the export is (Reels and TikTok −16 LUFS, YouTube −14).',
    fields: { lufs: { label: 'Target LUFS', nullable: { label: 'Off (skip the loudness pass)' } } } },
]

export type GateResult =
  | { ok: true; checking: boolean }
  | { ok: false; feature: string; fix: string; packagedExcluded: boolean }

// `report === null` means features haven't loaded (or the route failed):
// unknown is not unavailable, so the tool stays runnable with a quiet
// "checking…" badge — a 422 later is better than a greyed-out tool that would
// have worked.
export function gateFor(
  entry: CatalogEntry, report: FeatureReport | null, values?: Record<string, unknown>,
): GateResult {
  if (!entry.gate) return { ok: true, checking: false }
  if (entry.gateUnless && values && values[entry.gateUnless.field] === entry.gateUnless.equals) {
    return { ok: true, checking: false }
  }
  if (report === null) return { ok: true, checking: true }
  const missing = report.unavailable.find((f) => f.key === entry.gate)
  if (!missing) return { ok: true, checking: false }
  return { ok: false, feature: missing.feature, fix: missing.fix ?? '',
           packagedExcluded: !!missing.packaged_app_excluded }
}

export function filterCatalog(entries: readonly CatalogEntry[], query: string): CatalogEntry[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...entries]
  return entries.filter((e) =>
    e.label.toLowerCase().includes(q) || e.tool.includes(q)
    || e.description.toLowerCase().includes(q) || e.group.toLowerCase().includes(q)
    || (e.keywords ?? '').toLowerCase().includes(q))
}

export function groupCatalog(entries: readonly CatalogEntry[]): { group: AiGroup; entries: CatalogEntry[] }[] {
  return GROUP_ORDER
    .map((group) => ({ group, entries: entries.filter((e) => e.group === group) }))
    .filter((g) => g.entries.length > 0)
}

export type ClipRequirement =
  | { ok: true; clipId: string | null }
  | { ok: false; reason: string }

// 'video' checks the TRACK type, not just isMediaClip: types.ts's isMediaClip
// also matches audio-lane clips, and running upscale on a music clip is a 422.
export function clipRequirement(entry: CatalogEntry, edl: EDL | null, selection: string | null): ClipRequirement {
  if (!entry.needsClip) return { ok: true, clipId: null }
  const reason = entry.needsClip === 'video'
    ? 'Select a video clip on the timeline'
    : 'Select a clip with audio on the timeline'
  if (!edl || !selection) return { ok: false, reason }
  for (const track of edl.tracks) {
    const clip = track.clips.find((c) => c.id === selection)
    if (!clip) continue
    if (!isMediaClip(clip)) return { ok: false, reason }
    if (entry.needsClip === 'video' && track.type !== 'video') return { ok: false, reason }
    return { ok: true, clipId: clip.id }
  }
  return { ok: false, reason }
}

// First v1 media clip covering `t` — motion_track's default source clip.
export function videoClipUnder(edl: EDL, t: number): string | null {
  const v1 = edl.tracks.find((tr) => tr.id === 'v1')
  const clip = v1?.clips.find((c) => isMediaClip(c) && c.start <= t && t < clipEnd(c))
  return clip?.id ?? null
}

const round3 = (n: number) => Math.round(n * 1000) / 1000
const clamp01 = (n: number) => Math.max(0, Math.min(1, n))

// The sticker's on-canvas square at its first frame as a normalised bbox — the
// natural "follow this" box, so the form opens with the picture already framed.
function stickerBbox(sk: StickerClip, canvas: Canvas): number[] {
  const g = stickerGeom(sk, sk.start, canvas.w, canvas.h, canvas.w, canvas.h)
  const x = clamp01((g.cx - g.size / 2) / canvas.w)
  const y = clamp01((g.cy - g.size / 2) / canvas.h)
  const w = clamp01(Math.min(g.size / canvas.w, 1 - x))
  const h = clamp01(Math.min(g.size / canvas.h, 1 - y))
  return [x, y, w, h].map(round3)
}

// motion_track is the one tool whose natural gesture is selecting the OVERLAY:
// with a sticker/text clip selected, pre-pick it as the target, the v1 clip
// under its start as the source, and (stickers only) its geometry as the box.
export function motionTrackSeed(
  edl: EDL | null, selection: string | null,
): { target_id?: string; clip_id?: string; bbox?: number[] } {
  if (!edl || !selection) return {}
  for (const track of edl.tracks) {
    if (track.type !== 'text' && track.type !== 'sticker') continue
    const clip = track.clips.find((c) => c.id === selection)
    if (!clip) continue
    const source = videoClipUnder(edl, clip.start)
    return {
      target_id: clip.id,
      ...(source ? { clip_id: source } : {}),
      ...(track.type === 'sticker' && isSticker(clip) ? { bbox: stickerBbox(clip, edl.canvas) } : {}),
    }
  }
  return {}
}
