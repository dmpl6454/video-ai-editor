import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'

// Narrow shape of the bridge desktop.py's `_Api` exposes over pywebview's
// js_api — only the two methods this file calls, not the whole class.
interface PywebviewVoBridge {
  pywebview?: {
    api?: {
      vo_start?: (sessionId: string) => Promise<{ ok: boolean; error?: string }>
      vo_stop?: (sessionId: string, start: number, gainDb: number) =>
        Promise<{ ok: boolean; error?: string; clip_id?: string }>
    }
  }
}

/**
 * Mic voiceover recorder.
 *
 * Two capture paths:
 *  - Browser-dev mode (`:5173` or any real browser): `navigator.mediaDevices
 *    .getUserMedia` + MediaRecorder captures an Opus/WebM blob, posted to
 *    /vo_record, which transcodes to AAC and drops a clip on the vo track.
 *  - Packaged app (pywebview): getUserMedia is unusable there regardless of
 *    the Info.plist mic entitlement — WKWebView's Cocoa backend implements
 *    no media-capture permission delegate, AND the app is served over a
 *    non-TLS custom-port origin WKWebView won't treat as a secure context,
 *    so `navigator.mediaDevices` itself is typically `undefined`. Route
 *    Record/Stop through desktop.py's native `vo_start`/`vo_stop` js_api
 *    bridge instead, which captures via ffmpeg's avfoundation input and
 *    posts straight to the same /vo_record endpoint server-side. Detected by
 *    checking for the bridge methods FIRST (before falling back to
 *    getUserMedia), since a pywebview window may or may not also expose a
 *    (non-functional) `navigator.mediaDevices`.
 */
