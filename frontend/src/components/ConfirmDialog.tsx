import { useId } from 'react'
import { Dialog } from './Dialog'

/**
 * An in-app confirm (QA-099) in place of `window.confirm`, which showed the
 * native light dialog with the raw session id ("Delete project s_398e1ebaba?").
 *
 * A thin wrapper over THE app dialog (components/Dialog — one focus/Escape/
 * inert implementation, useMenuA11y): role="alertdialog", labelled by its
 * title and described by its body; focus starts on the SAFE choice (Cancel,
 * the first control), Tab stays inside, Escape cancels, and focus goes back
 * to where it was.
 */
export function ConfirmDialog({ title, body, confirmLabel, danger = false, onConfirm, onCancel }: {
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <Dialog open title={title} labelId={titleId} describedById={bodyId} role="alertdialog"
            onClose={onCancel} showClose={false} className="confirm-dialog"
            footer={<>
              <span className="spacer" />
              <button type="button" onClick={onCancel}>Cancel</button>
              <button type="button" className={danger ? 'danger' : 'primary'} onClick={onConfirm}>{confirmLabel}</button>
            </>}>
      <p id={bodyId} className="confirm-body">{body}</p>
    </Dialog>
  )
}
