// TopBar "CC Captions" — auto-captions, with the caption language chosen up front.
//
// Dispatches `auto_caption` (agent/dispatch.py): re-transcribes the v1 source
// with Whisper large-v3, builds broadcast-style cues, and lays down the caption
// track. The caption cues land as TextClips that TextLayer.tsx previews
// client-side — no preview re-render is needed for them to appear.
//
// TWO things this component exists to get right:
//
// 1. The wait must LOOK like work, and it varies by two orders of magnitude
//    depending on the machine. Measured decode time for 60s of Hindi with
//    large-v3: 185.6s sequential on CPU, 95.2s batched on CPU, 8.4s batched on
//    a CUDA GPU. So a 3-minute video is anywhere from ~25 seconds to ~9 minutes.
//    The old button showed an indefinite spinner for all of it, which is
//    indistinguishable from a hang and was reported as "the caption generator
//    button is not working" when it was working perfectly.
//
//    `etaSeconds` therefore EXTRAPOLATES from this run's own elapsed time and
//    reported progress rather than from any hardcoded rate — it has to
//    self-calibrate, because a constant tuned for the CPU path would promise
//    "~9 min" for something a GPU finishes in 25 seconds, and a wrong ETA is
//    worse than none. Note batched decoding reports progress COARSELY (roughly
//    once per 30s of audio, since it emits few long segments), so on a slow CPU
//    the first update can be ~48s in; the elapsed counter is what carries the
//    UI until then.
//
// 2. The caption language is not the spoken language. A Chinese video can be
//    captioned in Hindi; `target` picks the OUTPUT language and the backend
//    routes accordingly (Whisper's own translation into English, then a local
//    Argos hop into Hindi, then romanisation for Hinglish). The choice is
//    remembered, because a user who wants Hinglish wants it every time.
//
// SPEED is a third, independent choice: large-v3 (default, most accurate) or
// large-v3-turbo (~4x faster, measured). Turbo cannot serve English's
// translate task — it was fine-tuned on transcription only and asked to
// translate it returns a handful of ellipses, not text — so `auto_caption`
// silently substitutes large-v3 in that one case and reports back which model
// actually ran (`result.model`). The toast surfaces that substitution instead
// of letting "I picked Fastest" and "it used the slow model" go unexplained.
//
// Failure modes: no v1 clip → 400 ValueError; missing whisper model/binary →
// 422 RuntimeError; no translation package for an exotic source language → 422
// naming English as the way out. All carry a user-readable message which
// store.dispatch already toasts verbatim.

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store'
import { api } from '../api'
import { toast } from '../toast'
import { isMediaClip } from '../types'
import './CaptionsButton.css'

type Target = 'as-spoken' | 'en' | 'hi' | 'hinglish' | 'es'
type Speed = 'quality' | 'fast'

const TARGETS: { id: Target; label: string; hint: string }[] = [
  { id: 'as-spoken', label: 'As spoken', hint: 'Caption in whatever language the video is in' },
  { id: 'en', label: 'English', hint: 'Translate to English (works from any language)' },
  { id: 'hi', label: 'हिंदी Hindi', hint: 'Hindi in Devanagari script' },
  { id: 'hinglish', label: 'Hinglish', hint: 'Hindi written in Latin letters — "apni last meeting ke baad"' },
  { id: 'es', label: 'Español', hint: 'Translate to Spanish (works from any language)' },
]

// The caret's compact label. A lookup rather than `label.slice(-2)` — that
// fallback produced 'ol' for "Español" and was already showing 'sh'/'di' for
// English/Hindi (the trailing ASCII of "English"/"...Hindi"), neither of
// which reads as anything at a glance. An explicit map scales to more
// languages without each new one needing its own ternary.
const SHORT_LABEL: Record<Target, string> = {
  'as-spoken': 'Auto', en: 'Eng', hi: 'Hi', hinglish: 'Hing', es: 'Esp',
}

