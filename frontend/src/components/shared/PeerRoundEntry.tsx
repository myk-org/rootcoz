import { useState } from 'react'
import type { PeerRound, TokenUsageSummary } from '@/types'
import type { RepoUrl } from '@/lib/autoLink'
import { Badge } from '@/components/ui/badge'
import { ClassificationBadge } from '@/components/shared/ClassificationBadge'
import { LinkedText } from '@/components/shared/LinkedText'
import { TokenUsageBadge } from '@/components/shared/TokenUsageBadge'

interface PeerRoundEntryProps {
  entry: PeerRound
  repoUrls: RepoUrl[]
  compact?: boolean
  primaryUsage?: TokenUsageSummary | null
}

export function PeerRoundEntry({ entry, repoUrls, compact = false, primaryUsage }: PeerRoundEntryProps) {
  const [detailsExpanded, setDetailsExpanded] = useState(false)
  const isOrchestrator = entry.role === 'orchestrator'

  const TRUNCATE_LIMIT = 200
  const needsTruncation = compact && (entry.details?.length ?? 0) > TRUNCATE_LIMIT
  const displayDetails = needsTruncation && !detailsExpanded
    ? entry.details.slice(0, TRUNCATE_LIMIT) + '...'
    : entry.details

  return (
    <div className="flex flex-col gap-1.5 rounded-md bg-surface-elevated p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={isOrchestrator ? 'default' : 'purple'} className="text-[10px]">
          {isOrchestrator ? (compact ? 'Main AI' : 'Orchestrator') : 'Peer'}
        </Badge>
        <span className="font-mono text-[10px] text-text-tertiary">
          {entry.ai_provider}/{entry.ai_model}
        </span>
        {entry.classification && (
          <ClassificationBadge classification={entry.classification} className="text-[10px]" />
        )}
        {isOrchestrator && entry.round === 1 && primaryUsage ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-text-tertiary">
            Primary (shared) <TokenUsageBadge usage={primaryUsage} />
          </span>
        ) : entry.token_usage ? (
          <TokenUsageBadge usage={{
            total_input_tokens: entry.token_usage.input_tokens,
            total_output_tokens: entry.token_usage.output_tokens,
            total_cache_read_tokens: entry.token_usage.cache_read_tokens,
            total_cache_write_tokens: entry.token_usage.cache_write_tokens,
            total_tokens: entry.token_usage.total_tokens,
            total_cost_usd: entry.token_usage.cost_usd,
            total_duration_ms: entry.token_usage.duration_ms ?? 0,
            total_calls: 1,
            calls: [entry.token_usage],
          }} />
        ) : (
          <span className="text-[10px] text-text-tertiary">Usage unavailable</span>
        )}
        {!isOrchestrator && entry.agrees_with_orchestrator !== null && (
          <Badge variant={entry.agrees_with_orchestrator ? 'success' : 'destructive'} className="text-[10px]">
            {entry.agrees_with_orchestrator ? 'Agrees' : 'Disagrees'}
          </Badge>
        )}
        {!isOrchestrator && entry.agrees_with_orchestrator === null && (
          <Badge variant="destructive" className="text-[10px]">
            Failed
          </Badge>
        )}
        {entry.warning && (
          <Badge variant="warning" className="text-[10px]">
            Turn warning
          </Badge>
        )}
      </div>
      {entry.warning && (
        <p className="rounded border border-signal-orange/20 bg-glow-orange px-2.5 py-1.5 text-xs text-signal-orange whitespace-pre-wrap">
          {entry.warning}
        </p>
      )}
      {entry.error && (
        <p className="rounded border border-signal-red/20 bg-glow-red px-2.5 py-1.5 text-xs text-signal-red whitespace-pre-wrap">
          {entry.error}
        </p>
      )}
      {entry.details && entry.details !== entry.error && (
        <div>
          <p className="text-xs text-text-secondary whitespace-pre-wrap">
            <LinkedText text={displayDetails} repoUrls={repoUrls} />
          </p>
          {needsTruncation && (
            <button
              type="button"
              className="text-[10px] text-text-link hover:underline mt-0.5"
              aria-expanded={detailsExpanded}
              onClick={() => setDetailsExpanded(!detailsExpanded)}
            >
              {detailsExpanded ? 'Show less' : 'Show more'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
