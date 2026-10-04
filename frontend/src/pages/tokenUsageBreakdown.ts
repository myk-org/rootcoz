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

/** Always a dollar total. A null cost means unpriced rows were counted as $0,
 *  so the floor is reported; `partial` labels it as incomplete rather than
 *  hiding the number. Genuine zero renders as $0.00, not a dash. */
export function formatCostCell(cost: number | null | undefined): string {
  return formatCost(cost ?? 0)
}

/** SQLite stores the flag as 0/1, so treat both truthy encodings as partial. */
export function isPartial(flag: boolean | number | undefined): boolean {
  return flag === true || flag === 1
}
