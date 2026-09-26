// The inspector's Channels control (QA-122): stereo / left to both / right to
// both / mono mix, committed as `set_property audio.channels` (one undo step),
// plus a notice when the source's sound is on one side only — the case that
// used to export one-sided with nothing in the editor saying so.

import { useEffect, useId, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { CHANNEL_MODES, channelMode, oneSidedSource, type ChannelMode } from '../lib/audioChannels'

export function ChannelModeField({ clipId, src, audio }: {
  clipId: string
  src: string
  audio: { channels?: string | null } | undefined
}) {
  const sessionId = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const id = useId()
  const mode = channelMode(audio)
  // Which side the source's sound is on — from the waveform peaks (served from
  // the server's cache: no probe, no decode after the timeline's first fetch).
  const [side, setSide] = useState<{ src: string; side: 'left' | 'right' | null } | null>(null)
  useEffect(() => {
    if (!sessionId || !src) return
    let live = true
    api.waveform(sessionId, src, 50)
      .then((w) => { if (live) setSide({ src, side: oneSidedSource(w) }) })
      .catch(() => { if (live) setSide({ src, side: null }) })
    return () => { live = false }
  }, [sessionId, src])
  const oneSided = side?.src === src ? side.side : null
  const set = (value: ChannelMode) => { void dispatch('set_property', { clip_id: clipId, path: 'audio.channels', value }) }
  const current = CHANNEL_MODES.find((m) => m.value === mode)

  return (
    <div className="field">
      <label htmlFor={`${id}-ch`}>Channels</label>
      <select id={`${id}-ch`} value={mode} title={current?.title}
              onChange={(e) => set(e.target.value as ChannelMode)}>
        {CHANNEL_MODES.map((m) => <option key={m.value} value={m.value} title={m.title}>{m.label}</option>)}
      </select>
      {oneSided && mode === 'stereo' && (
        <div className="channel-notice" role="status">
          <span>This sound is on the {oneSided} channel only — it will play from one side.</span>
          <button type="button" onClick={() => set(oneSided)}>Fill both sides</button>
        </div>
      )}
    </div>
  )
}
