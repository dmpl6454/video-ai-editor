// What to tell the user after an import landed (QA-083 / QA-092), from the
// server's answer. Pure, so it is tested without a browser.
//
// * An audio-only file sent to the video ingress is now added to the Music
//   lane by the server (it used to be refused with advice that routed it
//   straight back) — say where it went.
// * An audio file is no longer silently cut to the video's length (an 85 s
//   narration became 20 s). When it runs past the picture the server says by
//   how much, and the user gets a one-click "Trim to video" instead.

export interface ImportAnswer {
  kind?: string
  routed_to?: string
  display_name?: string
  clip_id?: string | null
  start?: number
  past_video_s?: number
  video_end?: number
}

export interface FollowUp {
  message: string
  action?: { label: string; tool: string; args: Record<string, unknown> }
}

/** 65.2 → "1:05", 9.04 → "9 s". */
export function shortDuration(sec: number): string {
  const s = Math.round(sec)
  if (s < 60) return `${s} s`
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function importFollowUp(answer: ImportAnswer | null | undefined, fileName: string): FollowUp | null {
  if (!answer) return null
  const name = answer.display_name || fileName
  const past = Number(answer.past_video_s ?? 0)
  const lead = answer.routed_to === 'music' ? `${name} has no picture, so it went on the Music lane.` : ''
  if (past > 0.05 && answer.clip_id && answer.video_end !== undefined) {
    const out = Number(answer.video_end) - Number(answer.start ?? 0)
    const runs = `${lead ? `${lead} It` : name} runs ${shortDuration(past)} past the end of the video.`
    if (out > 0.05) {
      return { message: runs, action: { label: 'Trim to video', tool: 'trim_clip',
                                        args: { clip_id: answer.clip_id, out: Math.round(out * 1000) / 1000 } } }
    }
    return { message: runs }
  }
  return lead ? { message: lead } : null
}
