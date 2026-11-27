import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Upload, Loader2 } from 'lucide-react'

/**
 * Shared ghost "push to <exporter>" button used by every exporter push
 * (Report Portal, Datadog, Greenwave) — the only place this markup exists.
 */
interface ExporterPushButtonProps {
  label: string
  /** Label while a push is in flight; defaults to `label`. */
  pushingLabel?: string
  pushing?: boolean
  /** Semantic disable (e.g. no failures) — the tooltip hint rides on it. */
  disabled: boolean
  /** Hover hint; only rendered while `disabled`. */
  disabledHint?: string
  /** Test id for the hint content, for the gate tooltips tests assert on. */
  tooltipTestId?: string
  onClick: () => void
}

export function ExporterPushButton({
  label,
  pushingLabel,
  pushing = false,
  disabled,
  disabledHint,
  tooltipTestId,
  onClick,
}: ExporterPushButtonProps) {
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
              {pushing ? (pushingLabel ?? label) : label}
            </Button>
          </span>
        </TooltipTrigger>
        {disabled && disabledHint && (
          <TooltipContent data-testid={tooltipTestId}>{disabledHint}</TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  )
}
