// Caption style — the captions TRACK's look and position, for every cue at
// once (QA-075). It used to exist only as the AI tab's "Captions from
// transcript" form, which re-lays every cue; the CC button offered language
// and speed, and a selected cue's inspector said "controlled by the captions
// style" with no control. Opened from the CC ▾ menu and from a caption cue's
// inspector; every change is one `set_caption_style` op (one undo step) and
// both renderers draw it (rule 7 of lib/textLayout + captionAnchorY).

import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { Dialog } from './Dialog'
import { ColorField } from './ColorField'
import { CAPTION_PRESETS, captionLookOf, type CaptionLook } from '../lib/captionStyle'
import { registerCaptionStyleOpener } from '../lib/captionStyleOpen'


const FONTS: [string, string][] = [
  ['', 'Caption default (Inter Black)'], ['Inter-Black', 'Inter Black'], ['Inter-Bold', 'Inter Bold'],
  ['Anton-Regular', 'Anton'], ['BebasNeue-Regular', 'Bebas Neue'], ['Montserrat-Bold', 'Montserrat Bold'],
]

export function CaptionStylePanel() {
  const [open, setOpen] = useState(false)
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const returnRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    registerCaptionStyleOpener(() => { returnRef.current = document.activeElement as HTMLElement | null; setOpen(true) })
    return () => registerCaptionStyleOpener(null)
  }, [])

  const cap = edl?.tracks.find((t) => t.id === 'captions') as
    ({ clips: unknown[]; config?: { position?: string; look?: CaptionLook | null } | null } | undefined)
  const cues = cap?.clips.length ?? 0
  const position = cap?.config?.position ?? 'bottom'
  const look = captionLookOf(cap?.config?.look)
  const set = (args: Record<string, unknown>) => void dispatch('set_caption_style', args)

  return (
    <Dialog open={open} title="Caption style" labelId="caption-style-title" triggerRef={returnRef}
            onClose={() => setOpen(false)} className="caption-style"
            footer={<span className="export-dialog-estimate">
              {cues ? `Applies to all ${cues} caption${cues === 1 ? '' : 's'} · Undo reverts each change` : 'Add captions first (CC Captions)'}
            </span>}>
      {!cues ? (
        <p className="dialog-text">There are no captions on the timeline yet.</p>
      ) : (
        <div className="export-dialog-grid">
          <span className="export-dialog-label" id="cs-pos">Position</span>
          <div className="export-dialog-seg" role="radiogroup" aria-labelledby="cs-pos">
            {(['top', 'center', 'bottom'] as const).map((p) => (
              <button key={p} type="button" role="radio" aria-checked={position === p}
                      onClick={() => { if (p !== position) set({ position: p }) }}>
                {p[0].toUpperCase() + p.slice(1)}
              </button>
            ))}
          </div>

          <span className="export-dialog-label" id="cs-look">Look</span>
          <div className="caption-presets" role="radiogroup" aria-labelledby="cs-look">
            {CAPTION_PRESETS.map((p) => (
              <button key={p.id} type="button" role="radio" aria-checked={p.matches(look)}
                      className="caption-preset" onClick={() => set(p.args)} title={p.hint}>
                <span className="caption-preset-sample" style={p.sample}>Aa</span>
                {p.label}
              </button>
            ))}
          </div>

          <label htmlFor="cs-font">Font</label>
          <select id="cs-font" value={look.font ?? ''} onChange={(e) => set({ font: e.target.value || null })}>
            {FONTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>

          <label htmlFor="cs-color">Text colour</label>
          <div id="cs-color"><ColorField ariaLabel="Caption text colour" value={(look.color ?? '#FFFFFF').slice(0, 7).toLowerCase()}
                                         onCommit={(v) => set({ color: v })} /></div>

          <span className="export-dialog-label">Box</span>
          <div className="caption-box-row">
            <label><input type="checkbox" checked={!!look.background}
                          onChange={(e) => set({ background: e.target.checked ? '#000000B3' : null })} /> Box behind each caption</label>
            {look.background && (
              <ColorField ariaLabel="Caption box colour" value={look.background.slice(0, 7).toLowerCase()}
                          onCommit={(v) => set({ background: `${v}${look.background!.slice(7) || 'B3'}` })} />
            )}
          </div>

          <label htmlFor="cs-case">Letter case</label>
          <select id="cs-case" value={look.upper ? 'on' : ''}
                  onChange={(e) => set({ upper: e.target.value === 'on' ? true : null })}>
            <option value="">As transcribed</option>
            <option value="on">ALL CAPS</option>
          </select>
        </div>
      )}
    </Dialog>
  )
}
