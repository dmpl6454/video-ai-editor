import { formatTimecode } from './timecode'
import { laneName } from './timelineLanes'
import { prettyDiskName } from './mediaNames'
import type { Track } from '../types'

// Editor-language labels for dispatch tools and their summaries (QA-101).
//
// History, the chat's tool lines and the prompt run log used to print the
// dispatch vocabulary verbatim: "split_at — Split at 5.00s on v1 (1 clip(s)
// split)", "Added text None 'OVER FC30'", "Transform c_12cab508: rotation=0 …".
// The tool id and clip ids are internal; a user reads "Split", "Text added",
// "Transform: scale 1.2". The raw strings stay available as a hover title for
// bug reports — never as the visible text.

/** Human names for every tool a user can see an op or step for. */
export const TOOL_TITLES: Record<string, string> = {
  init: 'New project', add_clip: 'Add clip', add_caption_track: 'Captions', add_effect: 'Effect',
  add_fade: 'Fade', add_hook_overlay: 'Hook', add_keyframe: 'Keyframe', add_lower_third: 'Lower third',
  add_marker: 'Marker', add_mask: 'Mask', add_music: 'Music', add_sticker: 'Sticker',
  add_super_text: 'Title', add_text: 'Text', add_transition: 'Transition', apply_brand_kit: 'Brand kit',
  apply_export_preset: 'Export preset', apply_hook_stack: 'Hook', apply_lut: 'Colour look',
  apply_show_template: 'Show template', apply_template: 'Template', apply_text_template: 'Text template',
  assign_caption_speakers: 'Speaker colours', audit_aesthetic: 'Style check', auto_caption: 'Auto captions',
  auto_cut_to_beats: 'Cut to the beat', auto_reframe: 'Reframe', bulk_delete: 'Delete clips',
  bulk_duplicate: 'Duplicate clips', chroma_key: 'Green screen', color_grade: 'Colour',
  cut_range: 'Trim', diarize: 'Detect speakers', duplicate_clip: 'Duplicate', export_ass: 'Export subtitles',
  export_srt: 'Export subtitles', export_vtt: 'Export subtitles', find_broll: 'Find B-roll',
  find_moments: 'Find moments', fit_music_to_video: 'Fit music', generate_hook: 'Hook',
  import_srt: 'Import subtitles', instrumental_isolate: 'Music only', make_shorts: 'Shorts',
  match_style: 'Match style', motion_track: 'Motion tracking', move_clip: 'Move', multicam: 'Multicam',
  name_speakers: 'Name speakers', noise_reduce: 'Noise removal', object_erase: 'Erase object',
  paste_clips: 'Paste', record_voiceover: 'Voiceover', redo: 'Redo', remove_background: 'Remove background',
  remove_effect: 'Remove effect', remove_fillers: 'Remove filler words', remove_keyframe: 'Remove keyframe',
  remove_marker: 'Remove marker', remove_mask: 'Remove mask', remove_silences: 'Remove silences',
  remove_transition: 'Remove transition', reorder_clips: 'Reorder', ripple_delete: 'Delete',
  save_show_template: 'Save show template', set_aspect_ratio: 'Aspect ratio', set_canvas: 'Canvas',
  set_clip_fit: 'Fit', set_clip_muted: 'Mute', set_clip_timing: 'Timing', set_clip_transform: 'Transform',
  set_clip_z: 'Layer order', set_duck: 'Ducking', set_loudness_target: 'Loudness', set_pip_framing: 'Framing',
  set_property: 'Edit', set_speed: 'Speed', set_track_locked: 'Lock track', set_track_muted: 'Mute track',
  set_track_solo: 'Solo track', detach_audio: 'Detach audio',
  set_video_fade: 'Fade', set_volume: 'Volume', smooth_slow_motion: 'Smooth slow motion',
  split_at: 'Split', stabilize: 'Stabilize', transcribe: 'Transcribe', translate_captions: 'Translate captions',
  trim_clip: 'Trim', tts_voiceover: 'Voiceover', undo: 'Undo', upscale: 'AI upscale', vocal_isolate: 'Voice only',
  prompt: 'Prompt', verify_render: 'Check the result', download: 'Download', finish_short: 'Finish short',
  repair_media_paths: 'Relink media', repair_chunks: 'Repair preview',
}

