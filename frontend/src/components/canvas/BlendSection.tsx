import './canvas.css'
import { Icon } from '../Icon'
import { BLENDS, blendLiveNote, blendOf } from '../../lib/canvasBlend/catalog'

// The Inspector's Blend control on an overlay (PiP) clip (wave E, lane F2):
// CapCut's 14 blend modes in its order, one `set_blend_mode` dispatch per
// pick. The export composites with the W3C formula (render/pip.py); the
// live preview asks the browser for the same `mix-blend-mode`
// (lib/pipBlendLayers). Where THIS browser's live blend is not the export's
// (measured: lib/canvasBlend LIVE_BLEND_FLAGS), a note under the menu says
// so — the export is unaffected.

type Send = (tool: string, args: Record<string, unknown>) => unknown

export function BlendSection({ clipId, clip, send }: { clipId: string; clip: unknown; send: Send }) {
  const mode = blendOf(clip)
  const op = (clip as { transform?: { opacity?: unknown } } | null)?.transform?.opacity
  const note = blendLiveNote(mode, typeof op === 'number' ? op : op == null ? 1 : null)
  return (
    <div className="blend-section">
      <label className="blend-row">
        <span className="blend-label"><Icon name="blendMode" /> Mode</span>
        <select aria-label="Blend mode" value={mode} data-blend={mode}
                onChange={(e) => { void send('set_blend_mode', { clip_id: clipId, mode: e.target.value }) }}>
          {BLENDS.map((b) => (
            <option key={b.id} value={b.id}>{b.label}</option>
          ))}
        </select>
      </label>
      {note && <p className="canvas-note" role="note" data-blend-note>{note}</p>}
    </div>
  )
}
