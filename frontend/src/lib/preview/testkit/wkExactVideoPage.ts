// WK page for the server preview's colour-exact presenter (Final QA r2,
// tests/wk/test_wk_exact_video.py): the server's preview.mp4 of a flat-grey
// session is played in a <video>, then read back two ways in real WKWebView —
// through `createExactPresenter` (the WebGL canvas Preview.tsx lays over the
// <video> in WebKit) and through a plain 2D `drawImage(<video>)` (the
// colour-managed path). The exact read must be the export's grey.
import { createExactPresenter } from '../../videoColour'

interface Config { url: string }

export async function runExactVideo(cfg: Config): Promise<Record<string, unknown>> {
  const video = document.createElement('video')
  video.muted = true
  video.playsInline = true
  video.style.cssText = 'width:320px;height:180px'
  document.body.appendChild(video)
  const errors: string[] = []
  video.addEventListener('error', () => errors.push(String(video.error?.code)))
  video.src = cfg.url
  const t0 = performance.now()
  while (performance.now() - t0 < 8000 && video.readyState < 2) await new Promise((r) => setTimeout(r, 50))
  video.currentTime = 1.0
  await new Promise((r) => { video.addEventListener('seeked', r, { once: true }); setTimeout(r, 3000) })
  const w = 64
  const h = 36
  const cv = document.createElement('canvas')
  cv.width = w
  cv.height = h
  document.body.appendChild(cv)
  const presenter = createExactPresenter(cv)
  const drew = presenter ? presenter.draw(video) : false
  let exact: number[] | null = null
  if (drew) {
    const gl = cv.getContext('webgl2')!
    const px = new Uint8Array(w * h * 4)
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px)
    const i = ((h >> 1) * w + (w >> 1)) * 4
    exact = [px[i], px[i + 1], px[i + 2]]
  }
  const c2 = document.createElement('canvas')
  c2.width = w
  c2.height = h
  const g2 = c2.getContext('2d')!
  g2.drawImage(video, 0, 0, w, h)
  const d = g2.getImageData(w >> 1, h >> 1, 1, 1).data
  return {
    ua: navigator.userAgent, errors, readyState: video.readyState, presenter: !!presenter, drew,
    exact, plain: [d[0], d[1], d[2]], w: video.videoWidth, h: video.videoHeight,
  }
}
