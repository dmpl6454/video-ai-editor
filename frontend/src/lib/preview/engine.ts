// The instant-preview ENGINE facade (INSTANT_PREVIEW_SPEC §3, §4.1, §4.3,
// §9.1): one object, no React, that turns an EDL into a picture on a WebGL2
// canvas driven by laneA (MSE over all-intra proxy frames) and a sound driven
// by an AudioSink, both on the presented-frame clock.
//
// ============================================================== PUBLIC API
// The section below is the STABLE contract. The audio lane implements
// `AudioSink`; the integration lane (store.ts / Preview.tsx) drives
// `PreviewEngine`. Change it only additively.

import type { EdlLike } from './timeline/framePlan'
import type { SourceInfo } from './timeline/frameMap'
import type { AudioPlacement, ProgramDiff } from './timeline/programMap'
import type { ModeRange } from './timeline/support'
import type { Rational } from './timeline/timebase'

export type { ProgramDiff } from './timeline/programMap'

/** Where the picture comes from: this engine, or today's server preview.mp4
 *  (engine-level fallback, §7). */
export type EngineMode = 'client' | 'server'

/** A source's proxy, as `GET /api/sessions/{sid}/proxy` names it. */
export interface EngineProxyRef {
  /** 24-hex proxy key: frames come from `${proxyBaseUrl}/${key}/…`. */
  key: string
  /** The route's state; `failed`: the source's paused frames come from the
   *  degraded <video> tier (`EngineSource.media`), its ranges are BAKED
   *  while playing (§7). */
  state?: 'ready' | 'partial' | 'pending' | 'failed'
}

/** What the engine needs to know about one `src` of the EDL. */
export interface EngineSource {
  /** Frame-selection facts of the MASTER (`render/frame_map.py` SourceInfo:
   *  rate, stream time base, decoded frame count, start ticks, DISPLAY w/h).
   *  Geometry (fit/cover) is computed from `info.w`/`info.h`. */
  info: SourceInfo
  /** Null while no proxy exists yet: the source's frames are PENDING. */
  proxy: EngineProxyRef | null
  /** URL of the normalised MASTER (`/api/sessions/{sid}/files/uploads/…`):
   *  with the proxy failed, the degraded <video> tier shows its paused
   *  frames from it (§7). Absent: those frames hold the last good frame. */
  media?: string
}

/** `src` → source facts; null for an unknown source (its frames PENDING). */
export type EngineSourceLookup = (src: string) => EngineSource | null

export interface EngineStatus {
  mode: EngineMode
  /** Why the engine is in server mode (`no-mse`, `no-webgl2`, `rate`,
   *  `sourcebuffer-errors`, `webgl-lost`), or a degraded-state note. */
  reason: string | null
  playing: boolean
  /** laneA ran out of frames while playing (PENDING spans, §3.5). */
  buffering: boolean
  /** Show the corner spinner: the frame at the playhead has been waiting
   *  ≥ 80 ms (last good frame held), or playback is buffering (§7). */
  spinner: boolean
  presentedK: number
  total: number
  /** Fidelity classes of the current program (support.classify, §7). */
  ranges: readonly ModeRange[]
}

export interface EngineFrameEvent {
  /** The output frame now on the canvas. */
  k: number
  /** k / R, seconds. */
  t: number
  playing: boolean
  /** `black` for a timeline gap. */
  drawn: 'picture' | 'black'
}

/** A pause the engine did not issue: WebKit paused the muted laneA <video>
 *  on its own (page hidden, window occluded or minimised), or dropped the
 *  WebGL context mid-play ('context'). The engine has already stopped the
 *  sound at the same k and the clock. */
export interface ExternalPauseEvent {
  k: number
  cause: 'hidden' | 'element' | 'context'
  /** The user's last intent was to play: the engine resumes picture and
   *  sound together from a fresh anchor when the page is visible again
   *  (or, for 'context', when the WebGL context is restored). */
  willResume: boolean
}

export interface BufferingEvent { buffering: boolean; k: number }

export interface EngineEvents {
  status: EngineStatus
  frame: EngineFrameEvent
  'pause-external': ExternalPauseEvent
  buffering: BufferingEvent
}

/** The presented-frame clock (§3.5): overlays draw at `now()`. */
export interface EngineClock {
  /** Seconds of the frame on screen (presentedK / R); while playing,
   *  interpolated between frames for rAF-driven overlay layers. */
  now(): number
}