/** A title for a tool id: the table, else the id made readable. */
export function toolTitle(tool: string): string {
  const t = TOOL_TITLES[tool]
  if (t) return t
  const s = tool.replace(/_/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Internal ids: clips (c_/t_/s_/k_/m_…) and sessions carry an 8+ hex suffix.
const ID_RE = /\s*(?:→\s*)?\b[a-z]{1,3}_[0-9a-f]{6,}\b/g

/** A dispatch summary with internal ids and Python reprs taken out. */
export function cleanSummary(summary: string): string {
  let s = summary ?? ''
  s = s.replace(ID_RE, '')
  // "clip(s)" → "clip" / "clips" from the count in front of it.
  s = s.replace(/(\d+)\s+([a-z]+)\(s\)/gi, (_m, n: string, w: string) => `${n} ${Number(n) === 1 ? w : `${w}s`}`)
  s = s.replace(/\(s\)/g, 's')
  // A missing role printed as Python None ("Added text None 'X'").
  s = s.replace(/\bNone\s+/g, '')
  // {'brightness': 0.1, 'contrast': 1.2} → brightness 0.1, contrast 1.2
  s = s.replace(/\{([^{}]*)\}/g, (_m, body: string) =>
    body.replace(/'([^']+)':\s*/g, '$1 ').replace(/\s*,\s*/g, ', ').trim())
  // key=value pairs → "key value"
  s = s.replace(/\b([a-z_]+)=(-?[\w.]+)/gi, (_m, k: string, v: string) => `${k.replace(/_/g, ' ')} ${v}`)
  // Lane ids read the way the timeline labels them.
  s = s.replace(/\b(v|a)(\d)\b/g, (_m, l: string, n: string) => `${l.toUpperCase()}${n}`)
  s = s.replace(/\s+([:,)])/g, '$1').replace(/\(\s+/g, '(').replace(/\s{2,}/g, ' ').trim()
  return s.replace(/^[:—-]\s*/, '')
}

// A snake_case word that IS a tool id ("· add_text: replaced …").
const TOOL_ID_RE = /\b[a-z]+(?:_[a-z0-9]+)+\b/g

/** Free prose (a prompt reply, a step summary or error) in editor language:
 *  tool ids named as their titles, then `cleanSummary`. */
export function editorProse(text: string): string {
  return cleanSummary((text ?? '').replace(TOOL_ID_RE, (m) => TOOL_TITLES[m] ?? m))
}

export interface OpLabel { title: string; detail: string; raw: string }

/** What the project knows that turns a summary into editor language
 *  (QA-101): the frame rate (times read as SMPTE timecode, like the rest of
 *  the app), the media library's names (never the sanitised disk name
 *  `narration_en_92b62330.wav`), and the lanes (never a track id `vo`). */
export interface LabelContext {
  fps?: unknown
  /** src (absolute path) → the user's name for it (lib/mediaNames). */
  names?: ReadonlyMap<string, string>
  tracks?: readonly Track[]
}

const MEDIA_FILE_RE = /\b[\w.\-]+\.(?:mp4|mov|m4v|mkv|webm|wav|mp3|m4a|aac|flac|ogg|png|jpe?g|heic|gif|webp)\b/gi
const SPAN_RE = /(\d+(?:\.\d+)?)s?\s*[–-]\s*(\d+(?:\.\d+)?)s?(?=[)\s,]|$)/g
const AT_RE = /(^|\s)(?:@|at)\s+(\d+(?:\.\d+)?)s\b/g
const LANE_RE = /\b(to|on|from|in)\s+(v\d+|a\d+|vo\d*|music\d*|captions|text|stickers|tx_[a-z]+)\b/gi

/** `summary` in editor language for this project: `cleanSummary`, then media
 *  names, lane names and timecode from `ctx`. */
export function editorSummary(summary: string, ctx: LabelContext = {}): string {
  let s = summary ?? ''
  if (ctx.names || ctx.tracks) {
    const byBase = new Map<string, string>()
    for (const [src, name] of ctx.names ?? []) byBase.set(src.split(/[\\/]/).pop() ?? src, name)
    s = s.replace(MEDIA_FILE_RE, (f) => byBase.get(f) ?? prettyDiskName(f))
  }
  if (ctx.tracks) {
    const lanes = new Map(ctx.tracks.map((t) => [t.id.toLowerCase(), laneName(t)]))
    s = s.replace(LANE_RE, (m, prep: string, id: string) => {
      const name = lanes.get(id.toLowerCase())
      return name ? `${prep} ${name}` : m
    })
  }
  s = cleanSummary(s)
  if (ctx.fps) {
    s = s.replace(SPAN_RE, (_m, a: string, b: string) =>
      `${formatTimecode(Number(a), ctx.fps)}–${formatTimecode(Number(b), ctx.fps)}`)
    s = s.replace(AT_RE, (_m, pre: string, t: string) => `${pre}at ${formatTimecode(Number(t), ctx.fps)}`)
  }
  return s
}

/** What a History row shows for an op. `raw` is for the hover title only. */
export function opLabel(op: { tool: string; summary?: string | null }, ctx?: LabelContext): OpLabel {
  const title = toolTitle(op.tool)
  let detail = ctx ? editorSummary(op.summary ?? '', ctx) : cleanSummary(op.summary ?? '')
  // Don't say it twice: "Split — Split at 5.00s" → "Split — at 5.00s", and
  // "Prompt — Prompt: Reframe" → "Prompt — Reframe".
  const low = detail.toLowerCase()
  const t = title.toLowerCase()
  if (low.startsWith(t + ': ')) detail = detail.slice(title.length + 2)
  else if (low.startsWith(t + ' ')) detail = detail.slice(title.length + 1)
  return { title, detail, raw: `${op.tool} — ${op.summary ?? ''}` }
}

const ROLE_LABELS: Record<string, string> = {
  super: 'Title', hook: 'Hook', lower_third: 'Lower third', caption: 'Caption', label: 'Label', watermark: 'Watermark',
}

/** The inspector's name for a text clip's role (null = plain text). */
export function textRoleLabel(role: string | null | undefined): string {
  return (role && ROLE_LABELS[role]) || 'Text'
}
