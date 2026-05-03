import { Zap } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCompactNumber, formatCost } from '@/lib/format'
import type { TokenUsageSummary } from '@/types'

interface TokenUsageBadgeProps {
  usage: TokenUsageSummary
}

export function TokenUsageBadge({ usage }: TokenUsageBadgeProps) {
  const hasCost = usage.total_cost_usd != null && usage.total_cost_usd > 0

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1 rounded-full bg-surface-elevated px-2.5 py-0.5 text-[10px] font-mono text-text-tertiary">
          <Zap className="h-3 w-3" />
          {formatCompactNumber(usage.total_input_tokens)} in / {formatCompactNumber(usage.total_output_tokens)} out
          {hasCost && <> · {formatCost(usage.total_cost_usd)}</>}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        <div className="space-y-1 text-xs">
          <p>Total tokens: {usage.total_tokens.toLocaleString()}</p>
          <p>Input: {usage.total_input_tokens.toLocaleString()} · Output: {usage.total_output_tokens.toLocaleString()}</p>
          {usage.total_cache_read_tokens > 0 && <p>Cache read: {usage.total_cache_read_tokens.toLocaleString()}</p>}
          {usage.total_cache_write_tokens > 0 && <p>Cache write: {usage.total_cache_write_tokens.toLocaleString()}</p>}
          <p>API calls: {usage.total_calls.toLocaleString()}</p>
          {usage.total_duration_ms > 0 && <p>Duration: {(usage.total_duration_ms / 1000).toFixed(1)}s</p>}
          {hasCost && <p>Cost: {formatCost(usage.total_cost_usd)}</p>}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}
