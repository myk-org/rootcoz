import { useState } from 'react'
import type { PeerDebate } from '@/types'
import type { RepoUrl } from '@/lib/autoLink'
import { Badge } from '@/components/ui/badge'
import { PeerRoundEntry } from '@/components/shared/PeerRoundEntry'
import { groupPeerRounds } from '@/lib/peerDebate'
import { ChevronDown, ChevronRight } from 'lucide-react'

interface PeerDebateSectionProps {
  debate: PeerDebate
  repoUrls: RepoUrl[]
}

export function PeerDebateSection({ debate, repoUrls }: PeerDebateSectionProps) {
  const [expanded, setExpanded] = useState(false)

  const groupedRounds = groupPeerRounds(debate.rounds)

  return (
    <div className="rounded-md border border-border-muted">
      <button
        type="button"
        className="flex w-full items-center gap-3 p-3 text-left"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        )}
        <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary">
          Peer Analysis
        </h4>
        <Badge variant={debate.consensus_reached ? 'success' : 'warning'} className="text-[10px]">
          {debate.consensus_reached ? 'Consensus' : 'No Consensus'}
        </Badge>
        <span className="ml-auto text-[10px] font-mono text-text-tertiary">
          {debate.rounds_used} of {debate.max_rounds} rounds
        </span>
      </button>

      {expanded && (
        <div className="space-y-4 border-t border-border-muted p-3">
          {groupedRounds.map(({ round: roundNum, entries }) => (
            <div key={roundNum}>
              <p className="mb-2 text-[10px] font-display uppercase tracking-widest text-text-tertiary">
                Round {roundNum}
              </p>
              <div className="space-y-2">
                {entries.map((entry, i) => (
                  <PeerRoundEntry key={`${entry.role}-${entry.ai_provider}-${i}`} entry={entry} repoUrls={repoUrls} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
