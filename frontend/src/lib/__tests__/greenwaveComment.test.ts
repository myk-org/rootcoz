import { describe, it, expect } from 'vitest'
import { proposeWaiverComment } from '../greenwaveComment'
import type { AnalysisResult, FailureAnalysis } from '@/types'

function makeFailure(classification: string | null, testName: string): FailureAnalysis {
  return {
    id: `f-${testName}`,
    test_name: testName,
    error: 'err',
    analysis: {
      classification: classification ?? '',
      pattern: '',
      affected_tests: [testName],
      details: '',
      artifacts_evidence: '',
    },
    error_signature: `sig-${testName}`,
  } as FailureAnalysis
}

describe('proposeWaiverComment', () => {
  it('produces the full deterministic format with sorted classification counts', () => {
    const failures = [
      makeFailure('PRODUCT BUG', 'a'),
      makeFailure('CODE ISSUE', 'b'),
      makeFailure('PRODUCT BUG', 'c'),
    ]
    expect(proposeWaiverComment({ jobName: 'my-job', buildNumber: 42, failures })).toBe(
      'rootcoz: 3 failures reviewed for my-job #42 — PRODUCT BUG: 2, CODE ISSUE: 1',
    )
  })

  it('omits the build part when buildNumber is null, empty or missing', () => {
    const failures = [makeFailure('PRODUCT BUG', 'a')]
    const expected = 'rootcoz: 1 failure reviewed for my-job — PRODUCT BUG: 1'
    expect(proposeWaiverComment({ jobName: 'my-job', buildNumber: null, failures })).toBe(expected)
    expect(proposeWaiverComment({ jobName: 'my-job', buildNumber: '', failures })).toBe(expected)
    expect(proposeWaiverComment({ jobName: 'my-job', failures })).toBe(expected)
  })

  it('ends after the reviewed count when no failure carries a classification', () => {
    const failures = [makeFailure(null, 'a'), makeFailure('', 'b')]
    expect(proposeWaiverComment({ jobName: 'my-job', buildNumber: 7, failures })).toBe(
      'rootcoz: 2 failures reviewed for my-job #7',
    )
  })

  it('appends unclassified count when only some failures are classified', () => {
    const failures = [makeFailure('PRODUCT BUG', 'a'), makeFailure(null, 'b'), makeFailure('PRODUCT BUG', 'c')]
    expect(proposeWaiverComment({ jobName: 'my-job', buildNumber: 1, failures })).toBe(
      'rootcoz: 3 failures reviewed for my-job #1 — PRODUCT BUG: 2, unclassified: 1',
    )
  })

  it('orders equal counts by name ascending and is stable across calls', () => {
    const failures = [
      makeFailure('INFRASTRUCTURE', 'a'),
      makeFailure('CODE ISSUE', 'b'),
      makeFailure('PRODUCT BUG', 'c'),
    ]
    const first = proposeWaiverComment({ jobName: 'my-job', buildNumber: 2, failures })
    const second = proposeWaiverComment({ jobName: 'my-job', buildNumber: 2, failures })
    expect(first).toBe(second)
    expect(first).toBe(
      'rootcoz: 3 failures reviewed for my-job #2 — CODE ISSUE: 1, INFRASTRUCTURE: 1, PRODUCT BUG: 1',
    )
  })

  it('uppercases classification labels and pluralises correctly', () => {
    const failures: AnalysisResult['failures'] = [makeFailure('product bug', 'a')]
    expect(proposeWaiverComment({ jobName: 'j', buildNumber: 0, failures })).toBe(
      'rootcoz: 1 failure reviewed for j #0 — PRODUCT BUG: 1',
    )
  })

  it('counts the effective (overridden) classification, not the AI label', () => {
    // The report page renders classifications[key] ?? analysis.classification;
    // the approved text must describe the same thing the gating write carries.
    const failures = [makeFailure('PRODUCT BUG', 'a'), makeFailure('PRODUCT BUG', 'b')]
    expect(
      proposeWaiverComment({
        jobName: 'my-job',
        buildNumber: 9,
        failures,
        classifications: { a: 'CODE ISSUE', b: 'CODE ISSUE' },
      }),
    ).toBe('rootcoz: 2 failures reviewed for my-job #9 — CODE ISSUE: 2')
  })

  it('resolves overrides through the child-scoped review key', () => {
    const failures = [makeFailure('PRODUCT BUG', 'a'), makeFailure('PRODUCT BUG', 'b')]
    expect(
      proposeWaiverComment({
        jobName: 'child-job',
        buildNumber: 5,
        failures,
        childJobName: 'child-job',
        childBuildNumber: 5,
        classifications: {
          'child-job#5::a': 'INFRASTRUCTURE',
          // A bare-name override must NOT apply to a child-scoped failure.
          b: 'CODE ISSUE',
        },
      }),
    ).toBe('rootcoz: 2 failures reviewed for child-job #5 — INFRASTRUCTURE: 1, PRODUCT BUG: 1')
  })
})
