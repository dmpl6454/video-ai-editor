// SMPTE timecode — the one way the editor writes and reads a timeline instant
// (QA-048).
//
// The clock read "0.03 / 85.03s", the playhead chip "2.0s" for both frame 60
// and frame 61 at 30 fps, the ruler "1.0s", and the Timing fields took decimal
// seconds, so a frame could be neither identified nor typed. Every one of those
// now goes through this module: HH:MM:SS:FF on the project grid
// (`edl.canvas.fps`, lib/frameStep's rule), drop-frame (HH:MM:SS;FF) at the two
// NTSC rates that need it so a 29.97 timeline's hour still reads 01:00:00;00.
//
// Frames are counted exactly like lib/frameStep.stepFrames — `round(t · fps)` —
// so the frame a label names is the frame a step lands on.

import { projectFps } from './frameStep'

/** Integer label rate: 24 for 23.976, 30 for 29.97, 60 for 59.94. */
export function nominalRate(fps: unknown): number {
  return Math.max(1, Math.round(projectFps(fps)))
}

/** Frames dropped from the label count each minute (except every tenth):
 *  2 at 29.97, 4 at 59.94, 0 elsewhere (23.976 is always non-drop). */
export function dropFrames(fps: unknown): number {
  const f = projectFps(fps)
  if (Math.abs(f - 30000 / 1001) < 0.005) return 2
  if (Math.abs(f - 60000 / 1001) < 0.005) return 4
  return 0
}

/** The frame index of `t` seconds on the `fps` grid (never < 0). */
export function frameIndex(t: number, fps: unknown): number {
  const f = projectFps(fps)
  return Math.max(0, Math.round((Number.isFinite(t) ? t : 0) * f))
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** HH:MM:SS:FF (or HH:MM:SS;FF drop-frame) for frame `n`. */
export function framesToTimecode(n: number, fps: unknown): string {
  const nom = nominalRate(fps)
  const drop = dropFrames(fps)
  let frames = Math.max(0, Math.round(n))
  if (drop) {
    // SMPTE 12M: labels ;00 and ;01 (;00–;03 at 59.94) are skipped at the
    // start of every minute except minutes divisible by ten.
    const perTen = Math.round(projectFps(fps) * 600)        // 17982 / 35964
    const perMin = nom * 60 - drop                          // 1798 / 3596
    const tens = Math.floor(frames / perTen)
    const rem = frames % perTen
    frames += 9 * drop * tens + (rem > drop ? drop * Math.floor((rem - drop) / perMin) : 0)
  }
  const ff = frames % nom
  const totalS = Math.floor(frames / nom)
  const ss = totalS % 60
  const mm = Math.floor(totalS / 60) % 60
  const hh = Math.floor(totalS / 3600)
  return `${pad2(hh)}:${pad2(mm)}:${pad2(ss)}${drop ? ';' : ':'}${pad2(ff)}`
}

/** The timecode of `t` seconds at the project rate. */
export function formatTimecode(t: number, fps: unknown): string {
  return framesToTimecode(frameIndex(t, fps), fps)
}

/** Frame index for a SMPTE label, honouring drop-frame numbering. A label that
 *  drop-frame skips (00:01:00;00) resolves to the next real one (…;02). */
function timecodeToFrames(hh: number, mm: number, ss: number, ff: number, fps: unknown): number {
  const nom = nominalRate(fps)
  const drop = dropFrames(fps)
  const totalMin = hh * 60 + mm
  if (drop && ss === 0 && ff < drop && totalMin % 10 !== 0) ff = drop
  const labelled = (hh * 3600 + mm * 60 + ss) * nom + ff
  return drop ? labelled - drop * (totalMin - Math.floor(totalMin / 10)) : labelled
}

/**
 * Parse what a user typed into a timing field. Accepted, all landing ON the
 * frame grid:
 *   `01:02:03:04`, `02:03:04`, `03:04` — SMPTE, right-aligned like every NLE's
 *   timecode field (`3:04` is 3 s 4 f); `;` works as the frame separator.
 *   `12.5` / `12.5s` — seconds.   `1:05.5` — minutes:seconds with a decimal.
 *   `90f` — a frame count.
 * Returns seconds, or null for anything else (the field then reverts).
 */
export function parseTimecode(text: string, fps: unknown): number | null {
  const s = text.trim().toLowerCase()
  if (!s) return null
  const f = projectFps(fps)
  const toGrid = (sec: number) => (Number.isFinite(sec) && sec >= 0 ? frameIndex(sec, f) / f : null)
  let m = /^(\d+)\s*f$/.exec(s)
  if (m) return Number(m[1]) / f
  m = /^(\d+(?:\.\d*)?|\.\d+)\s*s?$/.exec(s)
  if (m) return toGrid(Number(m[1]))
  const parts = s.split(/[:;]/)
  if (parts.length < 2 || parts.length > 4 || parts.some((p) => p === '')) return null
  const last = parts[parts.length - 1]
  if (last.includes('.')) {
    // [H:]M:S.sss — clock time with a decimal, not SMPTE.
    if (!parts.slice(0, -1).every((p) => /^\d+$/.test(p)) || !/^\d+(\.\d*)?$/.test(last)) return null
    const nums = parts.map(Number)
    const sec = nums.reduce((acc, v) => acc * 60 + v, 0)
    return toGrid(sec)
  }
  if (!parts.every((p) => /^\d+$/.test(p))) return null
  const nums = parts.map(Number)
  while (nums.length < 4) nums.unshift(0)
  const [hh, mm, ss, ff] = nums
  if (mm > 59 || ss > 59 || ff >= nominalRate(fps)) return null
  return timecodeToFrames(hh, mm, ss, ff, fps) / f
}

/** One ruler tick: its time and whether it carries a label (major). */
export interface RulerTick { t: number; major: boolean }

// Major-tick spacing, in frames below a second and in timecode seconds above.
const FRAME_STEPS = [1, 2, 5, 10]
const SECOND_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]

