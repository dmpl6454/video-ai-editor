import React from 'react'
import './voice.css'
import { Icon } from '../Icon'
import type { IconName } from '../../lib/icons'
import { api } from '../../api'
import { useSliderCommit } from '../../lib/useSliderCommit'
import { useVoiceCatalog } from '../../lib/voice/voiceCatalog'
import { errorMessage } from '../../store'

// The Inspector's Voice effects section, CapCut's voice changer (wave E,
// F3): a grid of the presets (None first), a Strength slider for the chosen
// one, and Preview — a few seconds of THIS clip's sound from the playhead,
// through the chosen effect, rendered by the server with the export's own
// chain (POST /voice/preview; nothing is committed). The grid is one radio
// group: arrows move, Enter / Space choose; every choice is one undo step
// (`set_voice_effect`).
//
// Presets come from the server (lib/voice/voiceCatalog → edl/voice_effects
// .py), the one table set_voice_effect, the agent and the Prompt bar read.

type Send = (tool: string, args: Record<string, unknown>) => unknown

export interface VoiceEffectsSectionProps {
  sessionId: string | null
  clipId: string
  /** The clip's stored `audio.voice_effect` / `audio.voice_intensity`. */
  effect: string | null | undefined
  intensity: number | undefined
  /** Clip-local seconds of the playhead (where the preview starts). */
  localT: number
  /** A freeze frame has no sound: the section explains instead. */
  freeze: boolean
  /** The clip's source path (keys the "has it any sound?" answer). */
  src?: string
  send: Send
}

const PREVIEW_SECONDS = 4

/** Whether each (session, clip, source) has sound — asked once. */
const SOUND = new Map<string, boolean>()

/** Review RE: the section was offered on a clip whose file has no sound
 *  stream; its presets could only play silence. null while unknown. */
function useHasSound(sid: string | null, clipId: string, src: string | undefined): boolean | null {
  const key = `${sid}|${clipId}|${src ?? ''}`
  const [known, setKnown] = React.useState<{ key: string; value: boolean } | null>(null)
  React.useEffect(() => {
    if (!sid || SOUND.has(key)) return
    let live = true
    api.voiceSound(sid, clipId).then((r) => {
      SOUND.set(key, r.has_audio)
      if (live) setKnown({ key, value: r.has_audio })
    }).catch(() => { /* unknown: keep the section usable; dispatch refuses a silent clip */ })
    return () => { live = false }
  }, [sid, clipId, key])
  if (SOUND.has(key)) return SOUND.get(key) ?? null
  return known && known.key === key ? known.value : null
}

type Audition = { state: 'idle' } | { state: 'loading' } | { state: 'playing' } | { state: 'error'; message: string }

