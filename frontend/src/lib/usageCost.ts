import { formatCost } from '@/lib/format'

/**
 * Shared AI-spend cost resolution and disclosure.
 *
 * Both a shared badge and the report page render the same cost figures, so these
 * rules live here rather than in a page module: what a stored cost means, when a
 * figure is only a floor, and how the excluded calls are counted. A change to cost
 * disclosure has one place to live, so every surface tells the same story.
 */

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

/** SQLite stores the flag as 0/1, so treat both truthy encodings as partial. */
export function isPartial(flag: boolean | number | undefined): boolean {
  return flag === true || flag === 1
}

/** An unavailable figure has no honest dollar rendering — it is not $0. */
export const UNAVAILABLE_COST = 'Unavailable'

/** Used when the priced/total split is unknown, so no call count can be quoted. */
export const LOWER_BOUND_FALLBACK_NOTE =
  'Partial: some AI turns had no catalog price, so this is a lower bound.'

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

/** Whether a usage summary's cost is a floor rather than a complete sum.
 *
 *  Deliberately stricter than the figure `resolveUsageCost` displays. For display we
 *  trust a stored total and its backend flag; but a summary that still holds an
 *  unpriced call cannot be averaged in as a complete sum even when its total is
 *  numeric, because the backend's flag may predate that call. An absent summary
 *  means nothing is known about it, so it counts as a floor too. */
export function isFloorUsage(usage: UsageCost | null | undefined): boolean {
  if (!usage) return true
  if (isPartial(usage.cost_partial)) return true
  return (usage.calls ?? []).some(call => isPartial(call.cost_partial) || call.cost_usd == null)
}

/** Render a resolved cost. A numeric floor is always shown; an unavailable figure
 *  stays unavailable rather than masquerading as a free $0. */
export function formatResolvedCost(resolved: ResolvedCost): string {
  return resolved.value == null ? UNAVAILABLE_COST : formatCost(resolved.value)
}

/** A cost total from an aggregate: always a number, `null` only for a legacy row
 *  that recorded no spend at all. Genuine zero renders as $0.00, not a dash. */
export function formatCostCell(cost: number | null | undefined): string {
  return cost == null ? UNAVAILABLE_COST : formatCost(cost)
}

/** The sentence explaining a numeric cost floor, or null when the split is unknown.
 *
 *  `pricedCalls` counts the calls that contributed a real price, so the calls that
 *  were *excluded* are the difference — reporting the priced count as the unpriced
 *  one inverts the disclosure entirely. */
export function lowerBoundSentence(totalCalls?: number | null, pricedCalls?: number | null): string | null {
  if (totalCalls == null || pricedCalls == null || pricedCalls >= totalCalls) return null
  return `${totalCalls - pricedCalls} of ${totalCalls} calls had no recorded price and were counted as $0, so this is a lower bound.`
}

/** How many calls in a usage summary carry a real price. */
export function pricedCallCount(usage: UsageCost | null | undefined): number {
  return (usage?.calls ?? []).filter(call => call.cost_usd != null).length
}
