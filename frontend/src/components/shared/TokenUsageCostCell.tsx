import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCostCell, isPartial } from '@/pages/tokenUsageBreakdown'

/** Render a cost, marking a NUMERIC lower bound. An incomplete total still
 *  shows its dollar figure — the floor is real spend, and `pricedCalls` /
 *  `totalCalls` disclose how much was excluded. */
export function CostCell({ cost, partial, className, pricedCalls, totalCalls }: { cost: number | null; partial?: boolean | number; className?: string; pricedCalls?: number; totalCalls?: number }) {
  const numericPartial = isPartial(partial)
  const floor = numericPartial && pricedCalls != null && totalCalls != null && pricedCalls < totalCalls
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
            {floor
              ? `${pricedCalls} of ${totalCalls} calls had no recorded price and were counted as $0, so this is a lower bound.`
              : 'Partial: some AI turns had no catalog price, so this is a lower bound.'}
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  )
}
