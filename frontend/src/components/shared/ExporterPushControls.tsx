import type { ReactNode } from 'react'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  Upload,
  XCircle,
} from 'lucide-react'

export type ExporterPushStatus = 'failure' | 'noop' | 'partial' | 'success'

interface ExporterPushControlsProps {
  exporterName: string
  hasFailures: boolean
  pushing: boolean
  onRequestPush: () => void
  confirmOpen: boolean
  onConfirmOpenChange: (open: boolean) => void
  confirmTitle: string
  confirmContent?: ReactNode
  confirmClassName?: string
  onConfirm: () => void
  resultOpen: boolean
  onResultOpenChange: (open: boolean) => void
  resultStatus: ExporterPushStatus
  resultTitle: string
  resultContent?: ReactNode
}

const STATUS_ICON = {
  failure: <XCircle className="h-5 w-5 text-signal-red" />,
  noop: <Info className="h-5 w-5 text-signal-blue" />,
  partial: <AlertTriangle className="h-5 w-5 text-signal-orange" />,
  success: <CheckCircle2 className="h-5 w-5 text-signal-green" />,
} satisfies Record<ExporterPushStatus, ReactNode>

/** Shared exporter push trigger and dialog shells with exporter-specific slots. */
export function ExporterPushControls({
  exporterName,
  hasFailures,
  pushing,
  onRequestPush,
  confirmOpen,
  onConfirmOpenChange,
  confirmTitle,
  confirmContent,
  confirmClassName = 'sm:max-w-[400px]',
  onConfirm,
  resultOpen,
  onResultOpenChange,
  resultStatus,
  resultTitle,
  resultContent,
}: ExporterPushControlsProps) {
  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={onRequestPush}
                disabled={pushing || !hasFailures}
              >
                {pushing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
                {pushing ? 'Pushing...' : `Push to ${exporterName}`}
              </Button>
            </span>
          </TooltipTrigger>
          {!hasFailures && <TooltipContent>No failures to push</TooltipContent>}
        </Tooltip>
      </TooltipProvider>

      <Dialog open={confirmOpen} onOpenChange={onConfirmOpenChange}>
        <DialogContent className={`${confirmClassName} bg-surface-card border-border-default`}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Upload className="h-5 w-5 text-text-secondary" />
              {confirmTitle}
            </DialogTitle>
          </DialogHeader>
          {confirmContent}
          <DialogFooter>
            <Button variant="outline" onClick={() => onConfirmOpenChange(false)}>
              Cancel
            </Button>
            <Button onClick={onConfirm}>Push</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={resultOpen} onOpenChange={onResultOpenChange}>
        <DialogContent className="sm:max-w-[520px] bg-surface-card border-border-default overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {STATUS_ICON[resultStatus]}
              {resultTitle}
            </DialogTitle>
          </DialogHeader>
          {resultContent}
          <DialogFooter>
            <Button variant="outline" onClick={() => onResultOpenChange(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
