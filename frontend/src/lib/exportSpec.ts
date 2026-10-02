// The Export dialog's named choices (design §5, brief §8): resolutions,
// bit-rate tiers and codecs, and what each maps to on POST /export. Pure.
import type { CanvasLike } from './exportOptions'
import { exportDimensions } from './exportOptions'

export type ExportRes = '480P' | '720P' | '1080P' | '2K' | '4K' | '8K'
/** The short side each name means (lib/exportOptions.exportDimensions
 *  derives the long side from the canvas); 2K and 4K follow the DCI/UHD
 *  short sides used by the engine's NAMED_RESOLUTIONS. */
export const EXPORT_RES: readonly { id: ExportRes; short: number }[] = [
  { id: '480P', short: 480 }, { id: '720P', short: 720 }, { id: '1080P', short: 1080 }, { id: '2K', short: 1440 }, { id: '4K', short: 2160 }, { id: '8K', short: 4320 },
]

export type Bitrate = 'Lower' | 'Recommended' | 'Higher' | 'Custom'
/** A tier is a crf (the encoder ladder honours it on every encoder); Custom
 *  is an average-bitrate target in kbps. */
export const BITRATES: readonly { id: Bitrate; label: string; crf: number }[] = [
  { id: 'Lower', label: 'Lower', crf: 28 }, { id: 'Recommended', label: 'Recommended', crf: 23 }, { id: 'Higher', label: 'Higher', crf: 18 }, { id: 'Custom', label: 'Custom', crf: 18 },
]

export type Codec = 'H.264' | 'HEVC' | 'HEVC (Alpha)' | 'Apple ProRes 422' | 'Apple ProRes 422 LT' | 'Apple ProRes 422 HQ'
export const CODECS: readonly { id: Codec; available: boolean; why?: string }[] = [
  { id: 'H.264', available: true },
  { id: 'HEVC', available: false, why: 'HEVC is not available: the render pipeline encodes H.264 (VideoToolbox / NVENC / QSV / AMF / libx264).' },
  { id: 'HEVC (Alpha)', available: false, why: 'HEVC (Alpha) is not available: the render pipeline encodes H.264 without an alpha channel.' },
  { id: 'Apple ProRes 422', available: false, why: 'ProRes is not available: the render pipeline encodes H.264.' },
  { id: 'Apple ProRes 422 LT', available: false, why: 'ProRes is not available: the render pipeline encodes H.264.' },
  { id: 'Apple ProRes 422 HQ', available: false, why: 'ProRes is not available: the render pipeline encodes H.264.' },
]

export function exportSpec(canvas: CanvasLike, res: ExportRes, bitrate: Bitrate, customKbps: number): { shortSide: number; crf: number; bitrateKbps: number | null } {
  const short = EXPORT_RES.find((r) => r.id === res)?.short ?? 1080
  // The project's own size when the name matches it (no scaling).
  const shortSide = short === Math.min(canvas.w, canvas.h) ? 0 : short
  const tier = BITRATES.find((b) => b.id === bitrate) ?? BITRATES[2]
  const custom = bitrate === 'Custom' && Number.isFinite(customKbps) && customKbps >= 100 ? Math.round(customKbps) : null
  return { shortSide, crf: tier.crf, bitrateKbps: custom }
}

export function resolutionLabel(r: { id: ExportRes; short: number }, canvas: CanvasLike): string {
  const [w, h] = exportDimensions(canvas.w, canvas.h, r.short)
  return `${r.id} · ${w} × ${h}`
}
