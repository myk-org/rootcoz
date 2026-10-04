import { formatCost } from '@/lib/format'

export interface BreakdownRow {
  group: string
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  /** Floor of the group's spend: unpriced calls count as $0, so a total is
   *  always shown. `priced_calls` below `calls` means it is incomplete. */
  cost_usd: number | null
  /** Calls with a recorded price; fewer than `calls` means `cost_usd` is a floor. */
  priced_calls: number
  /** True when any call's recorded cost covers only some turns. */
  cost_partial: boolean
  avg_duration_ms: number
}

export interface JobUsageRecord {
  call_type: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number | null
  /** Raw endpoint value: SQLite stores 0/1, the aggregates report a boolean. */
  cost_partial?: boolean | number
  duration_ms: number
}

/** Aggregate a job's per-call records by call type for the expanded job row.
 *
 *  An unpriced call counts as $0 and is tracked in the row's `priced_calls`, so
 *  the group total is always a number. `cost_partial` marks a total that
 *  excluded an unpriced call, keeping a floor from reading as a complete sum. */
export function aggregateJobCallTypes(records: JobUsageRecord[]): BreakdownRow[] {
  const byType = new Map<string, BreakdownRow>()
  for (const r of records) {
    const key = r.call_type || 'unknown'
    const existing = byType.get(key) || {
      group: key, calls: 0, input_tokens: 0, output_tokens: 0,
      cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd: 0 as number | null, priced_calls: 0, cost_partial: false, avg_duration_ms: 0,
    }
    existing.calls += 1
    existing.input_tokens += r.input_tokens
    existing.output_tokens += r.output_tokens
    existing.cache_read_tokens += r.cache_read_tokens || 0
    existing.cache_write_tokens += r.cache_write_tokens || 0
    // Unpriced calls contribute $0 but stay out of priced_calls, so the total
    // reads as a floor rather than a complete spend.
    existing.cost_usd = (existing.cost_usd ?? 0) + (r.cost_usd ?? 0)
    if (r.cost_usd != null) existing.priced_calls += 1
    existing.cost_partial = existing.cost_partial || isPartial(r.cost_partial) || r.cost_usd == null
    existing.avg_duration_ms += r.duration_ms || 0
    byType.set(key, existing)
  }
  return [...byType.values()].map(row => ({
    ...row,
    avg_duration_ms: row.calls > 0 ? row.avg_duration_ms / row.calls : 0,
  }))
}

/** Sort breakdown rows. Unknown-cost rows always sort last: an unavailable cost
 *  is not free, and flipping that placement by direction would put them first in
 *  the default descending view. */
export function compareBreakdownRows(
  a: BreakdownRow,
  b: BreakdownRow,
  sortKey: string,
  dir: 1 | -1,
): number {
  if (sortKey === 'cost_usd' && (a.cost_usd == null || b.cost_usd == null)) {
    return (a.cost_usd == null ? 1 : 0) - (b.cost_usd == null ? 1 : 0)
  }
  const cmp = compareNumbers(a, b, sortKey)
  return cmp === null ? 0 : cmp * dir
}

function compareNumbers(a: BreakdownRow, b: BreakdownRow, key: string): number | null {
  switch (key) {
    case 'input_tokens': return a.input_tokens - b.input_tokens
    case 'output_tokens': return a.output_tokens - b.output_tokens
    case 'cache_read_tokens': return a.cache_read_tokens - b.cache_read_tokens
    case 'cache_write_tokens': return a.cache_write_tokens - b.cache_write_tokens
    case 'avg_duration_ms': return a.avg_duration_ms - b.avg_duration_ms
    // both costs are numbers here — the null case is handled by the caller
    case 'cost_usd': return (a.cost_usd ?? 0) - (b.cost_usd ?? 0)
    default: return null
  }
}

/** A cost summary as stored on a usage record. */
export interface UsageCost {
  total_cost_usd?: number | null
  cost_partial?: boolean | number
  calls?: { cost_usd: number | null; cost_partial?: boolean | number }[]
}

/** A cost the UI can actually show, and whether it is only a floor. */
export interface ResolvedCost {
  /** null means *unavailable* — no figure may be shown, not even $0. */
  value: number | null
  partial: boolean
}

/** Resolve a stored usage summary into a displayable cost.
 *
 *  A present `total_cost_usd` is authoritative: the backend sums the priced calls,
 *  counts unpriced ones as $0, and flags the result `cost_partial`.
 *
 *  Legacy rows predate that contract and stored `NULL`, which means *unavailable*
 *  and must never read as a free $0 — that is the exact state this reporting
 *  removed. Those rows still carry their per-call records, so the floor is
 *  derivable from them. With no recorded calls there is nothing to derive and the
 *  figure stays unavailable. */
export function resolveUsageCost(usage: UsageCost | null | undefined): ResolvedCost {
  if (!usage) return { value: null, partial: false }
  if (usage.total_cost_usd != null) {
    return { value: usage.total_cost_usd, partial: isPartial(usage.cost_partial) }
  }
  const calls = usage.calls ?? []
  if (calls.length === 0) return { value: null, partial: false }
  let total = 0
  for (const call of calls) {
    if (call.cost_usd != null) total += call.cost_usd
  }
  return {
    value: total,
    partial: calls.some(call => isPartial(call.cost_partial) || call.cost_usd == null),
  }
}

/** An unavailable figure has no honest dollar rendering — it is not $0. */
export const UNAVAILABLE_COST = 'Unavailable'

/** Render a resolved cost. A numeric floor is always shown; an unavailable figure
 *  stays unavailable rather than masquerading as a free $0. */
export function formatResolvedCost(resolved: ResolvedCost): string {
  return resolved.value == null ? UNAVAILABLE_COST : formatCost(resolved.value)
}

/** Used when the priced/total split is unknown, so no call count can be quoted. */
export const LOWER_BOUND_FALLBACK_NOTE =
  'Partial: some AI turns had no catalog price, so this is a lower bound.'

/** The sentence explaining a numeric cost floor, or null when the split is unknown.
 *
 *  `pricedCalls` counts the calls that contributed a real price, so the calls that
 *  were *excluded* are the difference — reporting the priced count as the unpriced
 *  one inverts the disclosure entirely. */
export function lowerBoundSentence(totalCalls?: number | null, pricedCalls?: number | null): string | null {
  if (totalCalls == null || pricedCalls == null || pricedCalls >= totalCalls) return null
  return `${totalCalls - pricedCalls} of ${totalCalls} calls had no recorded price and were counted as $0, so this is a lower bound.`
}

/** A cost total from an aggregate: always a number, `null` only for a legacy row
 *  that recorded no spend at all. Genuine zero renders as $0.00, not a dash. */
export function formatCostCell(cost: number | null | undefined): string {
  return cost == null ? UNAVAILABLE_COST : formatCost(cost)
}

/** SQLite stores the flag as 0/1, so treat both truthy encodings as partial. */
export function isPartial(flag: boolean | number | undefined): boolean {
  return flag === true || flag === 1
}
