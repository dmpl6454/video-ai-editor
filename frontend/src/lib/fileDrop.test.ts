// QA-006: one OS file drop on the Media panel's dropzone imported the file
// TWICE — the dropzone's React onDrop uploaded it, and the native event then
// bubbled to FileDropOverlay's window listener, which uploaded it again (two
// POST /upload at the same timestamp, two identical clips on V1).
//
// This replays that event path: the dropzone's handlers run first (as React's
// root listener does), then the SAME event reaches the window listener. Every
// drop must import exactly once, wherever it lands.
import { describe, expect, it, vi } from 'vitest'
import { dropzoneHandlers, importFiles, installWindowFileDrop, isFileDrag } from './fileDrop'

const file = (name: string, type = '') => ({ name, type }) as unknown as File

function fileDropEvent(type: string, files: File[]) {
  const e = new Event(type, { cancelable: true }) as Event & { dataTransfer: unknown }
  e.dataTransfer = { types: ['Files'], files, dropEffect: 'none' }
  return e
}

function harness() {
  const win = new EventTarget()
  const upload = vi.fn(async () => {})
  const uploadAudio = vi.fn(async () => {})
  const active: boolean[] = []
  const stop = installWindowFileDrop(win, {
    setActive: (a) => active.push(a),
    importFiles: (files) => { void importFiles(files, { upload, uploadAudio }) },
  })
  return { win, upload, uploadAudio, active, stop }
}

describe('a file dropped on the Media dropzone', () => {
  it('is imported exactly once', async () => {
    const { win, upload } = harness()
    const over: boolean[] = []
    const zone = dropzoneHandlers((o) => over.push(o))
    const clip = file('clipB.mp4', 'video/mp4')

    const enter = fileDropEvent('dragenter', [clip])
    win.dispatchEvent(enter)
    zone.onDragOver(fileDropEvent('dragover', [clip]))
    const drop = fileDropEvent('drop', [clip])
    zone.onDrop()                 // React's handler on the dropzone…
    win.dispatchEvent(drop)       // …then the native event bubbles to window
    await Promise.resolve()

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload).toHaveBeenCalledWith(clip)
    expect(drop.defaultPrevented).toBe(true)   // the browser never navigates to the file
    expect(over).toEqual([true, false])        // the highlight still tracks the drag
  })

  it('is imported once when dropped anywhere else too', async () => {
    const { win, upload } = harness()
    win.dispatchEvent(fileDropEvent('drop', [file('a.mp4')]))
    await Promise.resolve()
    expect(upload).toHaveBeenCalledTimes(1)
  })
})

describe('importFiles', () => {
  it('routes audio to the music ingress and hands every file over at once, in drop order', async () => {
    // QA-094: the store's import queue serialises the uploads; this helper
    // must enqueue them all immediately (every file gets its placeholder) and
    // resolve only when the last one is done.
    const handed: string[] = []
    const gates: Array<() => void> = []
    const gated = (tag: string) => vi.fn((f: { name: string }) => {
      handed.push(`${tag}:${f.name}`)
      return new Promise<void>((r) => gates.push(r))
    })
    const upload = gated('video')
    const uploadAudio = gated('audio')
    let done = false
    const all = importFiles([file('a.mp4'), file('song.mp3'), file('voice.bin', 'audio/x-foo')], { upload, uploadAudio })
      .then(() => { done = true })
    expect(handed).toEqual(['video:a.mp4', 'audio:song.mp3', 'audio:voice.bin'])
    gates[0](); gates[1]()
    await Promise.resolve()
    expect(done).toBe(false)
    gates[2]()
    await all
    expect(done).toBe(true)
  })
})

describe('the window listener', () => {
  it('ignores in-app drags (clips, emoji) and cleans up', async () => {
    const { win, upload, stop } = harness()
    const e = new Event('drop', { cancelable: true }) as Event & { dataTransfer: unknown }
    e.dataTransfer = { types: ['application/x-vai-src'], files: [] }
    win.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(false)
    stop()
    win.dispatchEvent(fileDropEvent('drop', [file('a.mp4')]))
    await Promise.resolve()
    expect(upload).not.toHaveBeenCalled()
    expect(isFileDrag(['Files'])).toBe(true)
  })

  it('reports the overlay on while a file drag is over the window', () => {
    const { win, active } = harness()
    win.dispatchEvent(fileDropEvent('dragenter', []))
    win.dispatchEvent(fileDropEvent('dragenter', []))
    win.dispatchEvent(fileDropEvent('dragleave', []))
    win.dispatchEvent(fileDropEvent('dragleave', []))
    expect(active).toEqual([true, true, false])
  })
})
