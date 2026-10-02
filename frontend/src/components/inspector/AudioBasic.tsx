// Audio › Basic and Voice changer (design §2c; brief §4 "Audio inspector"):
// Volume (dB), fade in / out, the five switches, and the translator row. The
// level, fades, mute, channel mode and volume keyframes are the clip's real
// controls (components/Properties' Audio section). The switches map onto the
// engine: Normalize loudness is the project's export target, Reduce noise and
// Isolate voice are AI jobs (opened with progress), Fill channel is the
// channel mode; Enhance voice is not in this build and says so.
import { useStore } from '../../store'
import { useLayoutStore } from '../../lib/layoutStore'
import { Properties } from '../Properties'
import { Toggle } from '../ui/Toggle'
import { Icon } from '../Icon'

export function AudioBasic({ clipId, kind }: { clipId?: string; kind: 'video' | 'audio' }) {
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const dispatch = useStore((s) => s.dispatch)
  const jumpToAi = useLayoutStore((s) => s.jumpToAi)
  const id = clipId ?? selection
  const clip = edl?.tracks.flatMap((t) => t.clips).find((c) => c.id === id) as unknown as { audio?: { channels?: string | null } } | undefined
  const lufs = edl?.canvas ? (edl.canvas as unknown as { loudness_lufs?: number | null }).loudness_lufs ?? null : null
  const filled = clip?.audio?.channels === 'mono'
  return (
    <div className="in-stack">
      <div className="in-pad in-legacy"><Properties bare sections={['Audio']} /></div>
      <div className="ui-hairline in-mx" />
      <div className="in-pad in-stack">
        <div className="ui-row-between"><span>Normalize loudness</span>
          <Toggle on={lufs != null} label="Normalize loudness" title={lufs != null ? `Exports master to ${lufs} LUFS (project-wide; Undo reverts)` : 'Master every export to −14 LUFS (project-wide; Undo reverts)'}
                  onChange={(on) => void dispatch('set_loudness_target', { lufs: on ? -14 : null })} /></div>
        <div className="ui-row-between"><span>Enhance voice</span>
          <Toggle on={false} label="Enhance voice" disabled title="Not available in this build: no speech-enhancement model is shipped. Reduce noise and the voice effects are below." onChange={() => undefined} /></div>
        <div className="ui-row-between"><span>Reduce noise</span>
          <button type="button" className="ui-small-btn" onClick={() => jumpToAi({ tool: 'noise_reduce', from: 'audio' })} title="Opens the tool with its options and progress — renders a cleaned copy">Apply…</button></div>
        <div className="ui-row-between"><span>Isolate voice</span>
          <button type="button" className="ui-small-btn" onClick={() => jumpToAi({ tool: 'vocal_isolate', from: 'audio' })} title="Opens the tool (demucs) — keeps the voice, drops the music">Apply…</button></div>
        <div className="ui-row-between"><span>Fill channel</span>
          <Toggle on={filled} label="Fill channel" title="Play a one-sided recording from both sides"
                  onChange={(on) => { if (id) void dispatch('set_property', { clip_id: id, path: 'audio.channels', value: on ? 'mono' : 'stereo' }) }} /></div>
      </div>
      <div className="ui-hairline in-mx" />
      <div className="in-pad">
        <div className="ui-row-between"><span>{kind === 'video' ? 'Video translator' : 'Audio translator'}</span>
          <button type="button" className="ui-small-btn" onClick={() => jumpToAi({ tool: 'translate_captions', from: 'captions' })} title="Translate this project's captions locally (MADLAD-400)"><Icon name="captions" />Translate…</button></div>
      </div>
    </div>
  )
}

export function VoiceChanger() {
  return <div className="in-pad in-legacy"><Properties bare sections={['Voice effects']} /></div>
}
