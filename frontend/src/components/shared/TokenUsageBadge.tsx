import { Zap } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCompactNumber, formatCost } from '@/lib/format'
import type { TokenUsageSummary } from '@/types'

type TokenUsageBadgeProps =
  | { usage: TokenUsageSummary; graftEstimatedTokensSaved?: never }
  | { usage?: never; graftEstimatedTokensSaved: number }

export function TokenUsageBadge({ usage, graftEstimatedTokensSaved }: TokenUsageBadgeProps) {
  if (graftEstimatedTokensSaved !== undefined) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex items-center gap-1 rounded-full bg-surface-elevated px-2.5 py-0.5 text-[10px] font-mono text-text-tertiary cursor-help">
            Estimated Graft tokens saved: {formatCompactNumber(graftEstimatedTokensSaved)}
          </span>
        </TooltipTrigger>
        <TooltipContent>
          {graftEstimatedTokensSaved.toLocaleString()} tokens saved, estimate versus reading whole referenced files, not billed AI tokens.
        </TooltipContent>
      </Tooltip>
    )
  }
  if (!usage) return null
  const cost = usage.total_cost_usd == null ? 'Unavailable' : formatCost(usage.total_cost_usd)

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1 rounded-full bg-surface-elevated px-2.5 py-0.5 text-[10px] font-mono text-text-tertiary">
          <Zap className="h-3 w-3" />
          {formatCompactNumber(usage.total_input_tokens)} in / {formatCompactNumber(usage.total_output_tokens)} out
          {' · '}{cost}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-h-80 max-w-sm overflow-y-auto">
        <div className="space-y-1 text-xs">
          <p>Total tokens: {usage.total_tokens.toLocaleString()}</p>
          <p>Input: {usage.total_input_tokens.toLocaleString()} · Output: {usage.total_output_tokens.toLocaleString()}</p>
          {usage.total_cache_read_tokens > 0 && <p>Cache read: {usage.total_cache_read_tokens.toLocaleString()}</p>}
          {usage.total_cache_write_tokens > 0 && <p>Cache write: {usage.total_cache_write_tokens.toLocaleString()}</p>}
          <p>API calls: {usage.total_calls.toLocaleString()}</p>
          {usage.calls.map((call, index) => (
            <p key={index}>{call.call_type} · {call.provider}/{call.model} · {call.credential_source ?? 'unknown'}: {call.total_tokens.toLocaleString()} tokens</p>
          ))}
          {usage.total_duration_ms > 0 && <p>Duration: {(usage.total_duration_ms / 1000).toFixed(1)}s</p>}
          <p>Cost: {cost}</p>
        </div>
      </TooltipContent>
    </Tooltip>
  )
}
