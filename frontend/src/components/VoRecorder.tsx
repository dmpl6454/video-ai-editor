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
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const recRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const startedAtRef = useRef<number>(0)
  const tickRef = useRef<number | null>(null)

  useEffect(() => () => {
    // cleanup on unmount
    if (tickRef.current) window.clearInterval(tickRef.current)
    if (recRef.current && recRef.current.state !== 'inactive') {
      recRef.current.stop()
    }
  }, [])

  const start = async () => {
    setError(null)
    if (!sid) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mime = (window as unknown as { MediaRecorder: typeof MediaRecorder })
        .MediaRecorder?.isTypeSupported?.('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm'
      const rec = new MediaRecorder(stream, { mimeType: mime })
      chunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data) }
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        if (tickRef.current) window.clearInterval(tickRef.current)
        const blob = new Blob(chunksRef.current, { type: mime })
        if (!sid || blob.size < 256) return
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
      rec.start(250)
      recRef.current = rec
      startedAtRef.current = playhead
      setPlaying(false)  // pause playback while recording
      setRecording(true)
      setElapsed(0)
      tickRef.current = window.setInterval(() => {
        setElapsed((e) => e + 0.1)
      }, 100) as unknown as number
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const stop = () => {
    setRecording(false)
    if (recRef.current && recRef.current.state !== 'inactive') {
      recRef.current.stop()
    }
  }

  return (
    <div style={{ marginTop: 12 }}>
      {!recording ? (
        <button
          style={{ width: '100%', fontSize: 11 }}
          onClick={start}
          disabled={submitting || !sid}
          title="Record a voiceover from your mic at the current playhead"
        >
          {submitting ? '⌛ Encoding…' : '🎙 Record voiceover'}
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
