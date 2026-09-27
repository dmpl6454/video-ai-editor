// The engine's observable state (INSTANT_PREVIEW_SPEC §3, engine.ts): the
// events, the mode, the transport's presented/target frames and the status
// a caller reads. ClientPreviewEngine (engineCore.ts) extends this; split
// out so engineCore stays under the 800-line house limit (review RD3).

import type { EngineEvents, EngineMode, EngineStatus } from './engine'
import type { ProgramMap } from './timeline/programMap'
import type { Support } from './timeline/support'
import { Emitter } from './engineOptions'
import { DelayedFlag } from './engineDraw'

const SPINNER_MS = 80

export abstract class EngineBase {
  protected readonly events = new Emitter<EngineEvents>()
  protected mode: EngineMode = 'client'
  protected reason: string | null = null
  protected pm: ProgramMap | null = null
  protected support: Support | null = null
  protected presented = 0
  protected target = 0
  protected _playing = false
  protected buffering = false
  protected readonly spinner = new DelayedFlag(SPINNER_MS, () => this.emitStatus())

  on<E extends keyof EngineEvents>(event: E, cb: (e: EngineEvents[E]) => void): () => void {
    return this.events.on(event, cb)
  }

  protected emit<E extends keyof EngineEvents>(event: E, payload: EngineEvents[E]): void {
    this.events.emit(event, payload)
  }

  get status(): EngineStatus {
    return {
      mode: this.mode, reason: this.reason, playing: this._playing, buffering: this.buffering,
      spinner: this.spinner.on || this.buffering, presentedK: this.presented, total: this.pm?.total ?? 0,
      ranges: this.support?.ranges ?? [],
    }
  }

  protected emitStatus(): void {
    this.emit('status', this.status)
  }

  get presentedK(): number {
    return this.presented
  }

  /** The frame a paused seek is waiting to show (additive; = presentedK when settled). */
  get targetK(): number {
    return this.target
  }

  get playing(): boolean {
    return this._playing
  }

  get program(): ProgramMap | null {
    return this.pm
  }
}
