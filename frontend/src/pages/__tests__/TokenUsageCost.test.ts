import { describe, expect, it } from 'vitest'
import {
  aggregateJobCallTypes,
  compareBreakdownRows,
  type JobUsageRecord,
} from '../tokenUsageBreakdown'
import {
  formatCostCell,
  formatResolvedCost,
  isFloorUsage,
  lowerBoundSentence,
  LOWER_BOUND_FALLBACK_NOTE,
  pricedCallCount,
  resolveUsageCost,
  UNAVAILABLE_COST,
} from '@/lib/usageCost'

const row = (over: Partial<Parameters<typeof compareBreakdownRows>[0]> = {}) => ({
  group: 'g',
  calls: 1,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cost_usd: null as number | null,
  priced_calls: 0,
  cost_partial: false,
  avg_duration_ms: 0,
  ...over,
})

const rec = (over: Partial<JobUsageRecord> = {}): JobUsageRecord => ({
  call_type: 'primary',
  input_tokens: 10,
  output_tokens: 5,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cost_usd: 0.02,
  duration_ms: 100,
  ...over,
})

describe('aggregateJobCallTypes', () => {
  it('sums priced calls of the same type', () => {
    const [group] = aggregateJobCallTypes([rec(), rec({ cost_usd: 0.03 })])
    expect(group.calls).toBe(2)
    expect(group.cost_usd).toBeCloseTo(0.05)
    expect(group.avg_duration_ms).toBe(100)
  })

  it('keeps a priced call when an unpriced one precedes it', () => {
    // The unpriced call counts as $0 and is flagged; it must not discard the
    // priced spend that follows it.
    const [group] = aggregateJobCallTypes([rec({ cost_usd: null }), rec({ cost_usd: 0.02 })])
    expect(group.cost_usd).toBeCloseTo(0.02)
    expect(group.priced_calls).toBe(1)
    expect(group.cost_partial).toBe(true)
  })

  it('keeps a priced call when an unpriced one follows it', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_usd: 0.02 }), rec({ cost_usd: null })])
    expect(group.cost_usd).toBeCloseTo(0.02)
    expect(group.priced_calls).toBe(1)
    expect(group.cost_partial).toBe(true)
  })

  it('reports an all-unpriced group as a $0 floor', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_usd: null })])
    expect(group.cost_usd).toBeCloseTo(0)
    expect(group.priced_calls).toBe(0)
    expect(group.cost_partial).toBe(true)
  })

  it('treats a free model priced at $0 as a complete total', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_usd: 0 })])
    expect(group.cost_usd).toBeCloseTo(0)
    expect(group.priced_calls).toBe(1)
    expect(group.cost_partial).toBe(false)
  })

  it('propagates the partial flag without inventing a cost', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_partial: true })])
    expect(group.cost_partial).toBe(true)
    expect(group.cost_usd).toBeCloseTo(0.02)
    expect(group.priced_calls).toBe(1)
  })

  it('buckets a missing call type under unknown', () => {
    const groups = aggregateJobCallTypes([rec({ call_type: '' })])
    expect(groups[0].group).toBe('unknown')
  })

  it('normalizes SQLite integer 1 to a partial flag', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_partial: 1 })])
    expect(group.cost_partial).toBe(true)
  })

  it('treats an integer 0 flag as complete', () => {
    const [group] = aggregateJobCallTypes([rec({ cost_partial: 0 })])
    expect(group.cost_partial).toBe(false)
  })
})

describe('compareBreakdownRows by cost', () => {
  const priced = row({ cost_usd: 0.05 })
  const pricier = row({ cost_usd: 0.5 })
  const unknown = row({ cost_usd: null })

  it('sorts unknown-cost rows last in the default descending view', () => {
    const sorted = [unknown, priced, pricier].sort((a, b) => compareBreakdownRows(a, b, 'cost_usd', -1))
    expect(sorted.map(r => r.cost_usd)).toEqual([0.5, 0.05, null])
  })

  it('keeps unknown-cost rows last when ascending too', () => {
    const sorted = [unknown, priced, pricier].sort((a, b) => compareBreakdownRows(a, b, 'cost_usd', 1))
    expect(sorted.map(r => r.cost_usd)).toEqual([0.05, 0.5, null])
  })

  it('orders priced rows by amount in both directions', () => {    expect(compareBreakdownRows(pricier, priced, 'cost_usd', -1)).toBeLessThan(0)
    expect(compareBreakdownRows(priced, pricier, 'cost_usd', 1)).toBeLessThan(0)
  })
})

