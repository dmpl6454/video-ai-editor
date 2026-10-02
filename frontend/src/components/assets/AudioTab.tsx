// Audio (design §2a; brief §2 table): Import (audio files and the voiceover
// recorder), Yours (the project's audio), and the catalogue rows the
// reference shows — AI music, Music, Sound effects — which this build does
// not ship as libraries: each says so and points at what does exist (the
// Prompt bar's generated music beds). Copyright is the plain statement.
import { VoRecorder } from '../VoRecorder'
import { MediaGrid } from './MediaTab'
import { Unavailable } from './Catalogue'

export function AudioTab({ sub }: { sub: string }) {
  switch (sub) {
    case 'Import':
      return (
        <div className="ab-stack">
          <MediaGrid filter="all" kinds={['audio']} searchPlaceholder="Search audio" recordSlot={null} />
          <div className="ui-hairline" />
          <span className="ui-heading">Record</span>
          <div className="ab-wrapped"><VoRecorder /></div>
        </div>
      )
    case 'Yours': return <MediaGrid filter="yours" kinds={['audio']} searchPlaceholder="Search audio" recordSlot={null} />
    case 'AI music':
      return <Unavailable title="AI music lives in the Prompt bar" body="Type “add upbeat music” or “add a calm bed under the voice” — the Prompt bar generates a music bed, ducks it under speech and lays it on the Music lane." />
    case 'Music':
      return <Unavailable title="No music library in this build" body="Import your own tracks under Import, or ask the Prompt bar for a generated bed." />
    case 'Sound effects':
      return <Unavailable title="No sound-effect library in this build" body="Import a sound file under Import; it lands on the Music lane at the playhead." />
    case 'Copyright':
      return (
        <div className="ab-stack">
          <span className="ui-heading">Copyright</span>
          <span className="ui-faint" style={{ lineHeight: 1.5 }}>
            Everything you import stays yours and stays on this computer. Generated music beds are synthesised locally and carry no
            third-party rights. Emoji stickers are Apple artwork served from a public mirror — fine locally; check before you publish an
            export commercially.
          </span>
        </div>
      )
    default: return null
  }
}
