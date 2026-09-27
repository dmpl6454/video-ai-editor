// The frame a <video> element PRESENTS, as opposed to its currentTime
// (INSTANT_PREVIEW_SPEC §3.4 "last good frame", §6 R7-R8).

/** Media time (s) of the frame the element PRESENTS right now, from a
 *  WebCodecs VideoFrame of it (its `timestamp` is that frame's pts), or null
 *  where the browser cannot say. Measured: in WKWebView over MSE it is the
 *  presented sample's tfdt (30/30 paused seeks); over a FILE-backed element
 *  WebKit reports currentTime instead (no information, but never wrong). */
export function presentedTime(video: HTMLVideoElement): number | null {
  if (typeof VideoFrame === 'undefined') return null
  try {
    const vf = new VideoFrame(video)
    const ts = vf.timestamp
    vf.close()
    return typeof ts === 'number' && Number.isFinite(ts) ? ts / 1e6 : null
  } catch {
    return null
  }
}

