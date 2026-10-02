// Captions (design §2a; brief §2 table): Auto captions (the one form the Text
// tab shares — "Text and Captions entry points should use the same underlying
// caption objects"), Templates (the caption look presets), Add captions (a
// subtitle import, or a caption at the playhead). AI packaging and Auto
// lyrics are not part of this build.
import { useStore } from '../../store'
import { openCaptionStyle } from '../../lib/captionStyleOpen'
import { layoutPlayhead } from '../../lib/timelineLayout'
import { selectNewClip } from '../../lib/newClip'
import { Icon } from '../Icon'
import { AutoCaptionsForm } from './AutoCaptionsForm'
import { LocalCaptions } from './TextTab'
import { Unavailable } from './Catalogue'

export function CaptionsTab({ sub }: { sub: string }) {
  const dispatch = useStore((s) => s.dispatch)
  switch (sub) {
    case 'Auto captions': return <AutoCaptionsForm />
    case 'Templates':
      return (
        <div className="ab-stack">
          <span className="ui-heading">Caption templates</span>
          <span className="ui-faint">The look of every caption on the timeline: style, size, colour, position and the word-emphasis mode.</span>
          <button type="button" className="ui-btn-secondary ab-self-start" onClick={openCaptionStyle}><Icon name="captions" />Open caption style…</button>
        </div>
      )
    case 'Add captions':
      return (
        <div className="ab-stack">
          <span className="ui-heading">Add a caption</span>
          <button type="button" className="ui-btn-secondary ab-self-start" onClick={() => {
            const st = useStore.getState()
            const at = layoutPlayhead(st.edl, st.playhead)
            void dispatch('add_text', { text: 'Caption', role: 'caption', start: at, end: at + 3 }).then((r) => { if (r) void selectNewClip(r.result) })
          }}><Icon name="plus" />Caption at the playhead</button>
          <div className="ui-hairline" />
          <LocalCaptions />
        </div>
      )
    case 'AI packaging':
      return <Unavailable title="AI packaging is not available in this build" body="The Prompt bar does the same job in words: “add captions and a hook title”." />
    case 'Auto lyrics':
      return <Unavailable title="Auto lyrics are not available in this build" body="Lyric alignment needs a music-aware model this build does not ship. Auto captions transcribe sung words as speech." />
    default: return null
  }
}
