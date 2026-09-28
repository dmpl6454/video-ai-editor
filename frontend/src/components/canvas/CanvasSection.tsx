import React from 'react'
import './canvas.css'
import { Icon } from '../Icon'
import type { IconName } from '../../lib/icons'
import { api } from '../../api'
import { errorMessage } from '../../store'
import {
  BLUR_DEFAULT, BLUR_LEVELS, IMAGE_EXTS, SWATCHES, canvasBgOf, type CanvasBg, type CanvasKind,
} from '../../lib/canvasBlend/catalog'

// The Inspector's Canvas section (wave E, lane F2): CapCut's Canvas for a
// letterboxed main-track clip — None (black bars), Colour, Blur or Image —
// with "Apply to all". Every pick is ONE `set_canvas_background` dispatch
// (one undo step); the table is lib/canvasBlend (edl/canvas_blend.py).

type Send = (tool: string, args: Record<string, unknown>) => unknown

export interface CanvasSectionProps {
  clipId: string
  /** The clip object (its `canvas_bg`, `fit`). */
  clip: unknown
  sessionId: string | null
  send: Send
}

const KINDS: Array<{ id: 'none' | CanvasKind; label: string; icon: IconName; hint: string }> = [
  { id: 'none', label: 'None', icon: 'canvasNone', hint: 'Black bars' },
  { id: 'color', label: 'Colour', icon: 'canvasColor', hint: 'A solid colour behind the video' },
  { id: 'blur', label: 'Blur', icon: 'canvasBlur', hint: 'A blurred copy of the clip behind it' },
  { id: 'image', label: 'Image', icon: 'canvasImage', hint: 'A picture behind the video' },
]

/** Roving focus inside a radiogroup: arrows move, like the Speed section. */
function onRadioKey(e: React.KeyboardEvent<HTMLDivElement>) {
  const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
  if (!keys.includes(e.key)) return
  e.preventDefault()
  e.stopPropagation()
  const btns = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]'))
  const at = btns.findIndex((b) => b === document.activeElement)
  const n = btns.length
  const to = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1
    : (at + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + n) % n
  btns[to]?.focus()
}

function baseName(p: string | null | undefined): string {
  return (p ?? '').replace(/\\/g, '/').split('/').pop() ?? ''
}