export function VoiceEffectsSection(p: VoiceEffectsSectionProps) {
  const cat = useVoiceCatalog()
  const sound = useHasSound(p.sessionId, p.clipId, p.src)
  const active = p.effect ?? 'none'
  const stored = typeof p.intensity === 'number' ? p.intensity : 1
  const [strength, setStrength] = React.useState(Math.round(stored * 100))
  const dragging = React.useRef(false)
  React.useEffect(() => { if (!dragging.current) setStrength(Math.round(stored * 100)) }, [stored])
  const h = useSliderCommit(Math.round(stored * 100), (v) => {
    dragging.current = false
    if (active !== 'none') void p.send('set_voice_effect', { clip_id: p.clipId, effect: active, intensity: v / 100 })
  })

  // ---- the audition
  const [aud, setAud] = React.useState<Audition>({ state: 'idle' })
  const player = React.useRef<{ el: HTMLAudioElement; url: string } | null>(null)
  const abort = React.useRef<AbortController | null>(null)
  const stop = React.useCallback(() => {
    abort.current?.abort()
    abort.current = null
    const cur = player.current
    player.current = null
    if (cur) {
      cur.el.pause()
      URL.revokeObjectURL(cur.url)
    }
    setAud((a) => (a.state === 'error' ? a : { state: 'idle' }))
  }, [])
  // a new clip or effect ends the old audition
  React.useEffect(() => stop, [p.clipId, active, stop])

  const preview = async () => {
    if (aud.state === 'loading' || aud.state === 'playing') { stop(); return }
    if (!p.sessionId || active === 'none') return
    const ctl = new AbortController()
    abort.current = ctl
    setAud({ state: 'loading' })
    try {
      const blob = await api.voicePreview(p.sessionId, {
        clip_id: p.clipId, effect: active, intensity: strength / 100,
        at: Math.max(0, p.localT), seconds: PREVIEW_SECONDS,
      }, ctl.signal)
      if (ctl.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const el = new Audio(url)
      player.current = { el, url }
      el.onended = () => stop()
      await el.play()
      setAud({ state: 'playing' })
    } catch (e) {
      if (ctl.signal.aborted) return
      player.current = null
      // review RE: the raw '400 Bad Request: {"error":{…}}' was shown
      setAud({ state: 'error', message: errorMessage(e) })
    }
  }

  if (p.freeze) {
    return <p className="voice-note"><Icon name="freeze" /> A freeze frame has no sound — nothing to change.</p>
  }
  if (sound === false && active === 'none') {
    return <p className="voice-note" data-voice="silent"><Icon name="voiceNone" /> This clip has no sound — its file
      carries no audio, so there is nothing for a voice effect to change.</p>
  }

  const presets = cat.status === 'ready' ? cat.table.presets : []
  const options: Array<{ id: string; label: string; hint: string; icon: IconName }> = [
    { id: 'none', label: 'None', hint: 'The clip\'s own voice, no effect', icon: 'voiceNone' },
    ...presets.map((q) => ({ id: q.id, label: q.label, hint: q.hint, icon: q.icon as IconName })),
  ]
  const choose = (id: string) => {
    if (id === active) return
    void p.send('set_voice_effect', { clip_id: p.clipId, effect: id })
  }
  const onRadioKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
    if (!keys.includes(e.key)) return
    e.preventDefault()
    e.stopPropagation()
    const btns = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]'))
    const at = btns.findIndex((b) => b === document.activeElement)
    const cols = 3                               // voice.css .voice-grid columns
    const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowDown' ? cols : e.key === 'ArrowUp' ? -cols : 0
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? btns.length - 1 : Math.min(btns.length - 1, Math.max(0, at + d))
    btns[next]?.focus()
  }
  const label = options.find((o) => o.id === active)?.label ?? active

  return (
    <div className="voice-section">
      {cat.status === 'loading' && <p className="voice-note">Loading voice effects…</p>}
      {cat.status === 'error' && (
        <p className="voice-note voice-error" role="alert">Voice effects are unavailable: the engine did not answer ({cat.message}).</p>
      )}
      {cat.status === 'ready' && (
        <div className="voice-grid" role="radiogroup" aria-label="Voice effect" onKeyDown={onRadioKey}>
          {options.map((o) => (
            <button key={o.id} type="button" role="radio" className="voice-tile" data-effect={o.id}
                    aria-checked={active === o.id} tabIndex={active === o.id ? 0 : -1}
                    title={o.hint} onClick={() => choose(o.id)}>
              <Icon name={o.icon} />
              <span>{o.label}</span>
            </button>
          ))}
        </div>
      )}
      {active !== 'none' && (
        <div className="row slider-row voice-strength" style={{ alignItems: 'center', gap: 6 }}>
          <span className="slider-name" aria-hidden="true">Strength</span>
          <input type="range" min={0} max={100} step={5} value={strength}
            aria-label={`${label} strength`} aria-valuetext={`${strength} %`}
            onChange={(e) => {
              const v = Number(e.target.value)
              dragging.current = true
              setStrength(v)
              h.change(v)
            }}
            onPointerUp={(e) => { dragging.current = false; h.onPointerUp(e) }}
            onPointerCancel={() => { dragging.current = false }}
            onKeyUp={h.onKeyUp}
            onBlur={() => { dragging.current = false; h.onBlur() }}
            style={{ flex: 1 }} />
          <span className="slider-value">{strength} %</span>
        </div>
      )}
      <div className="voice-preview-row">
        <button type="button" className="voice-preview" disabled={active === 'none' || !p.sessionId}
                aria-label={aud.state === 'playing' || aud.state === 'loading' ? 'Stop the voice preview'
                  : `Preview ${label} on this clip`}
                title={active === 'none' ? 'Choose a voice effect to preview it'
                  : `Hear ${PREVIEW_SECONDS} seconds of this clip from the playhead through ${label}`}
                onClick={() => void preview()}>
          <Icon name={aud.state === 'playing' ? 'stop' : aud.state === 'loading' ? 'loading' : 'play'}
                className={aud.state === 'loading' ? 'icon-spin' : undefined} />
          {aud.state === 'playing' || aud.state === 'loading' ? 'Stop' : 'Preview'}
        </button>
        <span className="voice-status" aria-live="polite">
          {aud.state === 'loading' ? 'Rendering…' : aud.state === 'playing' ? `Playing ${label}` : ''}
          {aud.state === 'error' ? `Preview failed: ${aud.message}` : ''}
        </span>
      </div>
    </div>
  )
}
