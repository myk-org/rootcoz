import { describe, it, expect } from 'vitest'
import { collectPushScopes, pushScopeQuery } from '../pushScopes'
import type { ChildJobAnalysis } from '@/types'

function makeChild(overrides: Partial<ChildJobAnalysis> = {}): ChildJobAnalysis {
  return {
    id: 'child-1',
    job_name: 'child-job',
    build_number: 1,
    jenkins_url: null,
    summary: null,
    failures: [
      {
        id: 'f-child',
        test_name: 'child-test',
        error: 'err',
        analysis: {
          classification: 'CODE ISSUE',
          pattern: '',
          affected_tests: [],
          details: '',
          artifacts_evidence: '',
        },
        error_signature: 'sig-c',
      },
    ],
    failed_children: [],
    note: null,
    ...overrides,
  }
}

describe('collectPushScopes', () => {
  it('collects the parent scope and each child scope with failures', () => {
    const scopes = collectPushScopes(
      [{ test_name: 'parent-test' }],
      [makeChild(), makeChild({ job_name: 'other', build_number: 2 })],
    )
    expect(scopes).toEqual([
      {},
      { childJobName: 'child-job', childBuildNumber: 1 },
      { childJobName: 'other', childBuildNumber: 2 },
    ])
  })

  it('skips nodes without failures', () => {
    const scopes = collectPushScopes(
      [],
      [makeChild({ failures: [] }), makeChild({ job_name: 'with-failures', build_number: 3 })],
    )
    expect(scopes).toEqual([{ childJobName: 'with-failures', childBuildNumber: 3 }])
  })

  it('deduplicates child nodes sharing job name and build number', () => {
    // The same child job (dup#7) reported under two different parents —
    // both nodes have failures but must yield a single push scope, or the
    // backend resolves both identical requests to the first node and the
    // second is never exported.
    const scopes = collectPushScopes([], [
      makeChild({ job_name: 'dup', build_number: 7 }),
      makeChild({
        job_name: 'parent-b',
        build_number: 8,
        failed_children: [makeChild({ job_name: 'dup', build_number: 7 })],
      }),
    ])
    expect(scopes).toEqual([
      { childJobName: 'dup', childBuildNumber: 7 },
      { childJobName: 'parent-b', childBuildNumber: 8 },
    ])
  })

  it('keeps child nodes with the same job name but different build numbers', () => {
    const scopes = collectPushScopes([], [
      makeChild({ job_name: 'dup', build_number: 7 }),
      makeChild({ job_name: 'dup', build_number: 9 }),
    ])
    expect(scopes).toEqual([
      { childJobName: 'dup', childBuildNumber: 7 },
      { childJobName: 'dup', childBuildNumber: 9 },
    ])
  })
})

describe('pushScopeQuery', () => {
  it('returns an empty string for the parent scope', () => {
    expect(pushScopeQuery({})).toBe('')
  })

  it('builds child query params', () => {
    expect(pushScopeQuery({ childJobName: 'child-job', childBuildNumber: 42 })).toBe(
      '?child_job_name=child-job&child_build_number=42',
    )
  })
})
