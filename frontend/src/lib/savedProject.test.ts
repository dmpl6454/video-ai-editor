// The saved-.vae link's identity, and the bug it exists to make impossible.
//
// The shipped flow the owner exercises is Save then Open. Before this, Save in
// session A left `savedUrl` on screen, Open set sessionId to B, and the click
// handler read the LIVE sid — so the visible "↓ .vae" link called
// save_export('B', 'B.vae') for a file that only exists under A. desktop.py
// returns None for a missing source, nativeSave maps None to 'cancelled', and
// TopBar says nothing about a cancel: no dialog, no toast, no download.
//
// These tests compose the two pieces exactly as TopBar does — pick the visible
// record, then claim the click with THAT record's sid — so the composition is
// what is pinned, not just each half.
import { describe, expect, it, vi } from 'vitest'
import { isSavedProjectStale, savedProject, visibleSavedProject } from './savedProject'
import { claimClickForNativeSave, projectFilename } from './nativeSave'

const A = 's_aaaaaaaaaa'
const B = 's_bbbbbbbbbb'
const savedInA = savedProject(A, `/api/sessions/${A}/files/exports/${A}.vae`, 3)

describe('visibleSavedProject', () => {
  it('shows the link in the session it was saved from', () => {
    expect(visibleSavedProject(savedInA, A)).toBe(savedInA)
  })

  it('hides a link belonging to another session', () => {
    // Open a project, switch in the picker, or hit New: all three used to leave
    // the previous project's link sitting in the toolbar.
    expect(visibleSavedProject(savedInA, B)).toBeNull()
  })

  it('brings the link back on returning to that session', () => {
    // The record is kept rather than reset, so switching away is not the same
    // as losing the save.
    expect(visibleSavedProject(visibleSavedProject(savedInA, B) ?? savedInA, A)).toBe(savedInA)
  })

  it('has nothing to show before a save, or with no session', () => {
    expect(visibleSavedProject(null, A)).toBeNull()
    expect(visibleSavedProject(savedInA, null)).toBeNull()
  })
})

describe('isSavedProjectStale', () => {
  it('is clean at the generation it was saved at', () => {
    expect(isSavedProjectStale(savedInA, 3)).toBe(false)
  })

  it('is stale once history advances past it', () => {
    expect(isSavedProjectStale(savedInA, 4)).toBe(true)
  })
})

describe('the link the packaged app actually clicks', () => {
  const host = (save_export: (sid: string, filename: string) => Promise<string | null>) =>
    ({ pywebview: { api: { save_export } } })

  it('hands the bridge the session the file was written from, never the live one', async () => {
    const save = vi.fn(async () => '/Users/me/Desktop/proj.vae')
    const evt = { preventDefault: vi.fn() }
    // Still in session A: the link is visible and claims its own click.
    const link = visibleSavedProject(savedInA, A)!
    const pending = claimClickForNativeSave(evt, link.sid, projectFilename(link.sid), host(save))
    expect(await pending).toEqual({ kind: 'saved', path: '/Users/me/Desktop/proj.vae' })
    expect(save).toHaveBeenCalledWith(A, `${A}.vae`)
    expect(evt.preventDefault).toHaveBeenCalled()
  })

  it('is simply gone after a session change, so no click can ask for the wrong file', () => {
    // The regression: a click here called save_export(B, 'B.vae') — a file that
    // does not exist — and silently did nothing.
    expect(visibleSavedProject(savedInA, B)).toBeNull()
  })
})
