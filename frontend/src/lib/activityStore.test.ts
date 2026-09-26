// lib/activityStore (LEFT_RAIL_SPEC §2.8, §6.2): what runs, and what the
// activity chip's polite live region says about it. The region must speak on
// STATE CHANGES and at 25 / 50 / 75 % only — never on a timer tick or an ETA
// update — or it chatters over VoiceOver for minutes of captioning.
import { beforeEach, describe, expect, it } from 'vitest'
import {
  captionsMessage, clockLabel, milestoneOf, recordingMessage, spokenDuration, useActivityStore,
  type CaptionsActivity,
} from './activityStore'

const cc = (patch: Partial<CaptionsActivity> = {}): CaptionsActivity => ({
  progress: null, etaS: null, elapsedS: 0, cancelling: false, cancel: () => {}, ...patch,
})

beforeEach(() => {
  useActivityStore.setState({ recording: null, captions: null, liveMessage: '' })
})

describe('recording', () => {
  it('announces the start and the stop, once each', () => {
    const s = useActivityStore.getState()
    let stopped = 0
    s.setRecording({ startedAt: 1000, stop: () => { stopped++ } })
    expect(useActivityStore.getState().liveMessage).toBe('Recording a voiceover')
    // The chip's Stop is the recorder's own stop.
    useActivityStore.getState().recording!.stop()
    expect(stopped).toBe(1)
    s.setRecording(null)
    expect(useActivityStore.getState()).toMatchObject({ recording: null, liveMessage: 'Recording stopped' })
  })

  it('says nothing for a re-publish of the same take', () => {
    expect(recordingMessage({ startedAt: 1, stop() {} }, { startedAt: 1, stop() {} })).toBeNull()
    expect(recordingMessage(null, null)).toBeNull()
  })
})

describe('captions', () => {
  it('announces start, the 25/50/75 % milestones and done — nothing in between', () => {
    const said: string[] = []
    const unsub = useActivityStore.subscribe((st, prev) => {
      if (st.liveMessage !== prev.liveMessage) said.push(st.liveMessage)
    })
    const set = useActivityStore.getState().setCaptions
    set(cc())
    for (let p = 0.01; p <= 0.99; p += 0.01) {
      // Every poll and every second's ETA re-publishes: none of it may speak
      // except a milestone crossing.
      set(cc({ progress: p, etaS: 100 - p * 100, elapsedS: Math.round(p * 100) }))
    }
    set(null, 'done')
    unsub()
    expect(said).toEqual(['Captions started', 'Captions 25%', 'Captions 50%', 'Captions 75%', 'Captions done'])
  })

  it('announces Stopping once, then Cancelled', () => {
    const set = useActivityStore.getState().setCaptions
    set(cc({ progress: 0.3 }))
    set(cc({ progress: 0.3, cancelling: true }))
    expect(useActivityStore.getState().liveMessage).toBe('Stopping captions')
    set(cc({ progress: 0.31, cancelling: true, elapsedS: 40 }))
    expect(useActivityStore.getState().liveMessage).toBe('Stopping captions')
    set(null, 'cancelled')
    expect(useActivityStore.getState()).toMatchObject({ captions: null, liveMessage: 'Captions cancelled' })
  })

  it('names a failure instead of calling it done', () => {
    expect(captionsMessage(cc(), null, 'failed')).toBe('Captions failed')
    expect(captionsMessage(cc(), null)).toBe('Captions done')
  })

  it('jumps straight to the highest milestone crossed (coarse batched progress)', () => {
    expect(captionsMessage(cc({ progress: 0.1 }), cc({ progress: 0.8 }))).toBe('Captions 75%')
    expect(captionsMessage(cc({ progress: 0.8 }), cc({ progress: 0.9 }))).toBeNull()
  })

  it('milestoneOf', () => {
    expect([null, 0, 0.249, 0.25, 0.5, 0.74, 0.75, 1].map(milestoneOf)).toEqual([0, 0, 0, 25, 50, 50, 75, 75])
  })
})

describe('the chip\'s clocks', () => {
  it('clockLabel: m:ss, h:mm:ss past an hour, never negative', () => {
    expect(clockLabel(0)).toBe('0:00')
    expect(clockLabel(12.9)).toBe('0:12')
    expect(clockLabel(65)).toBe('1:05')
    expect(clockLabel(3725)).toBe('1:02:05')
    expect(clockLabel(-3)).toBe('0:00')
  })
  it('spokenDuration: words for an aria-label', () => {
    expect(spokenDuration(1)).toBe('1 second')
    expect(spokenDuration(31)).toBe('31 seconds')
    expect(spokenDuration(89)).toBe('1 minute')
    expect(spokenDuration(150)).toBe('3 minutes')
  })
})
