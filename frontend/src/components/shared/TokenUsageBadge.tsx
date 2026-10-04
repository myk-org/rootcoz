import { Zap } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCompactNumber, formatCost, formatSummedDuration } from '@/lib/format'
import type { TokenUsageSummary } from '@/types'

type TokenUsageBadgeProps =
  | { usage: TokenUsageSummary; graftEstimatedTokensSaved?: number }
  | { usage?: never; graftEstimatedTokensSaved: number }

export function TokenUsageBadge({ usage, graftEstimatedTokensSaved }: TokenUsageBadgeProps) {
  const graftTooltip = graftEstimatedTokensSaved !== undefined && graftEstimatedTokensSaved > 0 && (
    <p>Estimated Graft tokens saved: {graftEstimatedTokensSaved.toLocaleString()}, estimate versus reading whole referenced files, not billed AI tokens.</p>
  )
  if (!usage) {
    if (!graftTooltip) return null
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex items-center gap-1 rounded-full bg-surface-elevated px-2.5 py-0.5 text-[10px] font-mono text-text-tertiary">
            <Zap className="h-3 w-3" /> Graft saved ~{graftEstimatedTokensSaved.toLocaleString()} tokens
          </span>
        </TooltipTrigger>
        <TooltipContent>{graftTooltip}</TooltipContent>
      </Tooltip>
    )
  }

  // A total is always shown: unpriced calls count as $0, so this figure is the
  // spend we can prove. `cost_partial` says whether calls were excluded.
  const cost = formatCost(usage.total_cost_usd ?? 0)
  const showPartial = usage.cost_partial === true
  const pricedCalls = usage.calls.filter(c => c.cost_usd != null).length
  const unpriced = usage.calls.length - pricedCalls
  const partialNote = showPartial && (
    unpriced > 0
      ? `${unpriced} of ${usage.calls.length} calls had no recorded price and were counted as $0, so this is a lower bound.`
      : 'Partial: some AI turns had no catalog price, so this is a lower bound.'
  )
  const summedDuration = formatSummedDuration(usage.total_duration_ms)
  const sources = new Set(usage.calls.map(call => call.credential_source))
  const source = usage.credential_source ?? (sources.has('user') && sources.has('server') ? 'mixed'
    : sources.size === 1 && sources.has('user') ? 'user'
      : sources.size === 1 && sources.has('server') ? 'server' : 'unknown')
  const credentialSource = source === 'mixed' ? 'Mixed' : source === 'user' ? 'User' : source === 'server' ? 'Server' : 'Unknown'

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-flex items-center gap-1 rounded-full bg-surface-elevated px-2.5 py-0.5 text-[10px] font-mono text-text-tertiary">
          <Zap className="h-3 w-3" />
          {formatCompactNumber(usage.total_input_tokens)} in / {formatCompactNumber(usage.total_output_tokens)} out
          {' · '}{cost}{showPartial ? '+' : ''}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-h-80 max-w-sm overflow-y-auto">
        <div className="space-y-1 text-xs">
          <p>Total tokens: {usage.total_tokens.toLocaleString()}</p>
          <p>Input: {usage.total_input_tokens.toLocaleString()} · Output: {usage.total_output_tokens.toLocaleString()}</p>
          {usage.total_cache_read_tokens > 0 && <p>Cache read: {usage.total_cache_read_tokens.toLocaleString()}</p>}
          {usage.total_cache_write_tokens > 0 && <p>Cache write: {usage.total_cache_write_tokens.toLocaleString()}</p>}
          <p>API calls: {usage.total_calls.toLocaleString()}</p>
          <p>Credential source: {credentialSource}</p>
          {graftTooltip}
          {usage.calls.map((call, index) => (
            <p key={index}>{call.call_type} · {call.provider}/{call.model} · {call.credential_source ?? 'unknown'}{call.success === false ? ' · failed' : ''}: {call.total_tokens.toLocaleString()} tokens</p>
          ))}
          {summedDuration && (
            <p>Summed call duration: {summedDuration} (not wall-clock)</p>
          )}
          <p>Cost: {cost}{showPartial ? ' (partial)' : ''}</p>
          {partialNote && <p>{partialNote}</p>}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}
