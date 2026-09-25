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
): { height?: number; crf?: number; bitrate_kbps?: number } {
  const body: { height?: number; crf?: number; bitrate_kbps?: number } = {}
  if (shortSide) body.height = shortSide
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