describe('formatCostCell', () => {
  it('renders a genuine zero as $0.00, not a dash', () => {
    expect(formatCostCell(0)).toBe('$0.00')
  })

  it('renders a null cost as unavailable rather than a free $0', () => {
    // A null aggregate total means unavailable, NOT unpriced-calls-counted-as-zero.
    // Coercing it to $0 would reintroduce the bug this reporting removed.
    expect(formatCostCell(null)).toBe(UNAVAILABLE_COST)
    expect(formatCostCell(undefined)).toBe(UNAVAILABLE_COST)
  })
})

describe('resolveUsageCost', () => {
  it('trusts a recorded total and its partial flag', () => {
    expect(resolveUsageCost({ total_cost_usd: 0.25, cost_partial: false })).toEqual({ value: 0.25, partial: false })
    expect(resolveUsageCost({ total_cost_usd: 0, cost_partial: false })).toEqual({ value: 0, partial: false })
    expect(resolveUsageCost({ total_cost_usd: 0.03, cost_partial: true })).toEqual({ value: 0.03, partial: true })
  })

  it('normalizes the integer flag SQLite returns', () => {
    expect(resolveUsageCost({ total_cost_usd: 0.03, cost_partial: 1 }).partial).toBe(true)
  })

  it('keeps a legacy null total unavailable when no calls survived', () => {
    expect(resolveUsageCost({ total_cost_usd: null, calls: [] })).toEqual({ value: null, partial: false })
    expect(resolveUsageCost(null)).toEqual({ value: null, partial: false })
  })

  it('derives a floor and a partial flag from a legacy null total with calls', () => {
    const resolved = resolveUsageCost({
      total_cost_usd: null,
      calls: [{ cost_usd: 0.4 }, { cost_usd: null }],
    })
    expect(resolved).toEqual({ value: 0.4, partial: true })
  })

  it('formats an unavailable figure as unavailable', () => {
    expect(formatResolvedCost(resolveUsageCost(null))).toBe(UNAVAILABLE_COST)
    expect(formatResolvedCost(resolveUsageCost({ total_cost_usd: 0.25 }))).toBe('$0.25')
  })
})

describe('lowerBoundSentence', () => {
  it('counts the calls that were EXCLUDED, not the ones priced', () => {
    // 2 priced of 3 total means ONE call was excluded.
    expect(lowerBoundSentence(3, 2)).toContain('1 of 3 calls had no recorded price')
  })

  it('reports 12 excluded from the real 339-of-351 regression', () => {
    expect(lowerBoundSentence(351, 339)).toContain('12 of 351 calls had no recorded price')
    expect(lowerBoundSentence(351, 339)).not.toContain('339 of 351 calls had no recorded price')
  })

  it('falls back to null when the split is unknown or complete', () => {
    expect(lowerBoundSentence(null, 2)).toBeNull()
    expect(lowerBoundSentence(3, null)).toBeNull()
    expect(lowerBoundSentence(3, 3)).toBeNull()
    expect(lowerBoundSentence(3, 4)).toBeNull()
  })

  it('exposes a generic note for when no split can be quoted', () => {
    expect(LOWER_BOUND_FALLBACK_NOTE).toContain('lower bound')
  })
})

describe('shared cost helpers live in lib, not a page module', () => {
  it('counts the calls that carry a real price', () => {
    expect(pricedCallCount({ total_cost_usd: 1, calls: [{ cost_usd: 0.5 }, { cost_usd: null }] })).toBe(1)
    expect(pricedCallCount({ total_cost_usd: 1, calls: [] })).toBe(0)
    expect(pricedCallCount(null)).toBe(0)
  })

  it('treats a floor or an absent summary as a floor', () => {
    expect(isFloorUsage({ total_cost_usd: 1, cost_partial: true })).toBe(true)
    // Stricter than display: an unpriced call inside a numeric total still makes the
    // figure unusable as a complete sum in an average.
    expect(isFloorUsage({ total_cost_usd: 1, calls: [{ cost_usd: null }] })).toBe(true)
    expect(isFloorUsage({ total_cost_usd: 1, cost_partial: false, calls: [{ cost_usd: 1 }] })).toBe(false)
    // No summary at all means nothing is known about the figure.
    expect(isFloorUsage(null)).toBe(true)
    // Display still trusts the stored total, which is the whole point of it.
    expect(resolveUsageCost({ total_cost_usd: 1, calls: [{ cost_usd: null }] })).toEqual({ value: 1, partial: false })
    expect(resolveUsageCost(null).value).toBeNull()
  })
})