export function CanvasSection({ clipId, clip, sessionId, send }: CanvasSectionProps) {
  const bg: CanvasBg | null = canvasBgOf(clip)
  const cover = (clip as { fit?: string } | null)?.fit === 'cover'
  const kind: 'none' | CanvasKind = bg ? bg.type : 'none'
  // The tab the user opened (Image shows its picker before a picture exists),
  // valid only while the clip's stored background is the one it was opened
  // over. Review RE: an Undo (or an edit from the Prompt bar or chat) left the
  // section on "Blur" with its strength row while the clip had none.
  const stored = `${clipId}|${kind}|${bg?.color ?? ''}|${bg?.blur ?? ''}|${bg?.image ?? ''}`
  const [opened, setOpened] = React.useState<'none' | CanvasKind | null>(null)
  // reset when the stored background changes (React's "adjust state on a
  // prop change" pattern: an Undo back to the value it was opened over
  // must reset it too, so a compare with that value is not enough)
  const [seen, setSeen] = React.useState(stored)
  if (seen !== stored) {
    setSeen(stored)
    setOpened(null)
  }
  const shown = opened ?? kind
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const file = React.useRef<HTMLInputElement>(null)
  const color = bg?.type === 'color' ? (bg.color ?? '#000000').toUpperCase() : null
  const level = bg?.type === 'blur' ? bg.blur ?? BLUR_DEFAULT : null

  const set = (args: Record<string, unknown>) => {
    setErr(null)
    void send('set_canvas_background', { clip_id: clipId, ...args })
  }
  const pickKind = (k: 'none' | CanvasKind) => {
    setOpened(k)
    if (k === 'none') set({ type: 'none' })
    else if (k === 'color') set({ type: 'color', color: color ?? '#000000' })
    else if (k === 'blur') set({ type: 'blur', blur: level ?? BLUR_DEFAULT })
    else if (bg?.type === 'image' && bg.image) set({ type: 'image', image: bg.image })
    else file.current?.click()
  }
  const upload = async (f: File) => {
    if (!sessionId) return
    setBusy(true)
    setErr(null)
    try {
      const body = await api.canvasBgUpload(sessionId, f)
      set({ type: 'image', image: body.src })
    } catch (e) {
      setErr(errorMessage(e))           // the server's reason ("… could not be read as a picture")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="canvas-section" data-canvas-kind={kind}>
      <div className="canvas-kinds" role="radiogroup" aria-label="Canvas background" onKeyDown={onRadioKey}>
        {KINDS.map((k) => (
          <button key={k.id} type="button" role="radio" aria-checked={shown === k.id}
                  tabIndex={shown === k.id ? 0 : -1} title={k.hint} data-kind={k.id}
                  onClick={() => pickKind(k.id)}>
            <Icon name={k.icon} /> <span>{k.label}</span>
          </button>
        ))}
      </div>

      {shown === 'color' && (
        <div className="canvas-colors">
          <div className="canvas-swatches" role="radiogroup" aria-label="Background colour" onKeyDown={onRadioKey}>
            {SWATCHES.map((hex) => (
              <button key={hex} type="button" role="radio" className="canvas-swatch"
                      aria-checked={color === hex} tabIndex={color === hex || (!color && hex === SWATCHES[0]) ? 0 : -1}
                      aria-label={`Colour ${hex}`} title={hex} style={{ background: hex }}
                      onClick={() => set({ type: 'color', color: hex })} />
            ))}
          </div>
          <label className="canvas-custom">
            <span>Custom</span>
            <input type="color" aria-label="Custom background colour" value={(color ?? '#000000').toLowerCase()}
                   onChange={(e) => {
                     const v = e.target.value.toUpperCase()
                     if (v !== color) set({ type: 'color', color: v })
                   }} />
            <span className="canvas-hex">{color ?? '#000000'}</span>
          </label>
        </div>
      )}

      {shown === 'blur' && (
        <div className="canvas-blurs" role="radiogroup" aria-label="Blur strength" onKeyDown={onRadioKey}>
          {BLUR_LEVELS.map((b) => (
            <button key={b.level} type="button" role="radio" className="canvas-blur"
                    aria-checked={level === b.level} tabIndex={level === b.level || (!level && b.level === BLUR_DEFAULT) ? 0 : -1}
                    onClick={() => set({ type: 'blur', blur: b.level })} data-level={b.level}>
              <span className="canvas-blur-chip" aria-hidden="true"
                    style={{ '--canvas-blur-px': `${1 + b.level * 1.2}px` } as React.CSSProperties} />
              <span>{b.label}</span>
            </button>
          ))}
        </div>
      )}

      {shown === 'image' && (
        <div className="canvas-image">
          <button type="button" className="canvas-tool" disabled={busy || !sessionId}
                  onClick={() => file.current?.click()}>
            <Icon name="upload" /> {busy ? 'Adding…' : bg?.type === 'image' ? 'Change picture…' : 'Choose picture…'}
          </button>
          {bg?.type === 'image' && bg.image && (
            <span className="canvas-image-name" title={bg.image}>{baseName(bg.image)}</span>
          )}
        </div>
      )}
      <input ref={file} type="file" hidden accept={IMAGE_EXTS.join(',')} aria-hidden="true" tabIndex={-1}
             onChange={(e) => {
               const f = e.target.files?.[0]
               e.target.value = ''
               if (f) void upload(f)
             }} />

      <div className="canvas-foot">
        <button type="button" className="canvas-tool" data-action="apply-all"
                aria-label="Apply this canvas background to all main-track clips"
                title="Apply this background to every clip on the main track (one undo step)"
                onClick={() => { setErr(null); void send('set_canvas_background', { clip_id: clipId, all: true }) }}>
          <Icon name="applyAll" /> Apply to all
        </button>
      </div>
      {cover && bg && (
        <p className="canvas-note">This clip fills the frame (Fill frame is on), so its background shows only
          once it is letterboxed.</p>
      )}
      {err && <p className="canvas-note canvas-error" role="alert">{err}</p>}
    </div>
  )
}
