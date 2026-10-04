import { describe, expect, it } from 'vitest'
import {
  aggregateJobCallTypes,
  compareBreakdownRows,
  type JobUsageRecord,
} from '../tokenUsageBreakdown'

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

  it('orders priced rows by amount in both directions', () => {
    expect(compareBreakdownRows(pricier, priced, 'cost_usd', -1)).toBeLessThan(0)
    expect(compareBreakdownRows(priced, pricier, 'cost_usd', 1)).toBeLessThan(0)
  })
})
