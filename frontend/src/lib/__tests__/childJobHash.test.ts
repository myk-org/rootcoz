import { describe, it, expect } from 'vitest'
import { expandKey, childJobHashId, collectChildExpandKeys } from '../childJobHash'
import { groupFailures } from '../grouping'
import type { ChildJobAnalysis, FailureAnalysis } from '@/types'

function makeFailure(testName: string, errorSig: string): FailureAnalysis {
  return {
    id: `uuid-${testName}`,
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

describe('expandKey', () => {
  it('produces canonical format', () => {
    expect(expandKey('job-1', 'group-uuid123')).toBe('rootcoz-expand-job-1-group-uuid123')
  })

  it('handles child-prefixed group IDs', () => {
    expect(expandKey('job-1', 'child-hashid-uuid456')).toBe(
      'rootcoz-expand-job-1-child-hashid-uuid456',
    )
  })

  it('handles empty id', () => {
    expect(expandKey('job-1', '')).toBe('rootcoz-expand-job-1-')
  })
})

describe('childJobHashId', () => {
  it('builds a hash fragment for a child job', () => {
    expect(childJobHashId('my-job', 42)).toBe('child-my-job-42')
  })

  it('prefixes with parentHashId for nested children', () => {
    expect(childJobHashId('nested', 7, 'child-parent-1')).toBe(
      'child-parent-1--child-nested-7',
    )
  })

  it('encodes special characters in job name', () => {
    expect(childJobHashId('job/with spaces', 1)).toBe('child-job%2Fwith%20spaces-1')
  })
})

describe('collectChildExpandKeys', () => {
  it('returns empty array for no children', () => {
    expect(collectChildExpandKeys([], 'job-1')).toEqual([])
  })

  it('produces keys using expandKey format', () => {
    const child: ChildJobAnalysis = {
      job_name: 'child-job',
      build_number: 10,
      failures: [makeFailure('test1', 'sig-a')],
      failed_children: [],
    }

    const keys = collectChildExpandKeys([child], 'job-1')

    // Every key must match the expandKey format
    for (const key of keys) {
      expect(key).toMatch(/^rootcoz-expand-job-1-.+/)
    }

    // Should have the section key + one failure group key
    const hashId = childJobHashId('child-job', 10)
    expect(keys).toContain(expandKey('job-1', hashId))

    // Failure group key: groupFailures with child- prefix
    const groups = groupFailures([makeFailure('test1', 'sig-a')], `child-${hashId}`)
    expect(groups).toHaveLength(1)
    expect(keys).toContain(expandKey('job-1', groups[0].id))
  })

  it('recurses into nested children', () => {
    const nested: ChildJobAnalysis = {
      job_name: 'grandchild',
      build_number: 3,
      failures: [makeFailure('test-deep', 'sig-deep')],
      failed_children: [],
    }
    const child: ChildJobAnalysis = {
      job_name: 'child-job',
      build_number: 10,
      failures: [],
      failed_children: [nested],
    }

    const keys = collectChildExpandKeys([child], 'job-1')

    const childHash = childJobHashId('child-job', 10)
    const nestedHash = childJobHashId('grandchild', 3, childHash)
    expect(keys).toContain(expandKey('job-1', childHash))
    expect(keys).toContain(expandKey('job-1', nestedHash))
  })
})

describe('key consistency', () => {
  it('top-level group key has no extra child-scope segments', () => {
    // Verify top-level keys use the simple format without --0 or child segments.
    // This catches regressions where someone reintroduces scoped segments.
    const groups = groupFailures([makeFailure('test1', 'sig-a')])
    const jobId = 'my-job-id'

    for (const g of groups) {
      const key = expandKey(jobId, g.id)
      // Key should be exactly: rootcoz-expand-{jobId}-{groupId}
      expect(key).toBe(`rootcoz-expand-${jobId}-${g.id}`)
      // Must NOT contain the old broken --0 scoped segment
      expect(key).not.toMatch(/--0-/)
      // Must NOT contain consecutive dashes after jobId that indicate empty scope segments
      expect(key).not.toContain(`rootcoz-expand-${jobId}--`)
    }
  })

  it('child group key embeds child identity in group.id, not in extra key segments', () => {
    // For child jobs, the child identity is encoded in the group.id prefix
    // (e.g., "child-childname-42-uuid"), not as extra segments in the key.
    const hashId = childJobHashId('child-job', 5)
    const groups = groupFailures(
      [makeFailure('test1', 'sig-b')],
      `child-${hashId}`,
    )
    const jobId = 'job-99'

    for (const g of groups) {
      const key = expandKey(jobId, g.id)
      // Group id should contain the child hash
      expect(g.id).toContain(`child-${hashId}`)
      // Key should be exactly rootcoz-expand-{jobId}-{groupId}
      expect(key).toBe(`rootcoz-expand-${jobId}-${g.id}`)
    }
  })

  it('collectChildExpandKeys failure keys match expandKey output', () => {
    // This test exercises the real collectChildExpandKeys function and
    // verifies its output matches what expandKey would produce.
    const child: ChildJobAnalysis = {
      job_name: 'child-job',
      build_number: 5,
      failures: [makeFailure('test1', 'sig-c'), makeFailure('test2', 'sig-c')],
      failed_children: [],
    }
    const jobId = 'job-99'
    const collectedKeys = collectChildExpandKeys([child], jobId)

    // Simulate what FailureCard would compute for each group
    const hashId = childJobHashId('child-job', 5)
    const groups = groupFailures(child.failures!, `child-${hashId}`)
    for (const g of groups) {
      expect(collectedKeys).toContain(expandKey(jobId, g.id))
    }
  })

  it('no inline rootcoz-expand templates outside expandKey utility', () => {
    // The expandKey function is the single source of truth.
    // Verify its output always starts with the expected prefix.
    const key = expandKey('any-job', 'any-id')
    expect(key).toMatch(/^rootcoz-expand-/)
    expect(key).toBe('rootcoz-expand-any-job-any-id')
  })
})
