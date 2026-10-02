// AI avatars (design §2a; brief §4 "AI avatars S12"): the two-step flow —
// avatar, frame and background, then a voiceover — drawn exactly where the
// reference draws it. Avatar generation is not part of this build (there is
// no talking-head model in the engine), so the step is shown, every choice
// is disabled, Next is disabled, and the panel says why instead of
// pretending. The voiceover half IS real: Audio › Import › Record, and the
// Prompt bar's AI voiceover.
import { useState } from 'react'
import { PanelState } from './Catalogue'
import { Icon } from '../Icon'

const FRAMES = ['Medium', 'Close-up', 'Full'] as const

export function AiAvatarsTab({ sub }: { sub: string }) {
  const [frame, setFrame] = useState<typeof FRAMES[number]>('Medium')
  if (sub === 'Voiceover') {
    return <PanelState kind="unavailable" title="Pick an avatar first" body="The voiceover step follows an avatar. Avatars are not available in this build; record or generate a voiceover from Audio › Import instead." />
  }
  return (
    <div className="ab-stack ab-avatars" aria-disabled="true">
      <div className="ab-stepper"><span className="is-current">1 AI avatar</span><Icon name="chevronRight" /><span>2 Voiceover</span></div>
      <div className="ab-field">
        <span className="ui-label">Avatar</span>
        <div className="ui-tiles is-4" role="group" aria-label="Avatar">
          {[200, 30, 280, 150].map((h, i) => (
            <button key={h} type="button" className={`ui-tile${i === 0 ? ' is-selected' : ''}`} disabled title="Avatars are not available in this build">
              <span className="ui-tile-art" style={{ aspectRatio: '3 / 4', background: `linear-gradient(160deg, hsl(${h} 35% 30%), hsl(${h} 30% 14%))` }} />
            </button>
          ))}
        </div>
      </div>
      <div className="ab-field">
        <span className="ui-label">Frame</span>
        <div className="ab-chips" role="radiogroup" aria-label="Frame">
          {FRAMES.map((f) => <button key={f} type="button" role="radio" aria-checked={frame === f} className={`ui-chip${frame === f ? ' is-active' : ''}`} onClick={() => setFrame(f)}>{f}</button>)}
        </div>
      </div>
      <div className="ab-field">
        <span className="ui-label">Background</span>
        <div className="ab-chips" role="group" aria-label="Background">
          <span className="ab-swatch is-selected" style={{ background: 'var(--bg-1)' }} />
          <span className="ab-swatch" style={{ background: 'linear-gradient(135deg, #334, #556)' }} />
          <span className="ab-swatch" style={{ background: 'linear-gradient(135deg, #1a3a2a, #2a5a4a)' }} />
          <span className="ab-swatch" style={{ background: 'var(--bg-2)', display: 'grid', placeItems: 'center' }}><Icon name="image" /></span>
        </div>
      </div>
      <button type="button" className="ui-btn-primary ab-self-start" disabled title="Avatars are not available in this build">Next</button>
      <PanelState kind="unavailable" title="AI avatars are not available in this build"
                  body="A talking-head generator is not shipped. Everything else here works: import a clip, record or generate a voiceover under Audio, and add captions." />
    </div>
  )
}
