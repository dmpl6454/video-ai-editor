import { useState } from 'react'
import { useStore } from '../../store'
import { selectNewClip } from '../../lib/newClip'
import { TEXT_STYLE_PRESETS } from '../../lib/textStyles'
import {
  TEXT_TEMPLATES, insertDefaultText, insertTextStyle, insertTextTemplate, templateEnabled,
  type TextInsertDeps,
} from '../../lib/textPresets'
import { useRailChord } from '../rail/useRailChord'
import { Icon } from '../Icon'
import { DeepLinks } from '../rail/DeepLinkRow'
import './textPanel.css'

// The Text tool panel (docs/design/LEFT_RAIL_SPEC.md §2.5, R2): the top bar's
// "Text" button and its "Text presets" popover, laid out inline with the same
// behaviour. "Add text at playhead" drops the default text (selected, so the
// Properties inspector opens on it); the Styles gallery and the Templates read
// the one field above them. #Hashtag and @Handle stay disabled until the field
// has text, as before. The inserts themselves live in lib/textPresets so the
// ⌥T `addText` command (R4) takes the same path. Last, the AI text & brand
// deep links (R5): each opens its card in the AI panel.
//
// `data-text-presets` wraps the panel and its FIRST button is the add button:
// the a11y suite's `[data-text-presets] > button` helper relies on both.

const deps: TextInsertDeps = {
  state: () => useStore.getState(),
  dispatch: (tool, args) => useStore.getState().dispatch(tool, args),
  select: selectNewClip,
}

export function TextPanel({ active = false }: { active?: boolean }) {
  const [fieldText, setFieldText] = useState('')
  // The chord is shown only while the live keymap binds one (R4 binds ⌥T).
  const chord = useRailChord('addText')
  return (
    <div className="text-panel" data-text-presets>
      <button
        type="button"
        className="panel-btn text-panel-add"
        onClick={() => { void insertDefaultText(deps) }}
        aria-keyshortcuts={chord.aria || undefined}
        title="Add a text overlay at the playhead (edit it in Properties)"
      >
        <Icon name="plus" /> Add text at playhead
        {chord.label && <kbd className="text-panel-kbd" aria-hidden="true">{chord.label}</kbd>}
      </button>
      <label className="text-panel-field">
        <span className="text-panel-field-label">Text, #hashtag or @handle</span>
        <input
          type="text"
          value={fieldText}
          onChange={(e) => setFieldText(e.target.value)}
          placeholder="e.g. fyp or @myhandle"
        />
      </label>

      <h3 className="section-label tool-section-label">Styles</h3>
      <div className="text-style-grid" role="group" aria-label="Text styles">
        {TEXT_STYLE_PRESETS.map((p) => (
          <button key={p.id} type="button" className="text-style-chip"
                  onClick={() => { void insertTextStyle(deps, p, fieldText) }}
                  title={`Add “${p.label}” text at the playhead`}>
            <span className="text-style-sample" style={{
              color: p.sample.color, background: p.sample.background ?? 'transparent',
              fontFamily: p.sample.fontFamily, fontWeight: p.sample.fontWeight ?? 400,
            }}>Aa</span>
            {p.label}
          </button>
        ))}
      </div>

      <h3 className="section-label tool-section-label">Templates</h3>
      <div className="text-template-row" role="group" aria-label="Text templates">
        {TEXT_TEMPLATES.map((t) => (
          <button
            key={t.name}
            type="button"
            title={t.title}
            disabled={!templateEnabled(t, fieldText)}
            onClick={() => { void insertTextTemplate(deps, t.name, fieldText) }}
          >
            {t.label}
          </button>
        ))}
      </div>
      <p className="text-panel-hint">#Hashtag and @Handle use the text above: type it first.</p>
      <DeepLinks from="text" active={active} />
    </div>
  )
}
