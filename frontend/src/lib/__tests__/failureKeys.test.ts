import { describe, expect, it } from 'vitest'
import { countUnclassifiedFailures, countUnreviewedFailures } from '@/lib/failureKeys'
import type { ChildJobAnalysis, FailureAnalysis, ReviewState } from '@/types'

/** One failure with the classification the server stored for it. */
function makeFailure(testName: string, classification: string): FailureAnalysis {
  return {
    id: `${testName}-id`,
    test_name: testName,
    error: 'boom',
    analysis: { classification, pattern: '', affected_tests: [], details: '', artifacts_evidence: '' },
    error_signature: 'sig',
  }
}

function makeReview(reviewed: boolean): ReviewState {
  return { reviewed, username: 'user', updated_at: '2025-01-01T00:00:00Z' }
}

/** One child job with a single failing test at the given build. */
function makeChild(jobName: string, buildNumber: number): ChildJobAnalysis {
  return {
    id: `${jobName}-${buildNumber}`,
    job_name: jobName,
    build_number: buildNumber,
    jenkins_url: null,
    summary: null,
    failures: [
      {
        id: `${jobName}-${buildNumber}-f`,
        test_name: 'test-x',
        error: 'error-x',
        analysis: { classification: 'PRODUCT BUG', pattern: '', affected_tests: ['test-x'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-x',
      },
    ],
    failed_children: [],
    note: null,
  }
}

describe('countUnreviewedFailures', () => {
  it('counts a child failure unreviewed when no review exists', () => {
    expect(countUnreviewedFailures([], [makeChild('child', 5)], {})).toBe(1)
  })

  it('counts an exact-build child review as reviewed', () => {
    const reviews = { 'child#5::test-x': makeReview(true) }
    expect(countUnreviewedFailures([], [makeChild('child', 5)], reviews)).toBe(0)
  })

  it('covers the child with a wildcard (build 0) review, mirroring the server gate', () => {
    // A child test reviewed without a build number is stored under the
    // wildcard key — the server accepts it for any build of that child job.
    const reviews = { 'child#0::test-x': makeReview(true) }
    expect(countUnreviewedFailures([], [makeChild('child', 5)], reviews)).toBe(0)
  })

  it('lets the exact-build review win over a wildcard review', () => {
    // Exact key reviewed → reviewed, even when the wildcard entry is unreviewed.
    const reviews = {
      'child#5::test-x': makeReview(true),
      'child#0::test-x': makeReview(false),
    }
    expect(countUnreviewedFailures([], [makeChild('child', 5)], reviews)).toBe(0)
  })

  it('falls back to the wildcard review when the exact-build entry is not reviewed', () => {
    // resolve_failure_review skips non-completed exact entries and tries the
    // wildcard — the frontend count must match or the button blocks a push
    // the server would allow.
    const reviews = {
      'child#5::test-x': makeReview(false),
      'child#0::test-x': makeReview(true),
    }
    expect(countUnreviewedFailures([], [makeChild('child', 5)], reviews)).toBe(0)
  })

  it('does not cover a different build with that build\'s exact review', () => {
    // A review pinned to build 3 never covers the same test at build 5.
    const reviews = { 'child#3::test-x': makeReview(true) }
    expect(countUnreviewedFailures([], [makeChild('child', 5)], reviews)).toBe(1)
  })

  it('keeps a wildcard review scoped to its own child job and away from top-level tests', () => {
    const reviews = { 'child-a#0::test-x': makeReview(true) }
    // Different child job at the same test name: not covered
    expect(countUnreviewedFailures([], [makeChild('child-b', 5)], reviews)).toBe(1)
    // Top-level failure with the same test name: not covered either
    expect(
      countUnreviewedFailures(
        [{ test_name: 'test-x' }],
        [],
        reviews,
      ),
    ).toBe(1)
  })

  it('counts parent and child failures together, each through its own precedence', () => {
    const reviews = {
      'test-parent': makeReview(true),
      'child#0::test-x': makeReview(true),
    }
    const unreviewed = countUnreviewedFailures(
      [{ test_name: 'test-parent' }, { test_name: 'test-unreviewed' }],
      [makeChild('child', 7)],
      reviews,
    )
    expect(unreviewed).toBe(1)
  })

  it('lets a fully reviewed child through while the parent is unreviewed', () => {
    // The server gates a push against the subtree it exports; a reviewed
    // child must not be blocked by an unreviewed sibling or parent.
    const reviews = { 'child#5::test-x': makeReview(true) }
    const failures = [{ test_name: 'test-parent' }]
    const children = [makeChild('child', 5)]

    expect(countUnreviewedFailures(failures, children, reviews)).toBe(1)
    expect(
      countUnreviewedFailures(failures, children, reviews, {
        childJobName: 'child',
        childBuildNumber: 5,
      }),
    ).toBe(0)
  })

  it('finds a nested child scope', () => {
    const nested = makeChild('inner', 9)
    const outer = { ...makeChild('outer', 5), failures: [], failed_children: [nested] }
    const reviews = { 'inner#9::test-x': makeReview(true) }

    expect(
      countUnreviewedFailures([], [outer], reviews, {
        childJobName: 'inner',
        childBuildNumber: 9,
      }),
    ).toBe(0)
  })
})

describe('countUnclassifiedFailures', () => {
  it('counts failures whose stored classification is empty', () => {
    const failures = [makeFailure('test-a', ''), makeFailure('test-b', 'CODE ISSUE')]
    expect(countUnclassifiedFailures(failures, [], {})).toBe(1)
  })

  it('treats a whitespace-only classification as empty, as the server does', () => {
    // storage._effective_classification strips before deciding.
    expect(countUnclassifiedFailures([makeFailure('test-a', '  ')], [], {})).toBe(1)
  })

  it('lets a live reviewer override clear a failure without a refetch', () => {
    // The banner is derived from this count, so a classification the reviewer
    // just saved must clear it immediately.
    const failures = [makeFailure('test-a', '')]
    expect(
      countUnclassifiedFailures(failures, [], { 'test-a': 'PRODUCT BUG' }),
    ).toBe(0)
  })

  it('counts child failures through their own review key', () => {
    const child = { ...makeChild('child', 5), failures: [makeFailure('test-x', '')] }
    expect(countUnclassifiedFailures([], [child], {})).toBe(1)
    expect(
      countUnclassifiedFailures([], [child], { 'child#5::test-x': 'CODE ISSUE' }),
    ).toBe(0)
    // A top-level override for the same test name never covers a child scope.
    expect(countUnclassifiedFailures([], [child], { 'test-x': 'CODE ISSUE' })).toBe(1)
  })

  it('never counts an ingest-only job, whose analysis has not run yet', () => {
    // Same empty classifications as a failed analysis, but the model was never
    // asked — mirrors the server's `incomplete_failures` analysis-state gate.
    const failures = [makeFailure('test-a', ''), makeFailure('test-b', '')]
    expect(countUnclassifiedFailures(failures, [], {}, 'submitted')).toBe(0)
    // ...and the same failures after an analysis that produced nothing do count.
    expect(countUnclassifiedFailures(failures, [], {}, 'analyzed')).toBe(2)
  })
})
