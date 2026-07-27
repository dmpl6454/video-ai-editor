// Path display helpers shared across panels.
//
// baseName: leaf filename of a path on EITHER separator. A naive
// `p.split('/').pop()` returns the whole `D:\...` path on Windows (and for
// .vae projects moved across OSes the stored separator can differ from the
// host's), so split on both. The `|| p` keeps trailing-separator inputs from
// degrading to '' — they fall back to the raw input instead (mirrors
// EffectsPanel's proven helper).
export const baseName = (p: string): string => {
  const parts = p.split(/[\\/]/)
  return parts[parts.length - 1] || p
}

// Extensions we treat as audio-only. Single source of truth: MediaBin's file
// picker, FileDropOverlay's window-wide drop and Timeline's media-bin drop all
// route on this, so they can't drift apart (they used to carry three copies of
// the same literal, and only two of them ever ran).
export const AUDIO_EXTS = /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|aif|aiff)$/i

// True when a path is audio-only by extension.
//
// Deliberately a cheap name check, not a probe: it decides which LANE a drop
// targets, and it must answer synchronously during a drag. It is an
// affordance, not the guard — the backend re-checks the real stream shape
// (dispatch._reject_videoless_on_video_lane) and is the actual enforcement for
// chat/MCP callers and for audio-only files wearing a video extension.
export const isAudioPath = (p: string): boolean => AUDIO_EXTS.test(baseName(p))
