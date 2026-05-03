import { describe, it, expect } from 'vitest'
import { groupingKey, groupFailures } from '../grouping'
import type { FailureAnalysis } from '@/types'

function makeFailure(testName: string, errorSig: string): FailureAnalysis {
  return {
    test_name: testName,
    error: 'some error',
    analysis: {
      classification: 'CODE ISSUE',
      affected_tests: [],
      details: '',
      artifacts_evidence: '',
    },
    error_signature: errorSig,
  }
}

describe('groupingKey', () => {
  it('returns error_signature when present', () => {
    const f = makeFailure('test1', 'sig-abc')
    expect(groupingKey(f)).toBe('sig-abc')
  })

  it('falls back to unique-{test_name} when no signature', () => {
    const f = makeFailure('test1', '')
    expect(groupingKey(f)).toBe('unique-test1')
  })
})

describe('groupFailures', () => {
  it('groups failures by signature', () => {
    const failures = [
      makeFailure('test1', 'sig-a'),
      makeFailure('test2', 'sig-a'),
      makeFailure('test3', 'sig-b'),
    ]
    const groups = groupFailures(failures)
    expect(groups).toHaveLength(2)
    expect(groups[0].count).toBe(2)
    expect(groups[0].tests).toHaveLength(2)
    expect(groups[1].count).toBe(1)
  })

  it('treats empty signature as unique per test', () => {
    const failures = [
      makeFailure('test1', ''),
      makeFailure('test2', ''),
    ]
    const groups = groupFailures(failures)
    expect(groups).toHaveLength(2)
  })

  it('returns empty array for no failures', () => {
    expect(groupFailures([])).toEqual([])
  })

  it('applies prefix to group IDs', () => {
    const failures = [makeFailure('test1', 'sig-a')]
    const groups = groupFailures(failures, 'child')
    expect(groups[0].id).toBe('child-sig-a')
  })
})
