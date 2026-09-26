// Settings (QA-063-SETTINGS, QA-106-CACHE-UI): the one place a person can give
// the packaged app an Anthropic key, see which brains can answer and why, see
// which models and voices are on this Mac, and reclaim render-cache space.
// Reached from the top bar's gear and ⌘, (keymap command `openSettings`).
//
// Built on THE app dialog (components/Dialog: focus in, Tab trapped, Escape,
// inert editor) and ConfirmDialog for every destructive or network step: a
// model download names its size and asks first; removing the key and clearing
// the cache ask first.
//
// The key: typed into a password field, sent ONCE to the backend, which keeps
// it in the macOS Keychain (keychain.py) and only ever answers with a masked
// suffix. The field is cleared the moment the save answers, success or not.

import { useCallback, useEffect, useId, useState, type ReactNode } from 'react'
import { api, type PromptModelRow, type RenderCacheUsage } from '../api'
import { errorMessage, useStore } from '../store'
import { toast } from '../toast'
import { usePromptStore } from '../lib/promptStore'
import { humanBytes, isLoopbackOrigin, shortModel } from '../lib/promptEvents'
import type { DownloadReport } from '../lib/modelDownloads'
import { projectLabel } from '../lib/projectName'
import { bannerSentence, mediaToolsProblem, type MediaToolsProblem } from '../lib/mediaTools'
import { useModelDownload } from '../lib/useModelDownload'
import { registerSettingsOpener } from '../lib/settingsOpen'
import { cacheLine, canSaveKey, freedMessage, keyInputProblem, keyStatusLine, modelConsentText,
         weightRows, type KeyStatus } from '../lib/settingsModel'
import { BRAINS_HEADING, BRAINS_HELP, BrainRows, CHECK_AGAIN } from './BrainRows'
import { Dialog } from './Dialog'
import { ConfirmDialog } from './ConfirmDialog'
import { Icon, type IconName } from './Icon'
import './settingsDialog.css'

export function SettingsDialog() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    registerSettingsOpener(() => setOpen(true))
    return () => registerSettingsOpener(null)
  }, [])
  const titleId = useId()
  return (
    <Dialog open={open} title="Settings" labelId={titleId} onClose={() => setOpen(false)}
            className="settings-dialog">
      <ClaudeKeySection />
      <BrainsSection />
      <ModelsSection />
      <MediaToolsSection />
      <StorageSection />
    </Dialog>
  )
}

function Section({ icon, title, children }: { icon: IconName; title: string; children: ReactNode }) {
  const id = useId()
  return (
    <section className="settings-section" aria-labelledby={id}>
      <h3 id={id}><Icon name={icon} /> {title}</h3>
      {children}
    </section>
  )
}

const DESKTOP_ONLY = 'This can only be changed on the Mac running the editor.'

// ---------------------------------------------------------------- Claude key

