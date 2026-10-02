// Source preview (design §2b "Source preview"; brief §6 "Distinguish source
// preview from timeline playback"): the previewed asset's own file, played
// with native controls, independent of the timeline.
import { useStore } from '../../store'
import { sourceFileUrl } from '../../lib/playerControls'
import { Icon } from '../Icon'
import type { PreviewedAsset } from '../inspector/inspectorStore'

export function SourcePreview({ asset, width, height }: { asset: PreviewedAsset | null; width: number; height: number }) {
  const sid = useStore((s) => s.sessionId)
  if (!asset || !sid) return null
  const url = sourceFileUrl(sid, asset.src)
  if (!url) {
    return (
      <div className="pl-source-note" style={{ width, height }}>
        <Icon name="info" /> This file lives outside the project folder, so the Player can't stream it. Add it to the timeline to see it.
      </div>
    )
  }
  if (asset.kind === 'audio') {
    return (
      <div className="pl-source-audio" style={{ width, height }}>
        <Icon name="audioFile" size={32} />
        <audio key={url} src={url} controls preload="metadata" aria-label={`Preview ${asset.name}`} />
      </div>
    )
  }
  return (
    <video key={url} src={url} controls preload="metadata" playsInline aria-label={`Preview ${asset.name}`}
           style={{ width, height, objectFit: 'contain', background: '#000', display: 'block' }} />
  )
}
