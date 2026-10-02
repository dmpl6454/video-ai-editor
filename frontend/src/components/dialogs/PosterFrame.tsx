// The 16:9 preview a dialog shows (Export, Export and share): the project's
// poster frame (the first video clip's frame, QA-099-THUMBS), or a gradient.
import { useStore } from '../../store'

export function PosterFrame({ children }: { children?: React.ReactNode }) {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const hasVideo = !!edl?.tracks.some((t) => t.type === 'video' && t.clips.length)
  const src = sid && hasVideo ? `/api/sessions/${sid}/poster?v=${edl?.duration ?? 0}` : null
  return (
    <div className="dlg-poster">
      {src && <img src={src} alt="" onError={(e) => { e.currentTarget.style.display = 'none' }} />}
      {children}
    </div>
  )
}
