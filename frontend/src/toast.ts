// Minimal toast system. A tiny Zustand store + a `toast` helper so any module
// (store actions, components) can surface a transient message without prop
// drilling. Rendered by <ToastHost>.
import { create } from 'zustand'

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastAction { label: string; onClick: () => void }

export interface Toast {
  id: number
  message: string
  kind: ToastKind
  action?: ToastAction   // optional inline button, e.g. "Undo"
}

interface ToastState {
  toasts: Toast[]
  push: (message: string, kind?: ToastKind, ttlMs?: number, action?: ToastAction) => number
  dismiss: (id: number) => void
}

let _nextId = 0
const DEFAULT_TTL = 4200

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (message, kind = 'info', ttlMs = DEFAULT_TTL, action) => {
    const id = ++_nextId
    set((s) => ({ toasts: [...s.toasts, { id, message, kind, action }] }))
    if (ttlMs > 0) {
      setTimeout(() => {
        set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
      }, ttlMs)
    }
    return id
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

export const toast = {
  success: (m: string, ttlMs?: number) => useToasts.getState().push(m, 'success', ttlMs),
  error: (m: string, ttlMs?: number) => useToasts.getState().push(m, 'error', ttlMs),
  info: (m: string, ttlMs?: number) => useToasts.getState().push(m, 'info', ttlMs),
  /** Toast with an inline action button (e.g. "Clip deleted" + "Undo"). */
  action: (m: string, action: ToastAction, opts?: { kind?: ToastKind; ttlMs?: number }) =>
    useToasts.getState().push(m, opts?.kind ?? 'info', opts?.ttlMs ?? DEFAULT_TTL, action),
}
