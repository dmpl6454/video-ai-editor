import { useToasts } from '../toast'

// Stacked transient notifications, bottom-right. Click to dismiss early.
export function ToastHost() {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  if (!toasts.length) return null
  return (
    <div className="toast-host" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <span className="toast-dot" aria-hidden="true" />
          <span className="toast-msg">{t.message}</span>
          {t.action && (
            <button
              className="toast-action"
              onClick={() => { t.action!.onClick(); dismiss(t.id) }}
            >
              {t.action.label}
            </button>
          )}
          <button className="toast-x" onClick={() => dismiss(t.id)} title="Dismiss" aria-label="Dismiss">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
