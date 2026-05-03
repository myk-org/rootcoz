import { useState, useMemo } from 'react'
import type { FailureAnalysis, PeerDebate, ChildJobAnalysis } from '@/types'
import type { RepoUrl } from '@/lib/autoLink'
import { Badge } from '@/components/ui/badge'
import { PeerRoundEntry } from '@/components/shared/PeerRoundEntry'
import { groupPeerRounds } from '@/lib/peerDebate'
import { ChevronDown, ChevronRight, Users } from 'lucide-react'

interface FailureWithDebate {
  id: string
  testName: string
  /** Additional test names sharing this debate via the same error signature. */
  siblingTestNames: string[]
  jobLabel: string  // e.g. "job-name #42" or empty for top-level
  debate: PeerDebate
}

/** Recursively collect failures with peer_debate, deduplicating by error_signature.
 *  Peer analysis runs once per unique signature; sibling failures share the same
 *  debate. This ensures the summary shows one entry per actual AI debate. */
function collectDebateFailures(
  failures: FailureAnalysis[],
  children: ChildJobAnalysis[],
  jobLabel = '',
): FailureWithDebate[] {
  const bySignature = new Map<string, FailureWithDebate>()

  for (const f of failures) {
    if (!f.peer_debate) continue
    if (!f.error_signature) continue  // skip malformed entries

    const key = `${jobLabel}::${f.error_signature}`
    const existing = bySignature.get(key)
    if (existing) {
      existing.siblingTestNames.push(f.test_name)
    } else {
      bySignature.set(key, {
        id: key,
        testName: f.test_name,
        siblingTestNames: [],
        jobLabel,
        debate: f.peer_debate,
      })
    }
  }

  const result = [...bySignature.values()]

  for (const child of children) {
    const childLabel = `${child.job_name} #${child.build_number}`
    result.push(
      ...collectDebateFailures(
        child.failures ?? [],
        child.failed_children ?? [],
        childLabel,
      ),
    )
  }

  return result
}

/** Extract unique AI provider/model pairs from all debates. */
function getUniqueAiLabels(debates: FailureWithDebate[]): string[] {
  const seen = new Set<string>()
  for (const { debate } of debates) {
    for (const cfg of debate.ai_configs ?? []) {
      seen.add(`${cfg.ai_provider}/${cfg.ai_model}`)
    }
  }
  return [...seen].sort()
}

interface PeerAnalysisSummaryProps {
  failures: FailureAnalysis[]
  childJobAnalyses?: ChildJobAnalysis[]
  repoUrls: RepoUrl[]
}

export function PeerAnalysisSummary({ failures, childJobAnalyses, repoUrls }: PeerAnalysisSummaryProps) {
  const [expanded, setExpanded] = useState(false)

  const debateFailures = useMemo(
    () => collectDebateFailures(failures ?? [], childJobAnalyses ?? []),
    [failures, childJobAnalyses],
  )

  const participatingAis = useMemo(() => getUniqueAiLabels(debateFailures), [debateFailures])
  const consensusCount = useMemo(
    () => debateFailures.filter((d) => d.debate.consensus_reached).length,
    [debateFailures],
  )
  const totalWithDebate = debateFailures.length

  // Do not render when there are no peer debates
  if (totalWithDebate === 0) return null

  const allConsensus = consensusCount === totalWithDebate
  const noConsensus = consensusCount === 0
  const consensusColor = allConsensus
    ? 'bg-signal-green/15 text-signal-green'
    : noConsensus
      ? 'bg-signal-red/12 text-signal-red'
      : 'bg-signal-orange/15 text-signal-orange'

  return (
    <div className="rounded-lg border border-border-muted animate-slide-up">
      {/* Header (always visible, clickable) */}
      <button
        type="button"
        className="flex w-full items-center gap-3 p-4 text-left"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-text-tertiary" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-text-tertiary" />
        )}
        <Users className="h-4 w-4 shrink-0 text-signal-purple" />
        <h2 className="text-xs font-display uppercase tracking-widest text-text-tertiary">
          Peer Analysis
        </h2>

        {/* Participating AI badges */}
        <div className="flex flex-wrap items-center gap-1.5">
          {participatingAis.map((label) => (
            <Badge key={label} variant="outline" className="text-[10px]">
              {label}
            </Badge>
          ))}
        </div>

        {/* Consensus summary */}
        <span className={`ml-auto inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold font-display ${consensusColor}`}>
          {consensusCount}/{totalWithDebate} consensus
        </span>
      </button>

      {/* Expanded content: per-failure debate timeline */}
      {expanded && (
        <div className="border-t border-border-muted p-4 space-y-4">
          {debateFailures.map(({ id, testName, siblingTestNames, jobLabel, debate }) => (
            <DebateEntry key={id} testName={testName} siblingTestNames={siblingTestNames} jobLabel={jobLabel} debate={debate} repoUrls={repoUrls} />
          ))}
        </div>
      )}
    </div>
  )
}

/** A single failure's debate entry within the summary. */
function DebateEntry({ testName, siblingTestNames, jobLabel, debate, repoUrls }: { testName: string; siblingTestNames: string[]; jobLabel: string; debate: PeerDebate; repoUrls: RepoUrl[] }) {
  const [timelineOpen, setTimelineOpen] = useState(false)
  const groupedRounds = useMemo(() => groupPeerRounds(debate.rounds), [debate.rounds])

  return (
    <div className="rounded-md border border-border-muted">
      {/* Entry header */}
      <button
        type="button"
        className="flex w-full items-center gap-3 p-3 text-left"
        onClick={() => setTimelineOpen(!timelineOpen)}
        aria-expanded={timelineOpen}
      >
        {timelineOpen ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        )}
        <div className="min-w-0 flex-1">
          {jobLabel && (
            <span className="block text-[10px] text-text-tertiary font-display">{jobLabel}</span>
          )}
          <span className="font-mono text-xs text-text-secondary truncate block" title={testName}>
            {testName}
          </span>
          {siblingTestNames.length > 0 && (
            <span className="text-[10px] text-text-tertiary" title={siblingTestNames.join('\n')}>
              +{siblingTestNames.length} test{siblingTestNames.length > 1 ? 's' : ''} with same error
            </span>
          )}
        </div>
        <Badge variant={debate.consensus_reached ? 'success' : 'warning'} className="text-[10px] shrink-0">
          {debate.consensus_reached ? 'Consensus' : 'No Consensus'}
        </Badge>
        <span className="text-[10px] font-mono text-text-tertiary shrink-0">
          {debate.rounds_used} of {debate.max_rounds} rounds
        </span>
      </button>

      {/* Timeline: rounds */}
      {timelineOpen && (
        <div className="space-y-4 border-t border-border-muted p-3">
          {groupedRounds.map(({ round: roundNum, entries }) => (
            <div key={roundNum}>
              <p className="mb-2 text-[10px] font-display uppercase tracking-widest text-text-tertiary">
                Round {roundNum}
              </p>
              <div className="space-y-2">
                {entries.map((entry) => (
                  <PeerRoundEntry key={`r${roundNum}-${entry.role}-${entry.ai_provider}-${entry.ai_model}`} entry={entry} repoUrls={repoUrls} compact />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
