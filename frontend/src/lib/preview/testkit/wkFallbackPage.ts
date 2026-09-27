// WK page for P1-R1's first bullet end to end (tests/wk/test_wk_fallback_preview.py):
// MediaSource (and ManagedMediaSource) deleted → the app's own decision is
// server mode for every setting that could ask for the client engine, and the
// SERVER's preview.mp4 (a real render of a real session) then PLAYS in a
// <video>: currentTime advances and frames are presented (rVFC where the
// engine has it). Review RD3: the robustness suite pointed here, and the
// file did not exist.
import { parsePreviewSettings, probePreviewCapabilities, resolvePreviewMode } from '../../previewEngineSetting'

interface Config { url: string }

export async function runFallbackPreview(cfg: Config): Promise<Record<string, unknown>> {
  const w = window as unknown as Record<string, unknown>
  const had = { mse: 'MediaSource' in w, managed: 'ManagedMediaSource' in w }
  delete w.MediaSource
  delete w.ManagedMediaSource
  const caps = probePreviewCapabilities()
  const decisions = ['auto', 'client', 'server'].map((engine) => ({
    engine, ...resolvePreviewMode(parsePreviewSettings({ engine, source: 'settings' }), caps, true),
  }))
  const video = document.createElement('video')
  video.muted = true
  video.playsInline = true
  video.style.cssText = 'width:320px;height:180px'
  document.body.appendChild(video)
  let presented = 0
  const hasRvfc = typeof (video as unknown as { requestVideoFrameCallback?: unknown }).requestVideoFrameCallback === 'function'
  if (hasRvfc) {
    const v = video as unknown as { requestVideoFrameCallback(cb: () => void): number }
    const tick = () => { presented++; v.requestVideoFrameCallback(tick) }
    v.requestVideoFrameCallback(tick)
  }
  const errors: string[] = []
  video.addEventListener('error', () => errors.push(String(video.error?.code)))
  video.src = cfg.url
  const t0 = performance.now()
  await video.play().catch((e: unknown) => errors.push(String(e)))
  while (performance.now() - t0 < 8000 && (video.currentTime < 1.0 || (hasRvfc && presented < 10))) {
    await new Promise((r) => setTimeout(r, 50))
  }
  return {
    ua: navigator.userAgent, had, gone: !('MediaSource' in w) && !('ManagedMediaSource' in w), caps, decisions,
    currentTime: video.currentTime, presented, hasRvfc, errors, readyState: video.readyState,
    w: video.videoWidth, h: video.videoHeight, paused: video.paused,
  }
}
