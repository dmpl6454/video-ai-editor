// Final sweep 2: "Add music…" with Media's "Add imports to the timeline"
// unticked silently put the file in Media only (and a retry imported another
// copy). The button says "Add music": it always lands on the Music lane.
import { describe, expect, it } from 'vitest'
import { addMusicFile } from './musicImport'
import type { ImportOptions } from '../store'

describe('addMusicFile (Audio panel > Add music…)', () => {
  it('asks for the Music lane explicitly, whatever the Media checkbox says', async () => {
    const calls: Array<ImportOptions | undefined> = []
    const file = new File([new Uint8Array(4)], 'bed.wav', { type: 'audio/wav' })
    await addMusicFile(file, async (_f, opts) => { calls.push(opts) })
    expect(calls).toEqual([{ addToTimeline: true }])
  })
})