const SPEEDS: { id: Speed; label: string; hint: string }[] = [
  { id: 'quality', label: 'Best quality', hint: 'large-v3 — the most accurate model' },
  { id: 'fast', label: 'Fastest', hint: '~4x faster (large-v3-turbo). Falls back to the accurate '
      + "model automatically for the English target, which turbo can't translate." },
]

// The literal faster-whisper model name — must match a name transcribe.py
// recognises (verified: WhisperModel("large-v3-turbo", …) loads correctly).
const TURBO_MODEL = 'large-v3-turbo'

const STORE_KEY = 'vai.captionTarget'
const STORE_KEY_SPEED = 'vai.captionSpeed'

function loadTarget(): Target {
  try {
    const v = localStorage.getItem(STORE_KEY)
    if (v && TARGETS.some((t) => t.id === v)) return v as Target
  } catch { /* private mode / storage disabled — the default is fine */ }
  return 'as-spoken'
}

function loadSpeed(): Speed {
  try {
    const v = localStorage.getItem(STORE_KEY_SPEED)
    if (v && SPEEDS.some((s) => s.id === v)) return v as Speed
  } catch { /* private mode / storage disabled — the default is fine */ }
  return 'quality'
}

/** Seconds remaining, extrapolated from how long the job has taken to reach
 *  `progress`. Returns null until there is enough signal to be honest about
 *  it — a wildly wrong ETA is worse than none. */
function etaSeconds(elapsed: number, progress: number): number | null {
  if (progress <= 0.02 || elapsed < 3) return null
  const total = elapsed / progress
  const left = Math.max(0, total - elapsed)
  return left > 1 ? left : null
}

function formatEta(sec: number): string {
  if (sec < 60) return `${Math.ceil(sec)}s left`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return s >= 30 ? `~${m + 1} min left` : `~${m || 1} min left`
}

