import { useEffect } from 'react'
import { useStore } from './store'

/**
 * Keyboard shortcuts. CapCut uses ⌘B for split; Premiere/Resolve use S, comma,
 * period, J, K, L, [, ], M. We support both vocabularies.
 */
export function useShortcuts() {
  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      const s = useStore.getState()
      const meta = e.metaKey || e.ctrlKey

      // ----- Transport -----
      if (e.code === 'Space') {
        e.preventDefault()
        s.setPlaying(!s.isPlaying)
        s.setPlaybackRate(1)
      } else if (!meta && e.code === 'KeyJ') {
        // J: shuttle reverse / faster reverse
        e.preventDefault()
        const r = s.playbackRate
        s.setPlaybackRate(r > 0 ? -1 : Math.max(-4, r * 2))
        s.setPlaying(true)
      } else if (!meta && e.code === 'KeyK') {
        e.preventDefault()
        s.setPlaying(false); s.setPlaybackRate(1)
      } else if (!meta && e.code === 'KeyL') {
        e.preventDefault()
        const r = s.playbackRate
        s.setPlaybackRate(r < 0 ? 1 : Math.min(4, r * 2 || 1))
        s.setPlaying(true)
      } else if (!meta && e.code === 'Comma') {
        e.preventDefault()
        s.setPlayhead(s.playhead - 1 / 30)
      } else if (!meta && e.code === 'Period') {
        e.preventDefault()
        s.setPlayhead(s.playhead + 1 / 30)
      } else if (e.code === 'ArrowLeft') {
        s.setPlayhead(s.playhead - (e.shiftKey ? 1 : 1 / 30))
      } else if (e.code === 'ArrowRight') {
        s.setPlayhead(s.playhead + (e.shiftKey ? 1 : 1 / 30))
      }

      // ----- Editing -----
      else if ((meta && e.code === 'KeyB') || (!meta && e.code === 'KeyS')) {
        e.preventDefault()
        await s.splitAtPlayhead()
      } else if ((e.code === 'Delete' || e.code === 'Backspace') && (s.selection || s.multiSelection.length)) {
        e.preventDefault()
        const ids = Array.from(new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[]))
        if (ids.length === 1) {
          await s.dispatch('ripple_delete', { clip_id: ids[0] })
        } else if (ids.length > 1) {
          await s.dispatch('bulk_delete', { clip_ids: ids })
        }
        s.clearSelection()
      } else if (meta && e.code === 'KeyD') {
        e.preventDefault()
        const ids = Array.from(new Set([s.selection, ...s.multiSelection].filter(Boolean) as string[]))
        if (ids.length > 1) await s.dispatch('bulk_duplicate', { clip_ids: ids })
        else await s.duplicateSelection()
      }

      // ----- Marks & markers -----
      else if (!meta && e.code === 'BracketLeft') {
        e.preventDefault()
        s.setInMark(s.playhead)
      } else if (!meta && e.code === 'BracketRight') {
        e.preventDefault()
        s.setOutMark(s.playhead)
      } else if (!meta && e.code === 'KeyM') {
        e.preventDefault()
        await s.dispatch('add_marker', { time: s.playhead })
      }

      // ----- Selection / undo -----
      else if (e.code === 'Escape') {
        s.clearSelection()
        s.setInMark(null); s.setOutMark(null)
      } else if (meta && e.shiftKey && e.code === 'KeyZ') {
        e.preventDefault()
        await s.dispatch('redo')
      } else if (meta && e.code === 'KeyZ') {
        e.preventDefault()
        await s.dispatch('undo')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
