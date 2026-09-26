// A project's poster frame in the project picker (QA-099-THUMBS). The URL
// comes from GET /api/sessions (`poster`), versioned by the project's EDL, so
// the browser keeps each image until that project changes. A project with
// nothing to show yet (no video, offline media) gets the film placeholder —
// the route answers 204 and the <img> falls back here.

import { useState } from 'react'
import { Icon } from './Icon'
import './projectPoster.css'

export function ProjectPoster({ src }: { src?: string | null }) {
  const [failed, setFailed] = useState(false)
  return (
    <span className="project-poster" aria-hidden="true">
      {src && !failed
        ? <img src={src} alt="" width={48} height={27} loading="lazy" decoding="async" onError={() => setFailed(true)} />
        : <Icon name="film" />}
    </span>
  )
}