export function VoRecorder() {
  const sid = useStore((s) => s.sessionId)
  const playhead = useStore((s) => s.playhead)
  const refresh = useStore((s) => s.refresh)
  const setPlaying = useStore((s) => s.setPlaying)
  const [recording, setRecording] = useState(false)
  const [requesting, setRequesting] = useState(false)  // mic-permission prompt in flight
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const recRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const startedAtRef = useRef<number>(0)
  const tickRef = useRef<number | null>(null)
  const nativeRecordingRef = useRef(false)  // true while a native (pywebview) capture is in flight

  // Release the mic + stop the elapsed ticker. Idempotent — safe to call on any
  // exit path (stop, error, unmount). Leaving the stream open keeps the OS mic
  // indicator lit, which reads as "stuck recording" even when the button isn't.
  const teardown = () => {
    if (tickRef.current) { window.clearInterval(tickRef.current); tickRef.current = null }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop())
      streamRef.current = null
    }
  }

  useEffect(() => () => {
    // cleanup on unmount
    if (recRef.current && recRef.current.state !== 'inactive') {
      recRef.current.stop()
    }
    if (nativeRecordingRef.current) {
      // Fire-and-forget: an unmount can't await. Best-effort so the native
      // ffmpeg process doesn't keep running (and the mic indicator lit)
      // after the component that started it is gone; the resulting clip (if
      // any) just won't get the select/flash UX since nothing is listening.
      const curSid = useStore.getState().sessionId
      const py = (window as unknown as PywebviewVoBridge).pywebview?.api
      if (curSid && py?.vo_stop) py.vo_stop(curSid, startedAtRef.current, 0).catch(() => {})
      nativeRecordingRef.current = false
    }
    teardown()
  }, [])

  const start = async () => {
    setError(null)
    if (!sid) return
    // Flip to a "requesting" state synchronously so the very first click gives
    // immediate visual feedback. getUserMedia can block for seconds behind the
    // browser's mic-permission prompt; without this the button looks dead.
    setRequesting(true)

    // Native path FIRST: check for the pywebview bridge before falling back
    // to getUserMedia. In the packaged app, getUserMedia is unusable (see the
    // module doc comment above) regardless of whether `navigator.mediaDevices`
    // happens to exist, so the bridge's presence — not a getUserMedia probe —
    // is the right signal to branch on.
    const py = (window as unknown as PywebviewVoBridge).pywebview?.api
    if (py?.vo_start && py?.vo_stop) {
      try {
        const res = await py.vo_start(sid)
        if (!res?.ok) {
          setError(res?.error || 'Could not start native mic recording.')
          setRequesting(false)
          return
        }
        nativeRecordingRef.current = true
        startedAtRef.current = playhead
        setPlaying(false)
        setRecording(true)
        setRequesting(false)
        setElapsed(0)
        tickRef.current = window.setInterval(() => {
          setElapsed((e) => e + 0.1)
        }, 100) as unknown as number
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        setRequesting(false)
      }
      return
    }

    // `navigator.mediaDevices` is undefined in some native-webview contexts
    // (no mic entitlement declared in the packaged app's Info.plist, or the
    // OS webview simply doesn't expose media capture) — calling
    // `.getUserMedia` on it directly throws a raw, unhelpful TypeError:
    // "undefined is not an object (evaluating 'navigator.mediaDevices.
    // getUserMedia')". Fail with a message that actually explains what's
    // wrong instead of surfacing that verbatim.
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Microphone recording isn’t available in this window (no mic access in this build). Try the browser-dev mode instead of the packaged app, or check the app’s microphone permission in System Settings.')
      setRequesting(false)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const mime = (window as unknown as { MediaRecorder: typeof MediaRecorder })
        .MediaRecorder?.isTypeSupported?.('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm'
      const rec = new MediaRecorder(stream, { mimeType: mime })
      chunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data) }
      rec.onstop = async () => {
        teardown()                 // release mic + ticker
        setRecording(false)        // ← always return to idle, every path below
        const blob = new Blob(chunksRef.current, { type: mime })
        if (!sid) return
        if (blob.size < 256) { setError('Recording too short — nothing captured.'); return }
        setSubmitting(true)
        try {
          const res = await api.voRecord(sid, blob, startedAtRef.current, 0)
          await refresh()
          // Draw attention to the freshly-added voiceover clip: select it (so the
          // Properties panel + selection border show) and flash it on the timeline.
          const cid = (res as { clip_id?: string } | undefined)?.clip_id
          if (cid) {
            useStore.getState().setSelection(cid)
            useStore.getState().flashClip(cid)
          }
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e))
        } finally {
          setSubmitting(false)
        }
      }
      rec.onerror = () => {
        teardown()
        setRecording(false)
        setError('Recording failed.')
      }
      rec.start(250)
      recRef.current = rec
      startedAtRef.current = playhead
      setPlaying(false)  // pause playback while recording
      setRecording(true)
      setRequesting(false)
      setElapsed(0)
      tickRef.current = window.setInterval(() => {
        setElapsed((e) => e + 0.1)
      }, 100) as unknown as number
    } catch (e) {
      // Any start failure (mic denied, unsupported recorder, throw after the mic
      // was acquired) → fully reset to idle AND release the mic so its indicator
      // doesn't stay lit.
      teardown()
      setRecording(false)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRequesting(false)
    }
  }

  const stop = async () => {
    setRecording(false)
    if (tickRef.current) { window.clearInterval(tickRef.current); tickRef.current = null }

    if (nativeRecordingRef.current) {
      nativeRecordingRef.current = false
      const py = (window as unknown as PywebviewVoBridge).pywebview?.api
      if (!sid || !py?.vo_stop) return  // bridge vanished mid-recording — nothing more we can do
      setSubmitting(true)
      try {
        const res = await py.vo_stop(sid, startedAtRef.current, 0)
        if (!res?.ok) {
          setError(res?.error || 'Native recording failed.')
          return
        }
        await refresh()
        // Same post-record UX as the getUserMedia path: select + flash the
        // freshly-added voiceover clip so it's obvious something happened.
        if (res.clip_id) {
          useStore.getState().setSelection(res.clip_id)
          useStore.getState().flashClip(res.clip_id)
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setSubmitting(false)
      }
      return
    }

    const rec = recRef.current
    if (rec && rec.state !== 'inactive') {
      rec.stop()        // → onstop runs teardown + upload
    } else {
      teardown()        // already inactive: release the mic/ticker ourselves
    }
  }

  return (
    <div style={{ marginTop: 12 }}>
      {!recording ? (
        <button
          style={{ width: '100%', fontSize: 11 }}
          onClick={start}
          disabled={submitting || requesting || !sid}
          title="Record a voiceover from your mic at the current playhead"
        >
          {submitting ? '⌛ Encoding…'
            : requesting ? '🎤 Requesting mic access…'
            : '🎙 Record voiceover'}
        </button>
      ) : (
        <button
          style={{ width: '100%', fontSize: 11, background: 'var(--accent)', color: '#fff' }}
          onClick={stop}
        >
          ⏺ Recording ({elapsed.toFixed(1)}s) · click to stop
        </button>
      )}
      {error && (
        <div style={{ color: '#fbb', fontSize: 10, marginTop: 4 }}>{error}</div>
      )}
    </div>
  )
}
