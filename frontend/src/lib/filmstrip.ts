// Filmstrip tiles and the thumbnail request queue (QA-059).
//
// Tiles were `clipWidth / min(12, …)` wide, so an 85 s clip at 80 px/s got
// twelve 567 px tiles; the cover-crop then took a ~6 px band of each 128×72
// thumb and stretched it across the tile — flat colour blocks, and a 12-minute
// clip read as one solid bar. Now every tile is the frame's own shape
// (row height × source aspect), there are as many as the clip needs, only the
// visible ones exist, and each samples the source time under its own centre.
//
// Requests went out for every tile the draw loop touched at once, saturating
// the browser's six connections and starving the preview video. The queue
// below keeps at most two in flight and forgets tiles scrolled out of view
// before their turn came.

export interface TileSpec {
  /** Content x of the tile's left edge and its width (the clip rect clips it). */
  x: number
  w: number
  /** SOURCE time the tile shows, on a zoom-stable grid. */
  ts: number
  /** That grid's step (s) — the sprite a tile's frame comes from. */
  step: number
}

export interface FilmstripInput {
  x: number; w: number          // clip rect (content px)
  clipH: number                 // drawn clip height
  aspect: number                // source w/h (16/9 until the first thumb says)
  srcIn: number; srcOut: number // source window the clip plays
  viewL: number; viewR: number  // visible content range
}

/** Source-time grid for a tile spanning `span` source seconds: 0.5 s, doubled
 *  until it is at least the span — so nearby zoom levels ask for the SAME
 *  thumbnails instead of minting new URLs on every step. */
export function thumbGrid(span: number): number {
  let g = 0.5
  while (g < span && g < 3600) g *= 2
  return g
}

export function filmstripTiles(p: FilmstripInput): TileSpec[] {
  const tileW = Math.max(12, p.clipH * (p.aspect > 0 ? p.aspect : 16 / 9))
  const srcDur = Math.max(0.01, p.srcOut - p.srcIn)
  if (!(p.w > 0)) return []
  const perPx = srcDur / p.w
  const grid = thumbGrid(tileW * perPx)
  const first = Math.max(0, Math.floor((p.viewL - p.x) / tileW))
  const last = Math.min(Math.ceil(p.w / tileW) - 1, Math.floor((p.viewR - p.x) / tileW))
  const out: TileSpec[] = []
  for (let k = first; k <= last; k++) {
    const tx = p.x + k * tileW
    const mid = Math.min(p.w, (k + 0.5) * tileW)
    let ts = p.srcIn + mid * perPx
    ts = Math.round(ts / grid) * grid
    ts = Math.min(Math.max(ts, p.srcIn), Math.max(p.srcIn, p.srcOut - 0.05))
    out.push({ x: tx, w: tileW, ts: Number(ts.toFixed(3)), step: grid })
  }
  return out
}

// ---------------------------------------------------------------------------
// Sprites (QA-059 remainder): one GET /thumbstrip returns SPRITE_TILES frames
// side by side — slot i of page p is the frame at (p·N + i)·step — so a
// filmstrip costs one request (one ffmpeg run) per SPRITE_TILES tiles instead
// of one per tile. Must match the `n` the server is asked for.

export const SPRITE_TILES = 16

export interface SpriteSlot { step: number; page: number; index: number }

/**
 * The sprite slot holding a tile's frame: the grid point nearest the tile's
 * time that lies inside the clip's source window [srcIn, srcOut). null when
 * the window holds no grid point at all (a clip shorter than one step at this
 * zoom) — that tile falls back to a single /thumb at its own time.
 */
export function spriteSlot(tile: Pick<TileSpec, 'ts' | 'step'>, srcIn: number, srcOut: number): SpriteSlot | null {
  const step = tile.step
  if (!(step > 0)) return null
  const lo = Math.ceil((srcIn - 1e-6) / step)
  const hi = Math.floor((srcOut - 0.05 + 1e-6) / step)
  if (lo > hi) return null
  const k = Math.min(hi, Math.max(lo, Math.round(tile.ts / step)))
  return { step, page: Math.floor(k / SPRITE_TILES), index: k % SPRITE_TILES }
}

/** The request for one sprite page. */
export function spriteUrl(sid: string, src: string, slot: SpriteSlot, h: number): string {
  return `/api/sessions/${sid}/thumbstrip?src=${encodeURIComponent(src)}&step=${slot.step}`
    + `&page=${slot.page}&n=${SPRITE_TILES}&h=${h}`
}

type Status = 'queued' | 'loading' | 'done' | 'error'

/**
 * A bounded, visibility-aware request queue. Call `frame()` at the start of a
 * draw pass, `want(key, url)` for every visible tile that is not loaded yet,
 * then `pump()`. `start(key, url, done)` performs one load and calls
 * `done(ok)`; a failed key is never retried this session.
 */
export class ThumbQueue {
  private status = new Map<string, Status>()
  private urls = new Map<string, string>()
  private order: string[] = []
  private wanted = new Set<string>()
  private active = 0
  private held = false

  private readonly start: (key: string, url: string, done: (ok: boolean) => void) => void
  private readonly maxConcurrent: number

  constructor(start: (key: string, url: string, done: (ok: boolean) => void) => void, maxConcurrent = 2) {
    this.start = start
    this.maxConcurrent = maxConcurrent
  }

  frame(): void {
    this.wanted.clear()
  }

  want(key: string, url: string): void {
    this.wanted.add(key)
    if (this.status.has(key)) return
    this.status.set(key, 'queued')
    this.urls.set(key, url)
    this.order.push(key)
  }

  /** While held, nothing new starts (queued keys wait): the filmstrip holds
   *  its requests until the preview has loaded (lib/previewGate). */
  hold(on: boolean): void {
    this.held = on
    if (!on) this.pump()
  }

  pump(): void {
    if (this.held) return
    while (this.active < this.maxConcurrent && this.order.length) {
      const key = this.order.shift()!
      if (this.status.get(key) !== 'queued') continue
      if (!this.wanted.has(key)) {
        // Scrolled away before its turn: forget it, a later frame re-asks.
        this.status.delete(key)
        this.urls.delete(key)
        continue
      }
      this.status.set(key, 'loading')
      this.active++
      this.start(key, this.urls.get(key)!, (ok) => {
        this.status.set(key, ok ? 'done' : 'error')
        this.active--
        this.pump()
      })
    }
  }

  inFlight(): number { return this.active }
  statusOf(key: string): Status | undefined { return this.status.get(key) }
}
