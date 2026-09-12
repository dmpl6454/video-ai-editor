// The pywebview save bridge as both callers see it: store.downloadExport
// (exports) and TopBar's "Saved" .vae link. The host object is injected, so
// no DOM is needed — `window.pywebview` is just `{pywebview:{api:{...}}}`.
import { describe, expect, it, vi } from 'vitest'
import { claimClickForNativeSave, nativeSave, projectFilename, saveExportBridge } from './nativeSave'

const host = (save_export?: (sid: string, filename: string) => Promise<string | null>) =>
  save_export ? { pywebview: { api: { save_export } } } : {}

const click = () => ({ preventDefault: vi.fn() })

describe('saveExportBridge', () => {
  it('is null outside the packaged app (no window.pywebview)', () => {
    expect(saveExportBridge({})).toBeNull()
    expect(saveExportBridge({ pywebview: {} })).toBeNull()
    expect(saveExportBridge({ pywebview: { api: {} } })).toBeNull()
  })

  it('calls save_export as a method of the api object pywebview installed', async () => {
    let receiver: unknown
    const api = { save_export(this: unknown, _sid: string, _f: string) { receiver = this; return Promise.resolve('/x') } }
    await saveExportBridge({ pywebview: { api } })!('s1', 'a.mp4')
    expect(receiver).toBe(api)
  })
})

describe('projectFilename', () => {
  it('is the leaf save_project writes into exports/ — the session id plus .vae', () => {
    expect(projectFilename('abc123')).toBe('abc123.vae')
  })
})

describe('nativeSave', () => {
  it('is null without a bridge, so the caller keeps its anchor', () => {
    expect(nativeSave('s1', 's1.vae', host())).toBeNull()
  })

  it('is null without a session even when the bridge exists', () => {
    const save = vi.fn(async () => '/p')
    expect(nativeSave(null, 'x.vae', host(save))).toBeNull()
    expect(save).not.toHaveBeenCalled()
  })

  it('reports the destination the bridge chose', async () => {
    await expect(nativeSave('s1', 's1.vae', host(async () => '/Users/me/Desktop/s1.vae')))
      .resolves.toEqual({ kind: 'saved', path: '/Users/me/Desktop/s1.vae' })
  })

  it('reports a cancelled dialog (desktop.py returns None) as cancelled, not failed', async () => {
    await expect(nativeSave('s1', 's1.vae', host(async () => null))).resolves.toEqual({ kind: 'cancelled' })
  })

  it('never rejects: a throwing bridge becomes a failed outcome', async () => {
    const boom = new Error('no webview window')
    await expect(nativeSave('s1', 's1.vae', host(async () => { throw boom })))
      .resolves.toEqual({ kind: 'failed', error: boom })
  })
})

describe('claimClickForNativeSave (the "Saved" .vae link)', () => {
  it('bridge present: calls it with the session and <sid>.vae and stops the navigation', async () => {
    const save = vi.fn(async () => '/tmp/s1.vae')
    const evt = click()
    const pending = claimClickForNativeSave(evt, 's1', projectFilename('s1'), host(save))
    expect(evt.preventDefault).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('s1', 's1.vae')
    await expect(pending).resolves.toEqual({ kind: 'saved', path: '/tmp/s1.vae' })
  })

  it('bridge absent: leaves the event alone so the <a download> proceeds', () => {
    const evt = click()
    expect(claimClickForNativeSave(evt, 's1', 's1.vae', host())).toBeNull()
    expect(evt.preventDefault).not.toHaveBeenCalled()
  })

  it('no session: leaves the event alone too', () => {
    const save = vi.fn(async () => '/p')
    const evt = click()
    expect(claimClickForNativeSave(evt, null, 'x.vae', host(save))).toBeNull()
    expect(evt.preventDefault).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })
})
