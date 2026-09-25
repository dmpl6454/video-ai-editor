// The top bar's one "Ratio" menu: the four aspect ratios and the platform
// presets, with the canvas' current choice checked (QA-012).
//
// They used to be nine loose buttons (~260 px) in a horizontally scrolling
// strip with no visible scrollbar: at 1440 px wide TikTok, IG 1:1, IG 4:5, Help
// and Shortcuts were off the edge; at 1024 so were Text and Captions. None of
// them showed which one was in effect — no class, no aria-pressed — so after
// clicking Reels nothing on screen said the canvas was now 1080×1920.

import { PLATFORM_SPECS } from './exportOptions'

export interface CanvasLike {
  w: number
  h: number
  fps: number
  bitrate_kbps?: number | null
  loudness_lufs?: number | null
}

export const ASPECTS = ['9:16', '16:9', '1:1', '4:5'] as const
export type Aspect = typeof ASPECTS[number]

export interface PlatformPreset {
  label: string
  title: string
  w: number
  h: number
  bitrateKbps: number
  lufs: number
}

// The menu's platform rows ARE lib/exportOptions' PLATFORM_SPECS (what
// apply_export_preset applies), so the check mark and the dispatch can never
// describe two different specs. No fps: the preset keeps the project's rate.
export const PLATFORM_PRESETS: readonly PlatformPreset[] = PLATFORM_SPECS.map((p) => ({
  label: p.label,
  title: `${p.label} — ${p.w}×${p.h}, ${p.bitrateKbps / 1000} Mbps, ${p.lufs} LUFS (keeps the project frame rate)`,
  w: p.w, h: p.h, bitrateKbps: p.bitrateKbps, lufs: p.lufs,
}))

const RATIO: Record<Aspect, number> = { '9:16': 9 / 16, '16:9': 16 / 9, '1:1': 1, '4:5': 4 / 5 }

/** The named aspect the canvas is in, or null for a custom size.
 *  Tolerant to even-pixel rounding (1080×1350 vs 1080×1352). */
export function activeAspect(c: CanvasLike | null | undefined): Aspect | null {
  if (!c || !c.w || !c.h) return null
  const r = c.w / c.h
  return ASPECTS.find((a) => Math.abs(RATIO[a] - r) / RATIO[a] < 0.01) ?? null
}

/** A preset is in effect when the canvas carries its WHOLE spec: size, bitrate
 *  target and loudness target — what apply_export_preset writes. Frame rate is
 *  not part of it (the preset keeps the project's rate, QA-009), and a canvas
 *  that merely has the size (an aspect switch) carries no bitrate target, so no
 *  preset is checked for it. Reels and TikTok are one spec: both read checked. */
export function presetActive(p: PlatformPreset, c: CanvasLike | null | undefined): boolean {
  if (!c || c.w !== p.w || c.h !== p.h) return false
  if (c.bitrate_kbps == null || c.loudness_lufs == null) return false
  return c.bitrate_kbps === p.bitrateKbps && Math.abs(c.loudness_lufs - p.lufs) < 0.05
}

/** The trigger's label: the aspect in effect, else the raw size. */
export function ratioLabel(c: CanvasLike | null | undefined): string {
  if (!c) return 'Ratio'
  return activeAspect(c) ?? `${c.w}×${c.h}`
}
