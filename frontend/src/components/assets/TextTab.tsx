// Text (design §2a; brief §2 table): Add text (the "Default text" tile), the
// text styles as Text effects, the four presets as Text templates, your text
// clips under Yours, Auto captions (the same form as the Captions tab), Local
// captions (an .srt / .vtt import). AI packaging is not part of this build.
import { useRef, useState } from 'react'
import { api } from '../../api'
import { errorMessage, useStore } from '../../store'
import { toast } from '../../toast'
import { isTextClip } from '../../types'
import { selectNewClip } from '../../lib/newClip'
import { TEXT_STYLE_PRESETS } from '../../lib/textStyles'
import { TEXT_TEMPLATES, insertDefaultText, insertTextStyle, insertTextTemplate, templateEnabled, type TextInsertDeps } from '../../lib/textPresets'
import { Icon } from '../Icon'
import { AutoCaptionsForm } from './AutoCaptionsForm'
import { PanelState, Tile, Unavailable } from './Catalogue'

const deps: TextInsertDeps = {
  state: () => useStore.getState(),
  dispatch: (tool, args) => useStore.getState().dispatch(tool, args),
  select: selectNewClip,
}

export function TextTab({ sub }: { sub: string }) {
  const [fieldText, setFieldText] = useState('')
  switch (sub) {
    case 'Add text':
      return (
        <div className="ab-stack" data-text-presets>
          {/* `[data-text-presets] > button` is the ⌥T keymap target (keymap/uiTargets). */}
          <button type="button" className="ab-default-text" onClick={() => { void insertDefaultText(deps) }} title="Add a text overlay at the playhead — edit it in the inspector">
            <span>Default text</span>
          </button>
          <label className="ab-field">
            <span className="ui-label">Text, #hashtag or @handle</span>
            <input className="ui-input" value={fieldText} onChange={(e) => setFieldText(e.target.value)} placeholder="e.g. fyp or @myhandle" />
          </label>
          <span className="ui-faint">Styles and templates use this text. Text effects and Text templates are on the left.</span>
        </div>
      )
    case 'Text effects':
      return (
        <div className="ab-stack">
          <label className="ab-field">
            <span className="ui-label">Text</span>
            <input className="ui-input" value={fieldText} onChange={(e) => setFieldText(e.target.value)} placeholder="The words (blank = the style's own)" />
          </label>
          <div className="ui-tiles" role="group" aria-label="Text styles">
            {TEXT_STYLE_PRESETS.map((p) => (
              <Tile key={p.id} label={p.label} title={`Add “${p.label}” text at the playhead`} onClick={() => { void insertTextStyle(deps, p, fieldText) }}>
                <span className="ab-text-sample" style={{ color: p.sample.color, background: p.sample.background ?? 'transparent', fontFamily: p.sample.fontFamily, fontWeight: p.sample.fontWeight ?? 400 }}>Aa</span>
              </Tile>
            ))}
          </div>
        </div>
      )
    case 'Text templates':
      return (
        <div className="ab-stack">
          <label className="ab-field">
            <span className="ui-label">Text, #hashtag or @handle</span>
            <input className="ui-input" value={fieldText} onChange={(e) => setFieldText(e.target.value)} placeholder="e.g. fyp or @myhandle" />
          </label>
          <div className="ui-tiles" role="group" aria-label="Text templates">
            {TEXT_TEMPLATES.map((t) => (
              <Tile key={t.name} label={t.label} title={t.title} disabled={!templateEnabled(t, fieldText)}
                    onClick={() => { void insertTextTemplate(deps, t.name, fieldText) }} aspect="16 / 9">
                <span className="ab-text-sample">{t.label}</span>
              </Tile>
            ))}
          </div>
        </div>
      )
    case 'Yours': return <YourTexts />
    case 'Auto captions': return <AutoCaptionsForm />
    case 'Local captions': return <LocalCaptions />
    case 'AI packaging':
      return <Unavailable title="AI packaging is not available in this build" body="Type what you want in the Prompt bar instead — “add a hook title and captions” lays down the same text objects." />
    default: return null
  }
}

function YourTexts() {
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  const mine = (edl?.tracks ?? []).filter((t) => t.type === 'text').flatMap((t) => t.clips.filter(isTextClip))
  if (mine.length === 0) return <PanelState kind="empty" title="No text on this timeline yet" body="Add text, a style or a template — it lands at the playhead." />
  return (
    <div className="ab-list">
      {mine.map((c) => (
        <button key={c.id} type="button" className={`ab-list-row${selection === c.id ? ' is-active' : ''}`} onClick={() => setSelection(c.id)}>
          <Icon name="text" /><span className="ab-list-text">{c.text || '(empty)'}</span>
          <span className="ui-faint">{c.start.toFixed(1)}–{c.end.toFixed(1)} s</span>
        </button>
      ))}
    </div>
  )
}

export function LocalCaptions() {
  const sid = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const ref = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const importFile = async (f: File) => {
    if (!sid) return
    setBusy(true)
    try {
      const up = await api.uploadSubtitle(sid, f)
      const r = await dispatch('import_srt', { path: up.path })
      if (r) toast.success(`Imported ${f.name}`)
    } catch (e) { toast.error(`Couldn't import ${f.name}: ${errorMessage(e)}`) } finally { setBusy(false) }
  }
  return (
    <div className="ab-stack">
      <span className="ui-heading">Local captions</span>
      <span className="ui-faint">Import an .srt or .vtt file; its cues become a caption track you can edit on the timeline.</span>
      <input ref={ref} type="file" accept=".srt,.vtt,.ass" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void importFile(f) }} />
      <button type="button" className="ui-btn-primary ab-self-start" disabled={busy || !sid} onClick={() => ref.current?.click()}>
        <Icon name="upload" />{busy ? 'Importing…' : 'Import subtitle file…'}
      </button>
    </div>
  )
}
