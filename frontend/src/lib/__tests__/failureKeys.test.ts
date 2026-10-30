import { describe, expect, it } from 'vitest'
import { countUnreviewedFailures } from '@/lib/failureKeys'
import type { ChildJobAnalysis, ReviewState } from '@/types'

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
