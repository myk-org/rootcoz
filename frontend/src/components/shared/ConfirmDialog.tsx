import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  variant?: 'default' | 'destructive'
  onConfirm: () => void
  loading?: boolean
  /** Error message shown in the dialog body (e.g. a failed request). */
  error?: string | null
  /** Muted second line under the error — the offending items behind it. */
  errorDetail?: string | null
  /** Informational line for a request that succeeded with a caveat. */
  notice?: string | null
  /** Muted second line under the notice — the items it names. */
  noticeDetail?: string | null
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  onConfirm,
  loading = false,
  error,
  errorDetail,
  notice,
  noticeDetail,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-signal-red">{error}</p>}
        {errorDetail && <p className="text-xs text-text-tertiary">{errorDetail}</p>}
        {notice && <p className="text-sm text-text-secondary">{notice}</p>}
        {noticeDetail && <p className="text-xs text-text-tertiary">{noticeDetail}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            variant={variant === 'destructive' ? 'destructive' : 'default'}
            onClick={onConfirm}
            disabled={loading}
          >
            {loading ? 'Loading...' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
