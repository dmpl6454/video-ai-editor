// Export settings the toolbar offers — the single frontend source for them.
//
// QA-025: the Export popover's "1080p" was sent as the output HEIGHT and the
// backend applied it literally, so 1080p on a 9:16 project exported 608x1080.
// A named resolution now means the SHORT side for the canvas orientation (the
// backend's `compositor.export_dimensions`, mirrored by `exportDimensions`
// below), and every option is labelled with the size the file will measure.
//
// QA-027: the top-bar Reels/Shorts/TikTok buttons dispatched `set_canvas` —
// canvas only, so Shorts kept the -16 LUFS default against its own -14 spec and
// no preset bitrate ever reached an export. They now dispatch
// `apply_export_preset`, whose bitrate/loudness the export encode honours, and
// the Export popover's Quality defaults to that platform target.

export interface CanvasLike {
  w: number
  h: number
  fps?: number | null
  bitrate_kbps?: number | null
  loudness_lufs?: number | null
}

/** (w, h) an export renders at for a named resolution. `shortSide` 0/undefined
 *  = the canvas itself. Same arithmetic as the backend's
 *  `compositor.export_dimensions` (floor-even height, width rounded-even). */
export function exportDimensions(canvasW: number, canvasH: number, shortSide?: number | null): [number, number] {
  let h: number
  if (!shortSide) h = Math.trunc(canvasH)
  else if (canvasH > canvasW) h = Math.round((shortSide * canvasH) / Math.max(1, canvasW))
  else h = Math.trunc(shortSide)
  h = Math.max(2, Math.floor(h / 2) * 2)
  const w = Math.round((canvasW * (h / canvasH)) / 2) * 2
  return [w, h]
}

export const NAMED_RESOLUTIONS: readonly { short: number; name: string }[] = [
  { short: 2160, name: '2160p (4K)' },
  { short: 1440, name: '1440p (2K)' },
  { short: 1080, name: '1080p' },
  { short: 720, name: '720p' },
  { short: 480, name: '480p' },
]

/** <select> options: value 0 is "Source" (the canvas). Each label carries the
 *  real WxH, so a vertical project reads "1080p (1080×1920)". */
export function resolutionOptions(canvas: CanvasLike | null | undefined): { value: number; label: string }[] {
  if (!canvas?.w || !canvas?.h) return NAMED_RESOLUTIONS.map((r) => ({ value: r.short, label: r.name }))
  const out = [{ value: 0, label: `Source (${canvas.w}×${canvas.h})` }]
  for (const r of NAMED_RESOLUTIONS) {
    const [w, h] = exportDimensions(canvas.w, canvas.h, r.short)
    out.push({ value: r.short, label: `${r.name} (${w}×${h})` })
  }
  return out
}

export interface PlatformSpec {
  label: string
  /** `apply_export_preset` name — dispatch._EXPORT_PRESETS. */
  preset: string
  w: number
  h: number
  bitrateKbps: number
  lufs: number
}

/** Mirrors the rows of dispatch._EXPORT_PRESETS the toolbar shows. */
export const PLATFORM_SPECS: readonly PlatformSpec[] = [
  { label: 'Reels', preset: 'reels', w: 1080, h: 1920, bitrateKbps: 8000, lufs: -16 },
  { label: 'Shorts', preset: 'shorts', w: 1080, h: 1920, bitrateKbps: 8000, lufs: -14 },
  { label: 'TikTok', preset: 'tiktok', w: 1080, h: 1920, bitrateKbps: 8000, lufs: -16 },
  { label: 'IG 1:1', preset: 'ig_feed_1x1', w: 1080, h: 1080, bitrateKbps: 6000, lufs: -16 },
  { label: 'IG 4:5', preset: 'ig_feed_4x5', w: 1080, h: 1350, bitrateKbps: 6000, lufs: -16 },
  // QA-100: the backend always had youtube_16x9; nothing in the UI reached it.
  { label: 'YouTube', preset: 'youtube_16x9', w: 1920, h: 1080, bitrateKbps: 12000, lufs: -14 },
]

export function presetTitle(p: PlatformSpec): string {
  return `${p.label} — ${p.w}×${p.h}, ${p.bitrateKbps / 1000} Mbps, ${p.lufs} LUFS`
}

/** What a platform button dispatches: the WHOLE spec, not just the canvas. */
export function platformPresetCommand(p: PlatformSpec): { tool: 'apply_export_preset'; args: { name: string } } {
  return { tool: 'apply_export_preset', args: { name: p.preset } }
}

/** The preset whose full spec the canvas currently carries, if any. Reels and
 *  TikTok share a spec, so the first match wins — they are the same delivery. */
