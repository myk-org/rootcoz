import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Upload, Loader2 } from 'lucide-react'

/**
 * Shared ghost "push to <exporter>" button used by every exporter push
 * (Report Portal, Greenwave) — the only place this markup exists.
 */
interface ExporterPushButtonProps {
  label: string
  pushingLabel: string
  pushing: boolean
  /** Semantic disable (e.g. no failures) — also drives the tooltip hint. */
  disabled: boolean
  disabledHint: string
  onClick: () => void
}

export function ExporterPushButton({ label, pushingLabel, pushing, disabled, disabledHint, onClick }: ExporterPushButtonProps) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span>
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5 text-xs"
              onClick={onClick}
              disabled={pushing || disabled}
            >
              {pushing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Upload className="h-3.5 w-3.5" />
              )}
              {pushing ? pushingLabel : label}
            </Button>
          </span>
        </TooltipTrigger>
        {disabled && (
          <TooltipContent>{disabledHint}</TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  )
}
