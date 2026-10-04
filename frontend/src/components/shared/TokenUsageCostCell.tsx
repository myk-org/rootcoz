import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  formatCostCell,
  isPartial,
  lowerBoundSentence,
  LOWER_BOUND_FALLBACK_NOTE,
} from '@/pages/tokenUsageBreakdown'

/** Render a cost, marking a NUMERIC lower bound. An incomplete total still
 *  shows its dollar figure — the floor is real spend, and `pricedCalls` /
 *  `totalCalls` disclose how much was excluded. */
export function CostCell({ cost, partial, className, pricedCalls, totalCalls }: { cost: number | null; partial?: boolean | number; className?: string; pricedCalls?: number; totalCalls?: number }) {
  // A marker next to a figure we cannot show would claim a floor exists when
  // nothing is displayed at all, so an unavailable cost carries no marker.
  const numericPartial = isPartial(partial) && cost != null
  return (
    <span className={className}>
      {formatCostCell(cost)}
      {numericPartial && (
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label="Why this cost is a lower bound"
              // This trigger lives inside clickable job rows; without stopping
              // propagation, reading the explanation expands or collapses the row.
              onClick={(event) => event.stopPropagation()}
              className="ml-1 cursor-default underline decoration-dotted"
            >
              lower bound
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            {lowerBoundSentence(totalCalls, pricedCalls) ?? LOWER_BOUND_FALLBACK_NOTE}
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  )
}
