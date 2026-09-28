// When a playhead change is a SEEK for the client engine (ClientPreview).
// While playing, the rAF loop writes the engine's own presented time into the
// store; those writes (`lastWritten`) are not jumps. A paused seek records the
// time it sought to as well — otherwise the Play that follows it (same
// playhead, but `lastWritten` still the pre-seek time) issued a redundant
// playing seek that stopped and restarted the sound 2–4 ms after play began
// (Final QA: the WebKit own-suspend race that paused playback 5 frames in).

const SAME_S = 1e-9

export function playheadSeek(
  isPlaying: boolean, playhead: number, lastWritten: number | null,
): { seek: boolean; lastWritten: number | null } {
  if (isPlaying && lastWritten !== null && Math.abs(playhead - lastWritten) < SAME_S) {
    return { seek: false, lastWritten }
  }
  return { seek: true, lastWritten: playhead }
}
