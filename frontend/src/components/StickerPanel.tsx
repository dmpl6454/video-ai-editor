import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'

// A curated picker — not the full Unicode tableau, but the common viral set.
// Categories ordered by what shows up in IG/TikTok captions all day.
const EMOJI_GROUPS: { name: string; emojis: string[] }[] = [
  { name: 'Faces', emojis: ['😂','🤣','😍','🥰','😎','🤩','🥹','🥺','😭','😤','🤯','🤔','😏','😮','🥶','🤤','😴','🫠','🫡','😈'] },
  { name: 'Hands', emojis: ['👀','👏','🙌','👍','👎','🙏','💪','🤝','👌','✌️','🫶','👇','☝️','👉','🫵','🤙'] },
  { name: 'Hearts', emojis: ['❤️','🩷','💖','💗','💓','💕','💞','🖤','🤍','💔','💯','♥️'] },
  { name: 'Symbols', emojis: ['🔥','✨','⭐','💫','💥','⚡','🚀','🎯','🏆','🎉','🎊','💎','💸','💰','🎵','🎶','📈','📉','⚠️','✅','❌','❗','❓','💡','🔔','📌'] },
  { name: 'Fashion', emojis: ['👗','👜','👠','💄','👑','💍','🕶️','👔','👖','👚','🥻','👘','🧥'] },
  { name: 'Food', emojis: ['🍕','🍔','🍟','🌮','🍣','🍜','🍩','🍰','☕','🍵','🍺','🍻','🥂','🍷'] },
  { name: 'Animals', emojis: ['🐶','🐱','🦁','🐻','🐼','🦊','🐨','🐯','🦄','🐸','🐔','🦋'] },
]

const RECENT_KEY = 'vai_recent_emojis'
const MAX_RECENT = 16

function loadRecent(): string[] {
  try {
    const v = localStorage.getItem(RECENT_KEY)
    return v ? JSON.parse(v) : []
  } catch {
    return []
  }
}

function pushRecent(e: string) {
  const cur = loadRecent().filter((x) => x !== e)
  cur.unshift(e)
  localStorage.setItem(RECENT_KEY, JSON.stringify(cur.slice(0, MAX_RECENT)))
}

export function StickerPanel() {
  const dispatch = useStore((s) => s.dispatch)
  const sid = useStore((s) => s.sessionId)
  const refresh = useStore((s) => s.refresh)
  const playhead = useStore((s) => s.playhead)
  const edl = useStore((s) => s.edl)
  const [recent, setRecent] = useState<string[]>(loadRecent())
  const [open, setOpen] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const [uploadErr, setUploadErr] = useState<string | null>(null)

  // Close the picker when clicking anywhere outside it. The ref wraps the
  // toggle button too, so clicking the toggle to close it doesn't fall through
  // here and immediately re-open.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const insert = async (emoji: string) => {
    const w = edl?.canvas.w ?? 1080
    const h = edl?.canvas.h ?? 1920
    const duration = edl?.duration ?? playhead + 3.0
    // Guarantee a real 3s window: if the playhead is close enough to the end
    // that a plain [playhead, playhead+3] clamped to duration would collapse
    // to near-zero, pull start back so the full 3s fits before the end
    // instead (never before 0). Inserting an emoji right at the tail of the
    // timeline used to silently produce a near-invisible sticker — issue 31b.
    const start = Math.max(0, Math.min(playhead, duration - 3.0))
    await dispatch('add_sticker', {
      emoji,
      start,
      end: Math.min(start + 3.0, duration),
      position: [w / 2, h * 0.55],
    })
    pushRecent(emoji)
    setRecent(loadRecent())
  }

  return (
    <div ref={pickerRef} className="sticker-picker" style={{ marginTop: 16 }}>
      <button
        style={{ width: '100%', fontSize: 11 }}
        onClick={() => setOpen((o) => !o)}
        title="Emoji & sticker picker"
      >
        {open ? '▼' : '▶'} 😀 Stickers
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          {recent.length > 0 && (
            <EmojiRow label="Recent" emojis={recent} onPick={insert} />
          )}
          {EMOJI_GROUPS.map((g) => (
            <EmojiRow key={g.name} label={g.name} emojis={g.emojis} onPick={insert} />
          ))}
          <button
            style={{ width: '100%', marginTop: 6, fontSize: 11 }}
            onClick={() => fileRef.current?.click()}
          >
            🖼️ Upload PNG sticker…
          </button>
          <input
            ref={fileRef} type="file" accept="image/png,image/webp,image/gif" hidden
            onChange={async (e) => {
              const f = e.target.files?.[0]
              if (!f || !sid) return
              setUploadErr(null)
              try {
                await api.stickerUpload(sid, f, true, playhead)
                await refresh()
              } catch (err) {
                setUploadErr(err instanceof Error ? err.message : String(err))
              } finally {
                if (fileRef.current) fileRef.current.value = ''
              }
            }}
          />
          {uploadErr && (
            <div style={{ color: '#fbb', fontSize: 10, marginTop: 4 }}>{uploadErr}</div>
          )}
          <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 6 }}>
            Click an emoji or upload a PNG to drop at the playhead (3s).
          </div>
        </div>
      )}
    </div>
  )
}

function EmojiRow({ label, emojis, onPick }: { label: string; emojis: string[]; onPick: (e: string) => void }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 10, color: 'var(--text-dim)', textTransform: 'uppercase',
                    letterSpacing: 0.06 * 10 + 'em', marginBottom: 4 }}>{label}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(8, 1fr)', gap: 2 }}>
        {emojis.map((e) => (
          <button
            key={e}
            onClick={() => onPick(e)}
            draggable
            onDragStart={(ev) => {
              ev.dataTransfer.effectAllowed = 'copy'
              ev.dataTransfer.setData('application/x-vai-emoji', e)
              ev.dataTransfer.setData('text/plain', e)
            }}
            title={`Click to insert at playhead, or drag onto the timeline`}
            style={{
              padding: '4px 0', fontSize: 18, background: 'var(--bg-2)',
              border: '1px solid var(--line)', borderRadius: 4, cursor: 'grab',
              lineHeight: 1, height: 28,
            }}
          >
            {e}
          </button>
        ))}
      </div>
    </div>
  )
}
