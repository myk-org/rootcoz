import { useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, Coins } from 'lucide-react'
import type { AnalysisResult } from '@/types'
import { formatCost, formatCompactNumber, formatSummedDuration } from '@/lib/format'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  analyzedFailingTests,
  peerGroups,
  primaryGroups,
  spendAverages,
  stageBreakdown,
  type PeerGroup,
  type PrimaryGroup,
  type StageUsage,
} from './aiSpend'

/** Cost or token total that may be unavailable — never render those as zero. */
function cost(value: number | null): string {
  return value == null ? 'Unavailable' : formatCost(value)
}

function Cost({ value }: { value: number | null }) {
  return <span className={value == null ? 'italic text-text-tertiary' : ''}>{cost(value)}</span>
}

/** Keyboard-focusable tooltip trigger — plain spans hide the explanation from keyboard users. */
function Hint({ label, trigger, content }: { label: string; trigger: string; content: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" aria-label={label} className="cursor-default underline decoration-dotted">
          {trigger}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{content}</TooltipContent>
    </Tooltip>
  )
}

/** Cache tokens are shown apart from input/output and excluded from the billed total. */
function StageRow({ stage }: { stage: StageUsage }) {
  return (
    <tr className="border-t border-border-muted">
      <td className="py-1 pr-2 font-mono text-xs">{stage.callType || 'unknown'}</td>
      <td className="py-1 pr-2 text-right font-mono text-xs">{stage.calls}</td>
      <td className="py-1 pr-2 text-right font-mono text-xs">{formatCompactNumber(stage.inputTokens)}</td>
      <td className="py-1 pr-2 text-right font-mono text-xs">{formatCompactNumber(stage.outputTokens)}</td>
      <td className="py-1 pr-2 text-right font-mono text-xs">
        {formatCompactNumber(stage.cacheReadTokens)}
        {stage.cacheWriteTokens > 0 && ` +${formatCompactNumber(stage.cacheWriteTokens)}w`}
      </td>
      <td className="py-1 pr-2 text-right font-mono text-xs">{formatCompactNumber(stage.totalTokens)}</td>
      <td className="py-1 text-right font-mono text-xs">
        <Cost value={stage.costUsd} />
        {stage.partial && <span className="text-text-tertiary"> (lower bound)</span>}
      </td>
    </tr>
  )
}

function StageTable({ stages }: { stages: StageUsage[] }) {
  return (
    <table className="w-full">
      <thead>
        <tr className="text-[10px] uppercase tracking-widest text-text-tertiary">
          <th className="py-1 pr-2 text-left font-normal">Stage</th>
          <th className="py-1 pr-2 text-right font-normal">Calls</th>
          <th className="py-1 pr-2 text-right font-normal">In</th>
          <th className="py-1 pr-2 text-right font-normal">Out</th>
          <th className="py-1 pr-2 text-right font-normal">
            <Hint
              label="What cache read and write tokens are"
              trigger="Cache r/w"
              content="Cache read + write tokens. Not billed like input/output, so they are excluded from the billed token total."
            />
          </th>
          <th className="py-1 pr-2 text-right font-normal">
            <Hint
              label="What billed tokens include"
              trigger="Tokens"
              content="Input + output tokens only. Cache read and write tokens are not billed the same way, so they are excluded."
            />
          </th>
          <th className="py-1 text-right font-normal">Cost</th>
        </tr>
      </thead>
      <tbody>
        {stages.map(stage => <StageRow key={stage.callType} stage={stage} />)}
      </tbody>
    </table>
  )
}

function PrimaryGroupRow({ group }: { group: PrimaryGroup }) {
  return (
    <tr className="border-t border-border-muted">
      <td className="py-1 pr-2 font-mono text-xs break-all">{group.testName}</td>
      <td className="py-1 pr-2 font-mono text-[10px] text-text-tertiary">
        {group.childLabel || 'top-level'} · shared by {group.failureCount} test{group.failureCount === 1 ? '' : 's'}
      </td>
      <td className="py-1 text-right font-mono text-xs">
        <Cost value={group.usage?.total_cost_usd ?? null} />
      </td>
    </tr>
  )
}

function PeerGroupRow({ group }: { group: PeerGroup }) {
  return (
    <tr className="border-t border-border-muted align-top">
      <td className="py-1 pr-2 font-mono text-xs break-all">{group.testName}</td>
      <td className="py-1 pr-2 text-[10px] text-text-tertiary">
        {group.childLabel || 'top-level'}
        {group.siblingCount > 0 && ` · ${group.siblingCount} shared`}
        <ul className="mt-0.5 space-y-0.5">
          {group.rounds.map((row, i) => (
            <li key={`${row.round}-${row.agentLabel}-${i}`} className="font-mono">
              R{row.round} {row.agentLabel} · {row.agent}:{' '}
              {row.usage
                ? `${formatCompactNumber(row.usage.total_tokens)} tokens · ${cost(row.usage.cost_usd)}`
                : <span className="italic">usage unavailable</span>}
            </li>
          ))}
        </ul>
      </td>
      <td className="py-1 text-right font-mono text-xs"><Cost value={group.costUsd} /></td>
    </tr>
  )
}

function AverageRow({ label, hint, value }: { label: string; hint: string; value: number | null }) {
  return (
    <div className="flex items-baseline justify-between gap-4 text-xs">
      <span className="flex items-center gap-1 text-text-tertiary">
        {label}
        <Hint label={`What counts in ${label.toLowerCase()}`} trigger="what counts?" content={hint} />
      </span>
      {value == null
        ? <span className="font-mono italic text-text-tertiary">N/A</span>
        : <span className="font-mono">{formatCost(value)}</span>}
    </div>
  )
}

