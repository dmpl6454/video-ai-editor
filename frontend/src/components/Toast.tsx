import { useToasts } from '../toast'

// Stacked transient notifications, bottom-right. Click to dismiss early.
export function ToastHost() {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  if (!toasts.length) return null
  return (
    <div className="toast-host" role="status" aria-live="polite">
      {toasts.map((t) => (
        <button
          key={t.id}
          className={`toast toast-${t.kind}`}
          onClick={() => dismiss(t.id)}
          title="Dismiss"
        >
          <span className="toast-dot" aria-hidden="true" />
          <span className="toast-msg">{t.message}</span>
        </button>
      ))}
    </div>
  )
}
