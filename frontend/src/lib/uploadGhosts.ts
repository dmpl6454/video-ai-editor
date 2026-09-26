// Ghost clips on the timeline while an import is in flight (QA-044 remainder).
//
// The Media panel showed a placeholder row with progress, but the timeline
// showed nothing until the server answered — minutes for a long 4K file —
// so a dropped file looked lost. Every queued import that will land on the
// timeline is drawn as a dashed, non-interactive rect where it will land: at
// the drop point for a lane drop, else at the end of Main video (video) or the
// Music lane (audio), stacked in drop order. The width is nominal: the length
// is not known until the server has read the file. The stage leads the
// label so a narrow ghost still shows the percentage.
import type { UploadItem } from './uploadQueue'
import { uploadStageLabel } from './uploadQueue'

export const GHOST_W = 160
export const GHOST_GAP = 4

export interface GhostRow { id: string; type: string }

export interface Ghost { id: string; laneId: string; x: number; w: number; label: string }

/**
 * @param laneEndX  content x where a lane's last clip ends (the label column's
 *                  right edge when the lane is empty)
 * @param xAt       content x of layout time `t` on a lane
 */
export function uploadGhosts(
  uploads: readonly UploadItem[], rows: readonly GhostRow[],
  laneEndX: (laneId: string) => number, xAt: (laneId: string, t: number) => number,
): Ghost[] {
  const drawn = new Set(rows.map((r) => r.id))
  const music = rows.find((r) => r.type === 'music')?.id ?? 'music'
  const nextX = new Map<string, number>()
  const out: Ghost[] = []
  for (const u of uploads) {
    if (!u.addToTimeline || u.stage === 'failed') continue
    const placed = u.lane != null && u.laneStart != null
    const laneId = placed ? u.lane! : u.kind === 'audio' ? music : 'v1'
    // A lane that is not on screen yet (an empty Music lane) has no row to
    // hold the ghost; the Media panel's row still shows the import.
    if (!drawn.has(laneId)) continue
    const key = placed ? `${laneId}@${u.laneStart}` : laneId
    const x = nextX.get(key) ?? (placed ? xAt(laneId, u.laneStart!) : laneEndX(laneId))
    out.push({ id: u.id, laneId, x, w: GHOST_W, label: `${uploadStageLabel(u)} · ${u.name}` })
    nextX.set(key, x + GHOST_W + GHOST_GAP)
  }
  return out
}