export function activePlatformPreset(canvas: CanvasLike | null | undefined): PlatformSpec | null {
  if (!canvas) return null
  return PLATFORM_SPECS.find((p) => p.w === canvas.w && p.h === canvas.h
    && p.bitrateKbps === canvas.bitrate_kbps && p.lufs === canvas.loudness_lufs) ?? null
}

/** Quality choice in the popover: 'platform' = the preset's bitrate target;
 *  a number = x264-style crf (18 High / 23 Medium / 28 Small). */
export type QualityChoice = 'platform' | number

export function qualityOptions(canvas: CanvasLike | null | undefined): { value: string; label: string }[] {
  const base = [
    { value: '18', label: 'High' },
    { value: '23', label: 'Medium' },
    { value: '28', label: 'Small file' },
  ]
  const kbps = canvas?.bitrate_kbps
  return kbps ? [{ value: 'platform', label: `Platform target (${kbps / 1000} Mbps)` }, ...base] : base
}

/** The Quality the popover starts on: the platform target when the project
 *  carries one, High otherwise. */
export function defaultQuality(canvas: CanvasLike | null | undefined): QualityChoice {
  return canvas?.bitrate_kbps ? 'platform' : 18
}

/** POST /export body for the popover's choices. An explicit crf choice sends
 *  `bitrate_kbps: 0` when the project has a platform target, so the user's
 *  pick is honoured instead of being overridden by the preset. */
export function exportBody(
  canvas: CanvasLike | null | undefined,
  shortSide: number,
  quality: QualityChoice,
  fps?: number | null,
): { height?: number; crf?: number; bitrate_kbps?: number; fps?: number } {
  const body: { height?: number; crf?: number; bitrate_kbps?: number; fps?: number } = {}
  if (shortSide) body.height = shortSide
  // QA-009: a frame rate only when it differs from the project's — absent,
  // the backend renders at edl.canvas.fps (exact, e.g. 30000/1001).
  if (fps && Number.isFinite(fps) && !sameRate(fps, canvas?.fps)) body.fps = fps
  if (quality === 'platform' && canvas?.bitrate_kbps) return body
  body.crf = typeof quality === 'number' ? quality : 18
  if (canvas?.bitrate_kbps) body.bitrate_kbps = 0
  return body
}

/** The command a platform-preset menu item dispatches, by its label. A known
 *  platform applies its WHOLE spec (`apply_export_preset`: canvas + bitrate +
 *  loudness, overlays re-placed); an unknown one falls back to the canvas-only
 *  `set_canvas` it always sent. */
export function platformMenuCommand(item: { label: string; w: number; h: number; fps?: number }):
  { tool: string; args: Record<string, unknown> } {
  const spec = PLATFORM_SPECS.find((p) => p.label === item.label)
  if (spec) return platformPresetCommand(spec)
  const args: Record<string, unknown> = { w: item.w, h: item.h }
  if (item.fps) args.fps = item.fps
  return { tool: 'set_canvas', args }
}

// ---- frame rate (QA-009) ----------------------------------------------------

/** Delivery rates offered in the Export dialog. NTSC rates are exact
 *  rationals; the backend turns any float into an ffmpeg rate via
 *  edl.timebase, so 30000/1001 and 29.97 name the same rate. */
export const FRAME_RATES: readonly { fps: number; label: string }[] = [
  { fps: 24000 / 1001, label: '23.976' },
  { fps: 24, label: '24' },
  { fps: 25, label: '25' },
  { fps: 30000 / 1001, label: '29.97' },
  { fps: 30, label: '30' },
  { fps: 50, label: '50' },
  { fps: 60000 / 1001, label: '59.94' },
  { fps: 60, label: '60' },
]

/** Two rates are the same delivery rate within a thousandth of a frame. */
export function sameRate(a: number | null | undefined, b: number | null | undefined): boolean {
  if (!a || !b) return false
  return Math.abs(a - b) < 1e-3
}

/** <select> options: value '' = the project rate (sends nothing). */
export function frameRateOptions(canvas: CanvasLike | null | undefined): { value: string; label: string }[] {
  const own = canvas?.fps ? FRAME_RATES.find((r) => sameRate(r.fps, canvas.fps))?.label
    ?? String(Number(canvas.fps.toFixed(3))) : null
  return [
    { value: '', label: own ? `Project (${own} fps)` : 'Project rate' },
    ...FRAME_RATES.filter((r) => !sameRate(r.fps, canvas?.fps)).map((r) => ({ value: String(r.fps), label: `${r.label} fps` })),
  ]
}

// ---- loudness (QA-100) ------------------------------------------------------

/** The loudness targets the dialog offers; null = normalisation off. */
export const LOUDNESS_TARGETS: readonly { lufs: number | null; label: string }[] = [
  { lufs: -14, label: '−14 LUFS · YouTube, Spotify' },
  { lufs: -16, label: '−16 LUFS · Reels, TikTok' },
  { lufs: -23, label: '−23 LUFS · Broadcast (EBU R128)' },
  { lufs: null, label: 'Off · keep the mix as it is' },
]

