import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'

/**
 * Mic voiceover recorder. Uses the browser's MediaRecorder to capture an
 * Opus/WebM blob, posts it to /vo_record, which transcodes to AAC and drops
 * a clip on the vo track at the current playhead.
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
    teardown()
  }, [])

  const start = async () => {
    setError(null)
    if (!sid) return
    // Flip to a "requesting" state synchronously so the very first click gives
    // immediate visual feedback. getUserMedia can block for seconds behind the
    // browser's mic-permission prompt; without this the button looks dead.
    setRequesting(true)
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
          await api.voRecord(sid, blob, startedAtRef.current, 0)
          await refresh()
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

  const stop = () => {
    setRecording(false)
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