export interface PreviewEngine {
  /** Mounts the engine's canvas (and its hidden laneA <video>) in `host`,
   *  sized to the host box × DPR (short edge ≤ 1080). */
  attach(host: HTMLElement): void
  /** A committed EDL (dispatch `include=edl`). Rebuilds the program map
   *  (per-clip memo), classifies it, schedules appends for the dirty frames
   *  and rebinds params; returns what changed. Never throws. */
  setTimeline(edl: EdlLike, renderHash: string, sourceLookup: EngineSourceLookup): ProgramDiff
  /** Synchronous and safe INSIDE a user gesture handler: starts laneA and
   *  the AudioSink (which resumes its AudioContext) in the same task. */
  play(): void
  pause(): void
  /** Paused-seek semantics: shows output frame k ((k + 0.5)/R, R7). */
  seek(k: number): void
  /** The output frame on screen (rVFC `mediaTime`, R8). */
  readonly presentedK: number
  readonly clock: EngineClock
  readonly playing: boolean
  readonly status: EngineStatus
  /** Subscribe; returns the unsubscribe function. */
  on<E extends keyof EngineEvents>(event: E, cb: (e: EngineEvents[E]) => void): () => void
  destroy(): void
}

/** Program facts handed to the sink with every new timeline. */
export interface AudioProgramInfo {
  R: Rational
  /** Output frames of the program. */
  totalFrames: number
  renderHash: string
  lookup: EngineSourceLookup
}

/**
 * The sound side (implemented by the audio lane, §3.6). The engine drives it
 * in lockstep with the picture: every start/stop happens at a presented k.
 * Output sample S(k) = samples_for_frames(k, R) at 48 kHz (R9).
 */
export interface AudioSink {
  /** A new program (after every setTimeline): the v1 placements
   *  (programMap.audioPlacements) and the EDL for params and other lanes. */
  prepare(edl: EdlLike, placements: readonly AudioPlacement[], program: AudioProgramInfo): void
  /** Make output sample `fromSample` heard at AudioContext time `atCtxTime`
   *  (seconds). Called SYNCHRONOUSLY inside play() — i.e. inside the user's
   *  gesture — so resume the AudioContext here. Called again while already
   *  running, it RE-ANCHORS (5 ms equal-power crossfade, §3.5). */
  start(atCtxTime: number, fromSample: number): void
  /** Silence in lockstep with the picture, ramping over `rampMs`. */
  stop(rampMs: number): void
  /** A structural edit while playing: re-schedule the dirty frames from
   *  output sample `fromSample` (≈ presentedK + 6 frames, §3.6). */
  reschedule(dirty: ProgramDiff, fromSample: number): void
  /** A parameter-only edit (gain, gain_env, fades, mute, channel mode). */
  setParams(clipId: string): void
  /** The AudioContext time (s) at which sound is HEARD for the display time
   *  `perfMs` (a performance.now() value), from getOutputTimestamp (§3.5);
   *  null when there is no context. When it was null at play() the engine
   *  calls start(0, sample) and anchors properly (a second start) at the
   *  first presented frame. */
  ctxTimeAt(perfMs: number): number | null
  /** Resolves true once the sound can really be heard: the AudioContext is
   *  running AND its clock advances. WebKit reports a context resumed after
   *  the page was hidden as 'running' up to ~1.8 s before it renders a
   *  sample (measured in WKWebView), so a resume after an external pause
   *  waits for this before the picture moves. False after `timeoutMs`.
   *  Optional: a sink without it is ready at once. */
  whenRunning?(timeoutMs: number): Promise<boolean>
  /** Output FRAME ranges where the master limiter may work — APPROX, the
   *  browser's limiter is not the export's alimiter (gate RX,
   *  audio/limiting.ts). Optional: a sink without it has none. */
  limitingFrames?(): ReadonlyArray<readonly [number, number]>
  /** Set by the engine: called when limitingFrames() changes by itself (a
   *  source's recorded peaks landed after prepare). */
  onLimitingChange?: (() => void) | null
  dispose?(): void
}

// ========================================================== IMPLEMENTATION
// engineCore.ts (the engine), engineFeed.ts (program → laneA), media/,
// render/, clock/. Re-exported here so `./engine` stays the one import.
export {
  ClientPreviewEngine, NullAudioSink, createPreviewEngine, engineUnsupportedReason, type EngineOptions,
} from './engineCore'