export function AiSpendBreakdown({ result }: { result: AnalysisResult }) {
  const [expanded, setExpanded] = useState(false)

  const stages = useMemo(() => stageBreakdown(result.token_usage), [result.token_usage])
  const groups = useMemo(() => primaryGroups(result), [result])
  const peers = useMemo(() => peerGroups(result), [result])
  const averages = useMemo(
    () => spendAverages(result.token_usage?.total_cost_usd, analyzedFailingTests(result), groups, peers),
    [result, groups, peers],
  )

  const failedStages = stages.filter(s => s.failedCalls > 0)
  const failedCalls = failedStages.reduce((sum, s) => sum + s.failedCalls, 0)
  const wastedCost = failedStages.some(s => s.failedCostUsd == null)
    ? null
    : failedStages.reduce((sum, s) => sum + s.failedCostUsd!, 0)
  // Any stage can hold legacy calls with no recorded outcome, so the failed-call
  // count is a lower bound whenever a single stage's outcomes are unknown.
  const outcomeUnknown = stages.some(s => !s.outcomeKnown)
  const summedDuration = formatSummedDuration(result.token_usage?.total_duration_ms)

  if (stages.length === 0 && groups.length === 0) return null

  return (
    <div className="rounded-lg border border-border-muted animate-slide-up">
      <button
        type="button"
        className="flex w-full items-center gap-3 p-4 text-left"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown className="h-4 w-4 shrink-0 text-text-tertiary" /> : <ChevronRight className="h-4 w-4 shrink-0 text-text-tertiary" />}
        <Coins className="h-4 w-4 shrink-0 text-signal-green" />
        <h2 className="text-xs font-display uppercase tracking-widest text-text-tertiary">AI Spend</h2>
        <span className="ml-auto font-mono text-xs text-text-secondary">
          {stages.reduce((sum, s) => sum + s.calls, 0)} calls · <Cost value={result.token_usage?.total_cost_usd ?? null} />
        </span>
      </button>

      {expanded && (
        <div className="space-y-5 border-t border-border-muted p-4">
          <section className="space-y-2">
            <h3 className="text-[10px] font-display uppercase tracking-widest text-text-tertiary">By stage</h3>
            {stages.length > 0
              ? <StageTable stages={stages} />
              : <p className="text-xs text-text-tertiary">No recorded AI calls for this job.</p>}
            {result.token_usage?.cost_partial && (
              <p className="text-[10px] text-text-tertiary">
                Some AI turns had no catalog price, so this job's cost is a lower bound, not the full spend.
              </p>
            )}
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-display uppercase tracking-widest text-text-tertiary">Failed calls</h3>
            {failedCalls === 0
              ? (
                <p className="text-xs text-text-tertiary">
                  No failed AI calls recorded.
                  {outcomeUnknown && ' Some calls predate outcome tracking, so this is not proof that every call succeeded.'}
                </p>
              )
              : (
                <p className="flex items-start gap-2 text-xs text-signal-orange">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>
                    {failedCalls} failed call{failedCalls === 1 ? '' : 's'}
                    {outcomeUnknown && ' (lower bound — some calls have no recorded outcome)'}
                    {' · '}cost wasted: <Cost value={wastedCost} />
                    {' · '}{failedStages.map(s => s.callType).join(', ')}
                  </span>
                </p>
              )}
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-display uppercase tracking-widest text-text-tertiary">Primary usage by failure group</h3>
            {groups.length === 0
              ? <p className="text-xs text-text-tertiary">No failure groups in this job.</p>
              : (
                <table className="w-full">
                  <tbody>
                    {groups.map(group => <PrimaryGroupRow key={group.key} group={group} />)}
                  </tbody>
                </table>
              )}
          </section>

          {peers.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-display uppercase tracking-widest text-text-tertiary">Peer usage by round</h3>
              <p className="text-[10px] text-text-tertiary">
                The same calls shown in the peer/revision stages above, split by round and agent — not extra cost.
              </p>
              <table className="w-full">
                <tbody>
                  {peers.map(group => <PeerGroupRow key={group.key} group={group} />)}
                </tbody>
              </table>
            </section>
          )}

          <section className="space-y-1.5 border-t border-border-muted pt-3">
            <h3 className="text-[10px] font-display uppercase tracking-widest text-text-tertiary">Averages</h3>
            <AverageRow
              label="Cost per failing test"
              hint="Job total cost / analyzed failed tests. Includes shared job overhead (clone, cross-failure, agent routing) and may include peer usage."
              value={averages.costPerFailingTest}
            />
            <AverageRow
              label="Cost per unique failure group"
              hint={`Sum of attributable primary group costs / ${averages.groupsWithKnownCost} of ${averages.totalGroups} groups with a known cost. Groups with unavailable cost are excluded from both sides.`}
              value={averages.costPerUniqueGroup}
            />
            <AverageRow
              label="Peer cost per debated group"
              hint={`Sum of known peer debate costs / ${averages.peersWithKnownCost} of ${averages.debatedGroups} debated group(s) with a known cost; the rest are excluded from both sides.`}
              value={averages.peerCostPerDebatedGroup}
            />
          </section>

          {summedDuration && (
            <p className="text-[10px] text-text-tertiary">
              Summed AI call duration: {summedDuration} across calls — not wall-clock analysis time.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
