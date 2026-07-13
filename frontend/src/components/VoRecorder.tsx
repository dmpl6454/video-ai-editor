import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'

// Narrow shape of the bridge desktop.py's `_Api` exposes over pywebview's
// js_api — only the two methods this file calls, not the whole class.
interface PywebviewVoBridge {
  pywebview?: {
    api?: {
      // `unsupported: true` means the native bridge exists (pywebview always
      // exposes the method) but this platform's vo_start deliberately refuses
      // to run — currently desktop.py's non-mac branch (Windows/Linux, where
      // the avfoundation capture path doesn't exist but WebView2's
      // getUserMedia works fine). Distinguishing this from a real failure
      // (mic denied, ffmpeg missing, etc.) lets the caller fall through to
      // getUserMedia instead of dead-ending on an error the user can't act on.
      vo_start?: (sessionId: string) => Promise<{ ok: boolean; error?: string; unsupported?: boolean }>
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
 *  - Packaged app on Windows: the pywebview bridge methods exist (same js_api
 *    class), but desktop.py's `vo_start` immediately returns
 *    `{ok: false, unsupported: true}` on any non-mac platform — avfoundation
 *    capture is macOS-only. WebView2 (the Windows pywebview backend) DOES
 *    implement getUserMedia correctly, so `unsupported: true` must fall
 *    through to the getUserMedia path below rather than surface an error;
 *    only a "real" failure (mic denied, ffmpeg missing, TCC denial on mac)
 *    should stop and show an error to the user.
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
  const fileInputRef = useRef<HTMLInputElement | null>(null)

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
        if (res?.ok) {
          nativeRecordingRef.current = true
          startedAtRef.current = playhead
          setPlaying(false)
          setRecording(true)
          setRequesting(false)
          setElapsed(0)
          tickRef.current = window.setInterval(() => {
            setElapsed((e) => e + 0.1)
          }, 100) as unknown as number
          return
        }
        if (!res?.unsupported) {
          // A real failure on a platform where the native bridge is meant to
          // work (mac: mic denied, ffmpeg missing, etc.) — surface it rather
          // than silently falling through, since getUserMedia is unusable in
          // this window anyway (see module doc comment) and would just fail
          // again with a less useful message.
          setError(res?.error || 'Could not start native mic recording.')
          setRequesting(false)
          return
        }
        // `unsupported: true` — this platform's native bridge deliberately
        // refuses (e.g. Windows: avfoundation is mac-only). Fall through to
        // the getUserMedia path below, which WebView2 supports natively.
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        setRequesting(false)
        return
      }
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

  // Guaranteed-working fallback: if neither the native bridge nor
  // getUserMedia can capture a mic in this window (TCC denial in the
  // packaged app, browser permission blocked, etc.), let the user attach
  // any existing audio file as the voiceover clip instead. This reuses the
  // exact same /vo_record endpoint + dispatch/commit path as a live
  // recording — from the backend's perspective an imported file and a
  // MediaRecorder blob are indistinguishable (`voRecord` just posts a
  // Blob/File either way) — so it's exercised, not a separate code path.
  const importFile = async (file: File) => {
    setError(null)
    if (!sid) return
    setSubmitting(true)
    try {
      const res = await api.voRecord(sid, file, playhead, 0)
      await refresh()
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

  const onFileChosen = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''  // allow re-selecting the same file next time
    if (file) void importFile(file)
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
      {/* Guaranteed-working fallback: import an existing audio file as the
          voiceover clip when live mic capture isn't available (native-bridge
          TCC denial in the packaged app, browser mic permission blocked,
          etc.) — see the module doc comment and importFile() above. Kept
          visible at all times, not just after an error, so it's discoverable
          rather than something the user has to fail first to find. */}
      <input
        ref={fileInputRef}
        type="file"
        accept="audio/*"
        style={{ display: 'none' }}
        onChange={onFileChosen}
      />
      <button
        style={{ width: '100%', fontSize: 10, marginTop: 4, opacity: 0.8 }}
        onClick={() => fileInputRef.current?.click()}
        disabled={submitting || recording || !sid}
        title="Import an existing audio file as the voiceover track (fallback if mic recording isn't available)"
      >
        📁 Import audio file as voiceover
      </button>
      {error && (
        <div style={{ color: '#fbb', fontSize: 10, marginTop: 4 }}>{error}</div>
      )}
    </div>
  )
}