/** Major step in FRAMES so labels sit at least `minPx` apart at `zoom` px/s:
 *  whole frames when zoomed in, whole timecode seconds (nominal-rate frame
 *  counts, so 23.976 labels read …:01:00, …:02:00) when zoomed out. */
export function rulerStepFrames(zoom: number, fps: unknown, minPx = 80): number {
  const f = projectFps(fps)
  const nom = nominalRate(fps)
  const pxPerFrame = zoom / f
  for (const n of FRAME_STEPS) if (n < nom / 2 && n * pxPerFrame >= minPx) return n
  for (const s of SECOND_STEPS) if (s * nom * pxPerFrame >= minPx) return s * nom
  return SECOND_STEPS[SECOND_STEPS.length - 1] * nom
}

/**
 * Ticks whose x (labelWidth + t·zoom) lies in [viewL, viewR], up to `maxT`.
 * Majors are every `rulerStepFrames` frames; when a frame is at least 5 px
 * wide every frame gets an unlabelled minor tick (frame ticks when zoomed).
 * Tick times are frame n / fps — computed per tick, never accumulated.
 */
export function rulerTicks(
  viewL: number, viewR: number, labelWidth: number, zoom: number, fps: unknown, maxT: number,
): RulerTick[] {
  if (!(zoom > 0)) return []
  const f = projectFps(fps)
  const major = rulerStepFrames(zoom, f)
  const pxPerFrame = zoom / f
  const minor = pxPerFrame >= 5 ? 1 : major
  const firstFrame = Math.max(0, Math.floor(((viewL - labelWidth) / zoom) * f / minor) * minor)
  const out: RulerTick[] = []
  for (let n = firstFrame; ; n += minor) {
    const t = n / f
    if (t > maxT + 1e-9) break
    const x = labelWidth + t * zoom
    if (x > viewR) break
    if (x >= viewL - 1) out.push({ t, major: n % major === 0 })
  }
  return out
}

/** The label boxes (content x, width) of the ruler's MAJOR ticks that the
 *  playhead chip [left, right] would cover — the overlay blanks them before
 *  drawing the chip, so a label never prints through or beside it (wave-B
 *  review: at 00:00:00:00 the chip and the ruler label printed on top of each
 *  other). Labels are drawn at x + 3 (Timeline's ruler). */
export function rulerLabelsUnder(
  ticks: readonly RulerTick[], labelWidth: number, zoom: number, fps: unknown,
  measure: (s: string) => number, left: number, right: number,
): { x: number; w: number }[] {
  const out: { x: number; w: number }[] = []
  for (const tk of ticks) {
    if (!tk.major) continue
    const x = labelWidth + tk.t * zoom + 3
    const w = measure(formatTimecode(tk.t, fps))
    if (x + w >= left && x <= right) out.push({ x, w })
  }
  return out
}
