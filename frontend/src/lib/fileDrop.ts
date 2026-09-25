// File import by drag-and-drop: ONE owner for every OS file drop.
//
// WHY ONE OWNER
// -------------
// The Media panel's dashed box had its own React `onDrop` that uploaded the
// files, and FileDropOverlay's window listener uploaded them too: a drop on the
// box fires the React handler at the root container and then bubbles on to
// `window`. The window listener never checked whether the drop was already
// handled, and the box never stopped propagation, so one drop made two
// concurrent POST /upload calls and two identical clips landed on V1 (the bin
// read "×2 on timeline"). A drop anywhere else in the window imported once.
//
// Checking `defaultPrevented` in the window listener would not be a fix: the
// Timeline's canvas `onDrop` calls preventDefault for every drop it sees
// (including a Finder file it cannot use), so a file dropped on the timeline
// would then import nothing at all. Instead the window listener is the only
// thing that imports an OS file drop, wherever it lands; the dropzone keeps
// only its hover highlight and its click-to-pick input, both of which call
// `importFiles` below — the same routing, the same order.

import { AUDIO_EXTS } from './paths'

export interface FileLike { readonly name: string; readonly type: string }

export interface Importers<F extends FileLike = File> {
  upload(file: F): Promise<void>
  uploadAudio(file: F): Promise<void>
}

/** An OS file drag (dataTransfer carries "Files"), not an in-app clip/emoji drag. */
export function isFileDrag(types: ArrayLike<string> | Iterable<string> | null | undefined): boolean {
  return !!types && Array.from(types as ArrayLike<string>).includes('Files')
}

/** Audio goes to the Music track; everything else is the video ingress. */
export const isAudioFile = (f: FileLike): boolean => AUDIO_EXTS.test(f.name) || f.type.startsWith('audio/')

/**
 * Hand every file to its importer AT ONCE, in drop order, and resolve when
 * all are done. The store's import queue (QA-044/094) runs them one after
 * another — so they still land in drop order and never race to append at the
 * same V1 end — but every file gets its placeholder row the moment it is
 * dropped, and the panel stays busy until the LAST one is in. (This used to
 * await each file before handing over the next, so a three-file drop showed
 * one file and then looked idle between files.)
 */
export async function importFiles<F extends FileLike>(
  files: ArrayLike<F> | Iterable<F> | null | undefined,
  to: Importers<F>,
): Promise<void> {
  if (!files) return
  const runs = Array.from(files as ArrayLike<F>).map((f) => (isAudioFile(f) ? to.uploadAudio(f) : to.upload(f)))
  await Promise.allSettled(runs)
}

// A file drop the TIMELINE placed on a lane itself (QA-093): the window
// listener below still clears the overlay for it but must not import it a
// second time. A marker rather than `defaultPrevented`, for the reason the
// header gives (the canvas prevents default on every drop it sees).
const CLAIMED = new WeakSet<object>()

/** Mark a native drop event as imported by its target. */
export function claimFileDrop(e: Event): void {
  CLAIMED.add(e)
}

export function isClaimedFileDrop(e: Event): boolean {
  return CLAIMED.has(e)
}

// Whether an OS file drag is over the timeline right now. The timeline shows
// its own lane-and-time drop preview then, so the full-window "Drop to import"
// overlay steps aside instead of blurring the lanes it is aiming at (QA-093).
let timelineOver = false
const overListeners = new Set<() => void>()

export function setTimelineFileDragOver(over: boolean): void {
  if (over === timelineOver) return
  timelineOver = over
  for (const l of overListeners) l()
}

export function timelineFileDragOver(): boolean {
  return timelineOver
}

export function subscribeTimelineFileDragOver(listener: () => void): () => void {
  overListeners.add(listener)
  return () => { overListeners.delete(listener) }
}

interface DropEventLike extends Event {
  dataTransfer?: { types?: ArrayLike<string>; files?: ArrayLike<File>; dropEffect?: string } | null
}

/**
 * Window-level file drag-and-drop: prevents the browser from navigating to a
 * stray dropped file, reports whether a file drag is over the window (for the
 * full-window overlay), and imports every dropped file exactly once.
 * Returns the cleanup that removes the listeners.
 */
export function installWindowFileDrop(
  target: EventTarget,
  opts: { setActive(active: boolean): void; importFiles(files: ArrayLike<File>): void },
): () => void {
  // dragenter/dragleave fire for every child element; depth tells when the
  // pointer has really left the window.
  let depth = 0
  const types = (e: DropEventLike) => e.dataTransfer?.types

  const onDragEnter = (e: Event) => {
    if (!isFileDrag(types(e as DropEventLike))) return
    e.preventDefault()
    depth += 1
    opts.setActive(true)
  }
  const onDragOver = (e: Event) => {
    const d = e as DropEventLike
    if (!isFileDrag(types(d))) return
    e.preventDefault()
    if (d.dataTransfer) d.dataTransfer.dropEffect = 'copy'
  }
  const onDragLeave = (e: Event) => {
    if (!isFileDrag(types(e as DropEventLike))) return
    depth = Math.max(0, depth - 1)
    if (depth === 0) { opts.setActive(false); setTimelineFileDragOver(false) }
  }
  const onDrop = (e: Event) => {
    const d = e as DropEventLike
    if (!isFileDrag(types(d))) return
    e.preventDefault()
    depth = 0
    opts.setActive(false)
    setTimelineFileDragOver(false)
    if (isClaimedFileDrop(e)) return
    const files = d.dataTransfer?.files
    if (files && files.length) opts.importFiles(files)
  }

  target.addEventListener('dragenter', onDragEnter)
  target.addEventListener('dragover', onDragOver)
  target.addEventListener('dragleave', onDragLeave)
  target.addEventListener('drop', onDrop)
  return () => {
    target.removeEventListener('dragenter', onDragEnter)
    target.removeEventListener('dragover', onDragOver)
    target.removeEventListener('dragleave', onDragLeave)
    target.removeEventListener('drop', onDrop)
  }
}

/**
 * The Media panel dropzone's drag handlers: a hover highlight and nothing
 * else. The drop itself bubbles to the window listener above, which imports
 * it — so this handler must never import, or the same drop imports twice.
 */
export function dropzoneHandlers(setOver: (over: boolean) => void) {
  return {
    onDragOver: (e: { preventDefault(): void }) => { e.preventDefault(); setOver(true) },
    onDragLeave: () => setOver(false),
    onDrop: () => setOver(false),
  }
}
