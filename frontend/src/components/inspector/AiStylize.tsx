// AI stylize (design §2c; brief §3 "AI stylize S27"): a prompt, Showcase /
// Random / Generate, and a Style grid. Generate runs the sentence through
// the Prompt bar's own planner (lib/promptStore.run) — the real "warm 35 mm
// film" path: a look recipe, previewed, verified and committed as one undo
// step, with the source clip intact. The Style tiles are the bundled LUT
// looks, applied to this clip. A generative restyle (a diffusion model) is
// not in this build and the note says so.
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { useStore } from '../../store'
import { usePromptStore, isBusy } from '../../lib/promptStore'
import { lutApplyArgs } from '../../lib/lutActions'
import { lutDisplayName } from '../../lib/lutName'
import { Checkbox } from '../ui/Checkbox'
import { Icon } from '../Icon'
import { SkeletonGrid } from '../ui/Skeleton'

const SHOWCASE = ['warm 35mm film, soft grain', 'cool teal and orange, punchy contrast', 'faded vintage, light vignette', 'clean and bright, a little more saturation', 'black and white, high contrast']

export function AiStylize({ clipId }: { clipId: string }) {
  const sid = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const status = usePromptStore((s) => s.status)
  const run = usePromptStore((s) => s.run)
  const [text, setText] = useState('')
  const [luts, setLuts] = useState<string[] | null>(null)
  useEffect(() => {
    if (!sid) return
    let live = true
    api.dispatch<{ luts?: string[] }>(sid, 'list_luts', {}).then((r) => { if (live) setLuts(r.result?.luts ?? []) }).catch(() => { if (live) setLuts([]) })
    return () => { live = false }
  }, [sid])
  const busy = isBusy(status)
  const generate = () => {
    const look = text.trim()
    if (!look) return
    void run(`make the selected clip look like ${look}`)
  }
  return (
    <div className="in-pad in-stack">
      <div className="ui-row-between"><span className="ui-heading">AI effects</span><Checkbox checked={false} onChange={() => undefined} disabled title="A generative restyle is not in this build" /></div>
      <textarea className="ui-textarea" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="Describe the look: ‘warm 35mm film, soft grain’" aria-label="Describe the look" data-keymap-ignore />
      <div className="in-two-actions">
        <button type="button" className="ui-small-btn" onClick={() => setText(SHOWCASE[0])}>Showcase</button>
        <button type="button" className="ui-small-btn" onClick={() => setText(SHOWCASE[Math.floor(Math.random() * SHOWCASE.length)])}><Icon name="shuffle" />Random</button>
        <span className="ed-grow" />
        <button type="button" className="ui-small-btn is-primary" disabled={!text.trim() || busy} onClick={generate}
                title="Runs the description through the Prompt bar: a verified look, one undo step">{busy ? 'Working…' : 'Generate'}</button>
      </div>
      <span className="ui-heading">Style</span>
      {luts === null ? <SkeletonGrid count={8} /> : (
        <div className="ui-tiles is-4" role="group" aria-label="Style">
          {luts.map((n, i) => (
            <button key={n} type="button" className="ui-tile" title={`Apply the ${lutDisplayName(n)} look`}
                    onClick={() => void dispatch('apply_lut', lutApplyArgs({ src: n, intensity: 1, clipId }))}>
              <span className="ui-tile-art" style={{ background: `linear-gradient(135deg, hsl(${(i * 47) % 360} 45% 32%), hsl(${(i * 47 + 60) % 360} 40% 14%))` }} />
              <span className="ui-tile-label">{lutDisplayName(n)}</span>
            </button>
          ))}
        </div>
      )}
      <span className="ui-faint" style={{ lineHeight: 1.4 }}>
        Generate plans a colour look from your words and applies it as an edit — the source clip stays intact and Undo removes it.
        A generative restyle (new pixels from a diffusion model) is not available in this build.
      </span>
    </div>
  )
}