// ---- size estimate (QA-100) -------------------------------------------------

/** The AAC stream every export carries (compositor `_AAC_OUT`). */
export const AUDIO_KBPS = 192
export const AUDIO_DESCRIPTION = 'AAC · stereo · 48 kHz · 192 kbps'

// ---- format (QA-100: audio-only export) -------------------------------------

/** What POST /export writes: a video file, or the timeline's sound alone
 *  (compositor.AUDIO_CONTAINERS) — mastered to the same loudness target and
 *  −1 dBTP ceiling, with no picture rendered at all. */
export type ExportContainer = 'mp4' | 'mov' | 'm4a' | 'wav'

export const EXPORT_FORMATS: readonly { value: ExportContainer; label: string; title: string }[] = [
  { value: 'mp4', label: 'MP4', title: 'Video — H.264 + AAC in MP4, plays everywhere' },
  { value: 'mov', label: 'MOV', title: 'Video — H.264 + AAC in QuickTime MOV' },
  { value: 'm4a', label: 'Audio M4A', title: 'The sound only — AAC in M4A (podcasts, voice-overs)' },
  { value: 'wav', label: 'Audio WAV', title: 'The sound only — uncompressed 24-bit WAV (hand-off to a mixer)' },
]

export function isAudioOnly(c: ExportContainer): boolean {
  return c === 'm4a' || c === 'wav'
}

/** 24-bit stereo PCM at 48 kHz (compositor `_AUDIO_EXPORT_ARGS['wav']`). */
export const WAV_KBPS = (48000 * 2 * 24) / 1000

/** The audio row of the dialog for a format. */
export function audioDescription(c: ExportContainer): string {
  return c === 'wav' ? 'WAV · PCM 24-bit · stereo · 48 kHz' : AUDIO_DESCRIPTION
}

/** Estimated size of an audio-only export: its audio stream alone. */
export function estimateAudioOnlyBytes(c: ExportContainer, seconds: number): number {
  const kbps = c === 'wav' ? WAV_KBPS : AUDIO_KBPS
  return Math.round((kbps * 1000 / 8) * Math.max(0, seconds))
}

/** Bits per pixel per frame the Mac's quality-mode export lands near.
 *  Measured with h264_videotoolbox at the -q:v each crf maps to
 *  (compositor._crf_to_videotoolbox_qv: 18→90, 23→78, 28→65) on the bench
 *  footage at 720×1280 and 2276×1280, 25 fps: 0.36-0.37 / 0.19-0.20 /
 *  0.095-0.097 bpp. An ESTIMATE, labelled "About"; the platform-target branch
 *  is exact up to the encoder's tolerance. */
const CRF_BPP: Record<number, number> = { 18: 0.36, 23: 0.195, 28: 0.096 }

/** Video kbps the export will average: the (pixel-scaled) platform target, or
 *  the quality-mode estimate at the chosen size and rate. */
export function estimateVideoKbps(
  canvas: CanvasLike | null | undefined, shortSide: number, quality: QualityChoice, fps?: number | null,
): number {
  const w0 = canvas?.w ?? 1920
  const h0 = canvas?.h ?? 1080
  const [w, h] = exportDimensions(w0, h0, shortSide || null)
  if (quality === 'platform' && canvas?.bitrate_kbps) {
    // Same pixel scaling as compositor.render_export's target.
    return Math.max(1, Math.round(canvas.bitrate_kbps * Math.min(1, (w * h) / Math.max(1, w0 * h0))))
  }
  const rate = fps || canvas?.fps || 30
  const bpp = CRF_BPP[typeof quality === 'number' ? quality : 18] ?? CRF_BPP[18]
  return Math.round((w * h * rate * bpp) / 1000)
}

/** Estimated file size in bytes for `seconds` of timeline. */
export function estimateBytes(videoKbps: number, seconds: number): number {
  return Math.round(((videoKbps + AUDIO_KBPS) * 1000 / 8) * Math.max(0, seconds))
}

/** A download name from the project name: no path characters, the right
 *  extension, never empty. */
export function exportFileName(name: string | null | undefined, container: ExportContainer): string {
  // Path separators, the characters a filesystem refuses, and control characters.
  const base = [...(name ?? '').replace(/\.(mp4|mov|m4a|wav)$/i, '')]
    .map((ch) => (ch.charCodeAt(0) < 32 || '\\/:*?"<>|'.includes(ch) ? ' ' : ch)).join('')
    .replace(/\s+/g, ' ').trim().slice(0, 120) || 'export'
  return `${base}.${container}`
}