export function CaptionsButton() {
  const dispatch = useStore((s) => s.dispatch)
  const edl = useStore((s) => s.edl)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [startedAt, setStartedAt] = useState(0)
  const [cancelling, setCancelling] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null)
  const [target, setTarget] = useState<Target>(loadTarget)
  const [speed, setSpeed] = useState<Speed>(loadSpeed)
  const jobRef = useRef<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const caretRef = useRef<HTMLButtonElement | null>(null)

  // auto_caption transcribes the first media clip on v1 — mirror that guard
  // here so the button is disabled (with an explaining tooltip) instead of
  // dispatching a guaranteed 400.
  const hasFootage = !!edl?.tracks
    .find((t) => t.id === 'v1')
    ?.clips.some(isMediaClip)

  // Elapsed-time ticker: the backend reports progress per decoded segment,
  // which on a long clip can be many seconds apart, so the seconds counter is
  // what tells the user the app is alive between those updates.
  //
  // `startedAt` is stamped in the click handler rather than here, so this
  // effect only starts a timer and never assigns state during its own run —
  // the cascading-render pattern react-hooks/set-state-in-effect flags, and
  // which this file has no reason to add to.
  useEffect(() => {
    if (!busy || !startedAt) return
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => window.clearInterval(id)
  }, [busy, startedAt])

  // The menu is rendered via a portal to document.body (positioned from
  // caretRef's rect) rather than as a normal absolutely-positioned child of
  // this button — TopBar's toolbar clips overflow on both axes to keep itself
  // on one line (`.topbar { overflow: hidden }`, see its session-picker and
  // export-options popovers, which needed the identical fix for the identical
  // reason: "dropdown is half-cut when clicked"). This component sits inside
  // that same toolbar, so a plain `position:absolute` menu was being clipped
  // to nothing the moment it opened — reported as "there is only an Auto
  // option", when in fact all three languages were there and even being sent
  // correctly; the menu that offered them was simply invisible.
  //
  // Position is computed in an EFFECT, not inline during render — reading a
  // ref's .current mid-render doesn't participate in React's reactivity model
  // and can observe a stale layout (react-hooks/refs flags this for a real
  // reason, not just style; TopBar's popovers follow the same rule).
  useEffect(() => {
    if (!menuOpen) return
    const rect = caretRef.current?.getBoundingClientRect()
    if (rect) setMenuPos({ left: rect.right, top: rect.bottom + 4 })
    const onDown = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      // The portaled menu lives OUTSIDE wrapRef's subtree, so `contains()`
      // alone would see every click inside it as "outside" and close the menu
      // the instant you tried to pick anything. `data-cc-menu` marks both the
      // trigger and the portaled content, mirroring TopBar's `data-session-
      // picker`/`data-export-opts` convention for the same portal shape.
      if (!wrapRef.current?.contains(tgt) && !tgt.closest('[data-cc-menu]')) {
        setMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenuOpen(false) }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  // Neither picker closes the menu — there are now two independent choices
  // (language, speed) and closing after the first would force reopening it to
  // set the second. The outside-click/Escape handler above is how it closes.
  const pick = (t: Target) => {
    setTarget(t)
    try { localStorage.setItem(STORE_KEY, t) } catch { /* not worth failing over */ }
  }

  const pickSpeed = (s: Speed) => {
    setSpeed(s)
    try { localStorage.setItem(STORE_KEY_SPEED, s) } catch { /* not worth failing over */ }
  }

  // Cancelling is not instant and pretending otherwise would repeat the very
  // mistake this component exists to fix. The decoder can only be interrupted
  // BETWEEN segments, and faster-whisper emits those per 30s window — measured
  // 58s from pressing Cancel to the job reporting `cancelled` on a short clip.
  // So the button switches to a "Stopping…" state and keeps the elapsed
  // counter running rather than freezing or lying about being done.
  const cancel = async () => {
    const id = jobRef.current
    if (!id) return
    setCancelling(true)
    try {
      await api.cancelJob(id)
    } catch {
      setCancelling(false)
      toast.error('Could not cancel — it may have already finished')
    }
  }

  const run = async () => {
    if (busy || !hasFootage) return
    setBusy(true)
    setProgress(0)
    setElapsed(0)
    setStartedAt(Date.now())
    setCancelling(false)
    jobRef.current = null
    try {
      // 'as-spoken' sends no target at all, which is the backend's own default
      // and the behaviour every pre-existing caller relies on. Same idea for
      // speed: 'quality' sends no `model` at all, so it rides WHISPER_CAPTION_MODEL
      // (default large-v3) exactly as every existing caller already does.
      const args: Record<string, unknown> = target === 'as-spoken' ? {} : { target }
      if (speed === 'fast') args.model = TURBO_MODEL
      const res = await dispatch('auto_caption', args, {
        onProgress: ({ jobId, progress: p }) => {
          jobRef.current = jobId
          setProgress(p)
        },
      })
      if (res) {
        const r = res.result as { cues?: number; language?: string; spoken?: string; model?: string } | null
        const lang = r?.language === 'hi-Latn' ? 'Hinglish' : r?.language === 'es' ? 'Spanish' : r?.language
        const from = r?.spoken && r.spoken !== r.language ? ` from ${r.spoken}` : ''
        // `r.model` is the model that ACTUALLY ran (auto_caption resolves the
        // substitution before reporting it) — so if Fastest was requested but
        // this doesn't say turbo, it's because the English target needed the
        // accurate model. Surface that instead of leaving it a silent surprise.
        const fellBack = speed === 'fast' && r?.model && !r.model.toLowerCase().includes('turbo')
        const modelNote = fellBack ? ` · used ${r!.model} (turbo can't translate)` : ''
        toast.success(
          typeof r?.cues === 'number'
            ? `Captions added — ${r.cues} cues${lang ? ` (${lang}${from})` : ''}${modelNote}`
            : 'Captions added',
        )
      }
      // res === null → the failure toast already fired inside store.dispatch.
    } finally {
      setBusy(false)
      setProgress(0)
      setCancelling(false)
      jobRef.current = null
    }
  }

  const current = TARGETS.find((t) => t.id === target) ?? TARGETS[0]
  const currentSpeed = SPEEDS.find((s) => s.id === speed) ?? SPEEDS[0]
  const pct = Math.round(progress * 100)
  const eta = etaSeconds(elapsed, progress)

  if (busy) {
    return (
      <span className="cc-busy" role="status" aria-live="polite">
        <span className="cc-spinner" aria-hidden="true" />
        {cancelling ? (
          <span className="cc-busy-text">
            Stopping…
            <span className="cc-busy-sub">
              finishing the current chunk · {elapsed}s
            </span>
          </span>
        ) : (
          <>
            <span className="cc-busy-text">
              Transcribing… {pct > 0 ? `${pct}%` : ''}
              <span className="cc-busy-sub">
                {eta ? formatEta(eta) : `${elapsed}s`}
              </span>
            </span>
            <span className="cc-bar" aria-hidden="true">
              <span className="cc-bar-fill" style={{ width: `${Math.max(2, pct)}%` }} />
            </span>
            <button className="cc-cancel" onClick={() => { void cancel() }}
                    title="Stop transcribing">Cancel</button>
          </>
        )}
      </span>
    )
  }

  return (
    <span className="cc-wrap" ref={wrapRef}>
      <button
        className="cc-main"
        onClick={() => { void run() }}
        disabled={!hasFootage}
        title={!hasFootage
          ? 'Add a video to the timeline first — captions transcribe the main (v1) footage'
          : `Auto-captions in ${current.label}, ${currentSpeed.label.toLowerCase()} — `
            + `re-transcribes the footage with Whisper ${speed === 'fast' ? 'large-v3-turbo' : 'large-v3'}. `
            + 'A long clip can take a while; progress and a Cancel button appear while it runs.'}
        style={{ fontSize: 11 }}
      >
        <b>CC</b> Captions
      </button>
      <button
        ref={caretRef}
        className="cc-caret"
        data-cc-menu
        onClick={() => setMenuOpen((o) => !o)}
        disabled={!hasFootage}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={`Caption language: ${current.label}, speed: ${currentSpeed.label}`}
        title={`Caption language: ${current.label} · Speed: ${currentSpeed.label}`}
      >{SHORT_LABEL[current.id]}
        {speed === 'fast' ? '⚡' : ''} ▾</button>

      {menuOpen && menuPos && createPortal(
        <div
          className="cc-menu"
          data-cc-menu
          role="menu"
          style={{
            position: 'fixed',
            left: menuPos.left,
            top: menuPos.top,
            transform: 'translateX(-100%)',   // right-align under the caret, like the old right:0
          }}
        >
          <div className="cc-menu-head">Caption language</div>
          {TARGETS.map((t) => (
            <button
              key={t.id}
              role="menuitemradio"
              aria-checked={t.id === target}
              className={`cc-menu-item${t.id === target ? ' is-on' : ''}`}
              onClick={() => pick(t.id)}
              title={t.hint}
            >
              <span className="cc-check" aria-hidden="true">{t.id === target ? '●' : ''}</span>
              <span>
                <span className="cc-menu-label">{t.label}</span>
                <span className="cc-menu-hint">{t.hint}</span>
              </span>
            </button>
          ))}
          <div className="cc-menu-sep" role="separator" />
          <div className="cc-menu-head">Speed</div>
          {SPEEDS.map((s) => (
            <button
              key={s.id}
              role="menuitemradio"
              aria-checked={s.id === speed}
              className={`cc-menu-item${s.id === speed ? ' is-on' : ''}`}
              onClick={() => pickSpeed(s.id)}
              title={s.hint}
            >
              <span className="cc-check" aria-hidden="true">{s.id === speed ? '●' : ''}</span>
              <span>
                <span className="cc-menu-label">{s.label}</span>
                <span className="cc-menu-hint">{s.hint}</span>
              </span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </span>
  )
}
