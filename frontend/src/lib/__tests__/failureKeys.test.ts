import { describe, it, expect } from 'vitest'
import { expandStateKey } from '../failureKeys'
import { collectChildExpandKeys } from '../childJobHash'
import type { ChildJobAnalysis, FailureAnalysis } from '@/types'

function makeFailure(id: string, testName: string): FailureAnalysis {
  return {
    id,
    test_name: testName,
    error: 'some error',
    error_signature: `sig-${testName}`,
    analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: [], details: '', artifacts_evidence: '' },
  }
}

function makeChild(jobName: string, buildNumber: number, failures: FailureAnalysis[]): ChildJobAnalysis {
  return { id: `${jobName}-${buildNumber}`, job_name: jobName, build_number: buildNumber, jenkins_url: null, summary: null, note: null, failures, failed_children: [] }
}

describe('expandStateKey', () => {
  it('top-level key matches FailureCard default format', () => {
    expect(expandStateKey('my-job', '', 0, 'group-1')).toBe('rootcoz-expand-my-job--0-group-1')
  })

  it('child key includes job name and build number', () => {
    expect(expandStateKey('my-job', 'child-job', 42, 'group-1')).toBe('rootcoz-expand-my-job-child-job-42-group-1')
  })
})

describe('collectChildExpandKeys', () => {
  it('failure-card keys match expandStateKey for a child job', () => {
    const child = makeChild('child-job', 42, [makeFailure('some-uuid', 'TestFoo')])
    const keys = collectChildExpandKeys([child], 'my-job')
    // hashId = child-child-job-42, group prefix = child-child-child-job-42
    const groupId = 'child-child-child-job-42-some-uuid'
    expect(keys).toContain(expandStateKey('my-job', 'child-job', 42, groupId))
  })
})