function ClaudeKeySection() {
  const loadBrains = usePromptStore((s) => s.loadBrains)
  const loopback = isLoopbackOrigin()
  const [status, setStatus] = useState<KeyStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState<'save' | 'test' | 'remove' | null>(null)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const inputId = useId()
  const hintId = useId()

  useEffect(() => {
    if (!loopback) return
    api.anthropicKeyStatus().then(setStatus).catch((e) => setLoadError(errorMessage(e)))
  }, [loopback])

  const problem = keyInputProblem(value)
  const line = status ? keyStatusLine(status) : null

  const test = async () => {
    setBusy('test')
    setResult(null)
    try { setResult(await api.testAnthropicKey()) } catch (e) { setResult({ ok: false, message: errorMessage(e) }) }
    finally { setBusy(null) }
  }

  const save = async () => {
    if (!canSaveKey(value)) return
    const key = value.trim()
    setValue('')            // never keep the key in UI state longer than the request
    setBusy('save')
    setResult(null)
    try {
      setStatus(await api.saveAnthropicKey(key))
      toast.success('Key saved to your Keychain.')
      void loadBrains(true)
      await test()
    } catch (e) {
      setResult({ ok: false, message: errorMessage(e) })
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    setConfirmRemove(false)
    setBusy('remove')
    setResult(null)
    try {
      setStatus(await api.removeAnthropicKey())
      toast.info('Key removed from your Keychain. Chat keeps working on this Mac.')
      void loadBrains(true)
    } catch (e) {
      setResult({ ok: false, message: errorMessage(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Section icon="key" title="Claude">
      <p className="settings-help">
        Claude is optional: chat and the Prompt bar work on this Mac without it. With an Anthropic
        API key, harder requests can also go to Claude over the internet.
      </p>
      {!loopback && <p className="settings-status" data-tone="muted">{DESKTOP_ONLY}</p>}
      {loadError && <p className="settings-status" data-tone="warn" role="alert">Couldn’t read the key status: {loadError}</p>}
      {line && <p className="settings-status" data-tone={line.tone} aria-live="polite">{line.text}</p>}
      {status?.can_edit && (
        <form className="settings-key-row" onSubmit={(e) => { e.preventDefault(); void save() }}>
          <label htmlFor={inputId} className="settings-visually-hidden">Anthropic API key</label>
          <input id={inputId} type="password" value={value} autoComplete="off" spellCheck={false}
                 autoCapitalize="off" autoCorrect="off" data-keymap-ignore
                 placeholder={status.configured ? 'Paste a new key to replace it' : 'sk-ant-…'}
                 aria-describedby={hintId} aria-invalid={problem ? true : undefined}
                 onChange={(e) => setValue(e.target.value)} />
          <button type="submit" className="primary" disabled={!canSaveKey(value) || busy !== null}>
            {busy === 'save' ? 'Saving…' : 'Save'}
          </button>
        </form>
      )}
      {status?.can_edit && (
        <p id={hintId} className={problem ? 'settings-problem' : 'settings-help'}>
          {problem ?? 'Create a key at console.anthropic.com › API keys. It is kept in your Mac’s Keychain, never in a file.'}
        </p>
      )}
      {status && (status.configured || status.can_edit) && (
        <div className="settings-actions">
          <button type="button" disabled={!status.configured || busy !== null} onClick={() => void test()}>
            {busy === 'test' ? 'Checking…' : 'Test key'}
          </button>
          {status.can_edit && status.configured && (
            <button type="button" disabled={busy !== null} onClick={() => setConfirmRemove(true)}>
              {busy === 'remove' ? 'Removing…' : 'Remove key'}
            </button>
          )}
          {result && (
            <span className="settings-result" data-tone={result.ok ? 'ok' : 'warn'} role="status">
              <Icon name={result.ok ? 'ok' : 'warning'} /> {result.message}
            </span>
          )}
        </div>
      )}
      {confirmRemove && (
        <ConfirmDialog title="Remove your Anthropic key?"
          body="The key is deleted from this Mac’s Keychain and Claude is switched off. Chat and the Prompt bar keep working on this Mac. You can add a key again any time."
          confirmLabel="Remove key" danger onConfirm={() => void remove()} onCancel={() => setConfirmRemove(false)} />
      )}
    </Section>
  )
}

// ------------------------------------------------------------------- brains

function BrainsSection() {
  const brains = usePromptStore((s) => s.brains)
  const brainsError = usePromptStore((s) => s.brainsError)
  const loading = usePromptStore((s) => s.brainsLoading)
  const loadBrains = usePromptStore((s) => s.loadBrains)
  useEffect(() => { void loadBrains(true) }, [loadBrains])
  return (
    <Section icon="brain" title={BRAINS_HEADING}>
      <p className="settings-help">{BRAINS_HELP}</p>
      {brainsError && <p className="settings-status" data-tone="warn">Couldn’t check: {brainsError}</p>}
      {!brains && !brainsError && <p className="settings-status" data-tone="muted">Checking…</p>}
      {/* In here the key field is right above; the popover's "add it in Settings" would point at itself. */}
      {brains && <BrainRows rows={brains.brains}
        fixFor={(row, fix) => (row.action === 'add_key' && !row.available ? 'Add your key under Claude above.' : fix)} />}
      <div className="settings-actions">
        <button type="button" disabled={loading} onClick={() => void loadBrains(true)}>{loading ? 'Checking…' : CHECK_AGAIN}</button>
      </div>
    </Section>
  )
}

// ------------------------------------------------------------ models/voices

/** What the models section lists: every first-run download, and (on the Mac
 *  itself — the route is loopback-only) the local models. */
function fetchModelLists(loopback: boolean) {
  return Promise.all([api.getDownloads(), loopback ? api.promptModels() : Promise.resolve(null)])
}

function ModelsSection() {
  const loopback = isLoopbackOrigin()
  const brains = usePromptStore((s) => s.brains)
  const loadBrains = usePromptStore((s) => s.loadBrains)
  const [weights, setWeights] = useState<DownloadReport | null>(null)
  const [models, setModels] = useState<PromptModelRow[] | null>(null)
  const [tier, setTier] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ask, setAsk] = useState<{ kind: 'download' | 'delete'; row: PromptModelRow } | null>(null)

  const apply = useCallback(([d, m]: Awaited<ReturnType<typeof fetchModelLists>>) => {
    setWeights(d.downloads)
    if (m) { setModels(m.models ?? []); setTier(m.tier ?? null) }
  }, [])
  const fail = useCallback((e: unknown) => setError(errorMessage(e)), [])
  const refresh = useCallback(() => fetchModelLists(loopback).then(apply, fail), [loopback, apply, fail])
  useEffect(() => { fetchModelLists(loopback).then(apply, fail) }, [loopback, apply, fail])
  const afterJob = useCallback(async () => { await refresh(); await loadBrains(true) }, [refresh, loadBrains])
  const { dl, start, remove, cancel } = useModelDownload(afterJob)

  const localRow = brains?.brains.find((b) => b.id === 'local_model') ?? null
  // Only offer a download the local-model brain can actually use (the
  // packaged app may not include it — then there is nothing to download FOR).
  const canDownload = loopback && localRow?.action === 'download'

  return (
    <Section icon="storage" title="Models and voices">
      <p className="settings-help">Downloaded once, then used on this Mac with no internet. Nothing downloads without asking.</p>
      {error && <p className="settings-status" data-tone="warn">Couldn’t list models: {error}</p>}
      {models && models.length > 0 && (
        <>
          <h4 className="settings-subhead">Local model for the Prompt bar</h4>
          <ul className="settings-rows">
            {models.map((m) => {
              const running = dl.status === 'running' && dl.id === m.id
              const size = m.installed ? m.bytes_on_disk : m.expected_bytes
              return (
                <li className="settings-row" key={m.id}>
                  <span className="settings-row-name">
                    <span className="settings-dot" data-on={m.installed ? 'true' : 'false'} aria-hidden="true" />
                    {shortModel(m.id)}{m.id === tier ? ' · suggested for this Mac' : ''}
                  </span>
                  <span className="settings-row-side">
                    {size ? humanBytes(size) : ''}
                    {running ? (
                      <button type="button" onClick={() => void cancel()} aria-label={`Cancel ${dl.status === 'running' && dl.action === 'delete' ? 'removing' : 'downloading'} ${shortModel(m.id)}`}>
                        Cancel {Math.round((dl.status === 'running' ? dl.progress : 0) * 100)}%
                      </button>
                    ) : m.installed ? (
                      loopback && <button type="button" disabled={dl.status === 'running'} onClick={() => setAsk({ kind: 'delete', row: m })}
                                          aria-label={`Remove ${shortModel(m.id)} from this Mac`}>Remove</button>
                    ) : canDownload && (
                      <button type="button" disabled={dl.status === 'running'} onClick={() => setAsk({ kind: 'download', row: m })}
                              aria-label={`Download ${shortModel(m.id)}${size ? `, ${humanBytes(size)}` : ''}`}>
                        <Icon name="download" /> Download
                      </button>
                    )}
                  </span>
                  <span className="settings-row-meta">
                    {m.installed ? 'On this Mac' : 'Not downloaded'}
                  </span>
                </li>
              )
            })}
          </ul>
          {dl.status === 'error' && <p className="settings-status" data-tone="warn" role="alert">{dl.message}</p>}
        </>
      )}
      {!loopback && <p className="settings-status" data-tone="muted">{DESKTOP_ONLY}</p>}
      <h4 className="settings-subhead">Captions, voices and AI tools</h4>
      {!weights && !error && <p className="settings-status" data-tone="muted">Checking…</p>}
      <ul className="settings-rows">
        {weightRows(weights).map((w) => (
          <li className="settings-row" key={w.key}>
            <span className="settings-row-name">
              <span className="settings-dot" data-on={w.cached ? 'true' : 'false'} aria-hidden="true" />
              {w.name}
            </span>
            <span className="settings-row-side">{w.size}</span>
            <span className="settings-row-meta">{w.usedBy} · {w.state}</span>
          </li>
        ))}
      </ul>
      {ask && (
        <ConfirmDialog
          title={ask.kind === 'download' ? `Download ${shortModel(ask.row.id)}?` : `Remove ${shortModel(ask.row.id)}?`}
          body={ask.kind === 'download'
            ? modelConsentText(ask.row.expected_bytes, ask.row.free_bytes)
            : `This frees ${humanBytes(ask.row.bytes_on_disk ?? 0)}. The Prompt bar falls back to the other brains, and you can download it again later.`}
          confirmLabel={ask.kind === 'download' ? 'Download' : 'Remove'}
          danger={ask.kind === 'delete'}
          onConfirm={() => { const a = ask; setAsk(null); void (a.kind === 'download' ? start(a.row.id) : remove(a.row.id)) }}
          onCancel={() => setAsk(null)} />
      )}
    </Section>
  )
}

// ------------------------------------------------------------ video engine

/** ffmpeg's status (QA-108). The top-of-window notice can be hidden for the
 *  session; this is where "Check again" stays reachable. */
function MediaToolsSection() {
  const [problem, setProblem] = useState<MediaToolsProblem | null | undefined>(undefined)
  // Starts "checking": the first read is in flight from mount.
  const [checking, setChecking] = useState(true)
  const read = useCallback(() => api.health().then((h) => setProblem(mediaToolsProblem(h)))
    .catch(() => setProblem(undefined)).finally(() => setChecking(false)), [])
  const check = () => { setChecking(true); void read() }
  useEffect(() => { void read() }, [read])
  return (
    <Section icon="film" title="Video engine">
      {problem === undefined && <p className="settings-status" data-tone="muted">{checking ? 'Checking…' : 'Couldn’t check.'}</p>}
      {problem === null && <p className="settings-status">ffmpeg is installed — import, preview and export can run.</p>}
      {problem && (
        <>
          <p className="settings-status" data-tone="warn">{bannerSentence(problem)}</p>
          <p className="settings-help"><code>{problem.command}</code></p>
        </>
      )}
      <div className="settings-actions">
        <button type="button" disabled={checking} onClick={check}>{checking ? 'Checking…' : CHECK_AGAIN}</button>
      </div>
    </Section>
  )
}

// ------------------------------------------------------------ render cache

function StorageSection() {
  const sid = useStore((s) => s.sessionId)
  const name = useStore((s) => s.sessionName)
  const [usage, setUsage] = useState<RenderCacheUsage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [clearing, setClearing] = useState(false)

  useEffect(() => {
    if (!sid) return
    api.renderCacheUsage(sid).then(setUsage).catch((e) => setError(errorMessage(e)))
  }, [sid])

  const clear = async () => {
    setConfirm(false)
    if (!sid) return
    setClearing(true)
    try {
      const r = await api.clearRenderCache(sid)
      setUsage(r)
      toast.success(freedMessage(r.freed_bytes))
    } catch (e) {
      toast.error(`Couldn’t clear the render cache: ${errorMessage(e)}`)
    } finally {
      setClearing(false)
    }
  }

  const fill = usage && usage.budget_bytes > 0 ? Math.min(1, usage.bytes / usage.budget_bytes) : 0
  const project = projectLabel(name, sid)
  return (
    <Section icon="film" title="Render cache">
      <p className="settings-help">
        Previews of “{project}” saved so playback is instant. Safe to clear: they are rebuilt when needed.
      </p>
      {error && <p className="settings-status" data-tone="warn">Couldn’t read the cache size: {error}</p>}
      <div className="settings-cache">
        <span className="settings-cache-line">{sid ? cacheLine(usage) : 'Open a project first'}</span>
        <span className="settings-bar" role="meter" aria-label="Render cache used" aria-valuemin={0}
              aria-valuemax={100} aria-valuenow={Math.round(fill * 100)}>
          <span style={{ transform: `scaleX(${fill})` }} />
        </span>
        <button type="button" disabled={!sid || clearing || !usage || usage.bytes === 0} onClick={() => setConfirm(true)}>
          {clearing ? 'Clearing…' : 'Clear render cache'}
        </button>
      </div>
      {confirm && (
        <ConfirmDialog title="Clear the render cache?"
          body={`Previews for “${project}”${usage ? ` (${humanBytes(usage.bytes)})` : ''} are deleted and rebuilt when you play or export. The one on screen is kept. Your media, exports and edit history are not touched.`}
          confirmLabel="Clear cache" onConfirm={() => void clear()} onCancel={() => setConfirm(false)} />
      )}
    </Section>
  )
}
