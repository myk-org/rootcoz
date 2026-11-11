// The one URL contract for job filters (#546). This file imports
// `@/lib/jobFilters` as its ONLY entry: a back-import from `filterPresets.ts`
// would throw `Cannot access 'JOB_FILTER_KEYS' before initialization` here,
// loudly, instead of in the app at runtime.
import { describe, it, expect } from 'vitest'
import {
  DEFAULT_TEST_NAME_MODE,
  EMPTY_JOB_FILTERS,
  JOB_FILTER_KEYS,
  TEST_NAME_FILTER_KEYS,
  applyJobFiltersToParams,
  jobFiltersFromFilters,
  jobFiltersFromParams,
  jobFiltersToApiQuery,
  jobFiltersToFilters,
  normalizeSubmitters,
  type JobFilterValues,
} from '@/lib/jobFilters'

const DASHBOARD_KEYS = [...JOB_FILTER_KEYS, ...TEST_NAME_FILTER_KEYS]

function params(entries: Record<string, string | string[]>): URLSearchParams {
  const p = new URLSearchParams()
  for (const [key, value] of Object.entries(entries)) {
    for (const v of Array.isArray(value) ? value : [value]) p.append(key, v)
  }
  return p
}

const values = (over: Partial<JobFilterValues> = {}): JobFilterValues => ({
  ...EMPTY_JOB_FILTERS,
  ...over,
})

describe('canonical keys', () => {
  it('lists the eleven dimensions once, in bar order', () => {
    expect(JOB_FILTER_KEYS).toEqual([
      'team',
      'tier',
      'version',
      'label',
      'exclude_label',
      'status',
      'review_status',
      'analysis_state',
      'submitted_by',
      'date_from',
      'date_to',
    ])
    expect(TEST_NAME_FILTER_KEYS).toEqual(['test_name', 'test_name_mode'])
  })
})

describe('URL round-trip', () => {
  it('reads back exactly what it wrote, leaving `search` alone', () => {
    const start = values({
      team: ['a', 'b'],
      status: ['failed'],
      review_status: 'reviewed',
      date_from: '2025-01-01',
      date_to: '2025-12-31',
      test_name: 'tests.login.test_one',
      test_name_mode: 'exact',
    })
    const url = applyJobFiltersToParams(new URLSearchParams('search=keepme'), start, DASHBOARD_KEYS)

    expect(url.get('search')).toBe('keepme')
    expect(url.getAll('team')).toEqual(['a', 'b'])
    expect(url.get('review_status')).toBe('reviewed')
    expect(jobFiltersFromParams(url, DASHBOARD_KEYS)).toEqual(start)
  })

  it('reads the legacy from/to aliases and rewrites them as canonical keys', () => {
    const url = applyJobFiltersToParams(
      new URLSearchParams('from=2025-01-01&to=2025-02-01'),
      jobFiltersFromParams(params({ from: '2025-01-01', to: '2025-02-01' }), DASHBOARD_KEYS),
      DASHBOARD_KEYS,
    )
    expect(url.get('date_from')).toBe('2025-01-01')
    expect(url.get('date_to')).toBe('2025-02-01')
    expect(url.get('from')).toBeNull()
    expect(url.get('to')).toBeNull()
  })

  it('deletes the aliases even when no dates are selected', () => {
    const url = applyJobFiltersToParams(
      new URLSearchParams('from=2025-01-01&team=a'),
      values({ team: ['a'] }),
      DASHBOARD_KEYS,
    )
    expect([...url.keys()]).toEqual(['team'])
  })
})

describe('preset normalisation', () => {
  it('normalises a preset saved with the legacy date keys', () => {
    const preset = { team: ['storage'], from: '2025-01-01', to: '2025-02-01' }
    expect(jobFiltersToFilters(jobFiltersFromFilters(preset))).toEqual({
      team: ['storage'],
      date_from: '2025-01-01',
      date_to: '2025-02-01',
    })
  })

  it('writes no test_name_mode without a committed name', () => {
    // A mode alone is not a filter: it must not reach the URL, and it must not
    // make a clean URL look dirty (FilterPresetsMenu's hasFilters).
    const onlyMode = jobFiltersFromFilters({ test_name_mode: 'exact' })
    expect(onlyMode.test_name).toBe('')
    expect(onlyMode.test_name_mode).toBe(DEFAULT_TEST_NAME_MODE)
    expect(jobFiltersToFilters(onlyMode)).toEqual({})

    const url = applyJobFiltersToParams(
      new URLSearchParams('test_name_mode=exact'),
      onlyMode,
      DASHBOARD_KEYS,
    )
    expect(url.get('test_name_mode')).toBeNull()
  })

  it('clearing the name clears the mode', () => {
    const cleared = values({ test_name: '', test_name_mode: 'ends_with' })
    expect(jobFiltersToFilters(cleared)).toEqual({})
  })

  it('writes the mode only when it differs from the default', () => {
    expect(jobFiltersToFilters(values({ test_name: 'x' }))).toEqual({ test_name: 'x' })
    expect(
      jobFiltersToFilters(values({ test_name: 'x', test_name_mode: 'starts_with' })),
    ).toEqual({ test_name: 'x', test_name_mode: 'starts_with' })
  })

  it('falls back to contains for an unknown stored mode', () => {
    expect(jobFiltersFromFilters({ test_name: 'x', test_name_mode: 'regex' }).test_name_mode).toBe(
      'contains',
    )
  })

  it('collapses the parked selectors, so a clean view is `{}`', () => {
    expect(jobFiltersToFilters(EMPTY_JOB_FILTERS)).toEqual({})
  })
})

describe('per-page API encoding', () => {
  const picked = values({
    team: ['a', 'b'],
    label: ['nightly'],
    exclude_label: ['flaky'],
    status: ['failed'],
    submitted_by: ['alice'],
    review_status: 'not_reviewed',
    date_from: '2025-01-01',
    test_name: 'login',
    test_name_mode: 'exact',
  })

  it('repeats list values for the dashboard endpoint', () => {
    const q = jobFiltersToApiQuery('dashboard', picked)
    expect(q.getAll('team')).toEqual(['a', 'b'])
    expect(q.getAll('label')).toEqual(['nightly'])
    expect(q.getAll('exclude_label')).toEqual(['flaky'])
    expect(q.getAll('submitted_by')).toEqual(['alice'])
    expect(q.get('test_name')).toBe('login')
    expect(q.get('test_name_mode')).toBe('exact')
  })

  it('sends CSV for the reports endpoint, renaming labels to tags', () => {
    const q = jobFiltersToApiQuery('reports', picked)
    expect(q.get('team')).toBe('a,b')
    expect(q.get('tags')).toBe('nightly')
    expect(q.get('exclude_tags')).toBe('flaky')
    expect(q.getAll('team')).toEqual(['a,b'])
    // The job-list dimension travels the same way on both pages: the reports
    // aggregate filters jobs by a failing test too.
    expect(q.get('test_name')).toBe('login')
    expect(q.get('test_name_mode')).toBe('exact')
  })

  it('omits the parked selectors from both pages', () => {
    const parked = values({ review_status: 'all', analysis_state: 'all' })
    for (const page of ['dashboard', 'reports'] as const) {
      const q = jobFiltersToApiQuery(page, parked)
      expect(q.get('review_status')).toBeNull()
      expect(q.get('analysis_state')).toBeNull()
    }
  })
})

describe('normalizeSubmitters', () => {
  it('folds onto the enumerated spelling so one selection is one chip', () => {
    expect(normalizeSubmitters(['ALICE', 'alice'], ['alice', 'bob'])).toEqual(['alice'])
    expect(normalizeSubmitters(['Änne'], ['Änne'])).toEqual(['Änne'])
  })

  it('keeps a spelling the server has but the list does not', () => {
    expect(normalizeSubmitters(['  bob  '], [])).toEqual(['bob'])
  })

  it('drops blanks, duplicates and over-long names', () => {
    expect(normalizeSubmitters(['', '   ', 'a', 'A'], [])).toEqual(['a'])
    expect(normalizeSubmitters(['x'.repeat(65)], [])).toEqual([])
    expect(normalizeSubmitters(['x'.repeat(64)], [])).toEqual(['x'.repeat(64)])
  })

  it('counts code points, not UTF-16 units, so an emoji name survives', () => {
    const emoji = '🙂'.repeat(64) // 128 UTF-16 units, 64 characters
    expect(normalizeSubmitters([emoji], [])).toEqual([emoji])
    expect(normalizeSubmitters(['🙂'.repeat(65)], [])).toEqual([])
  })

  it('caps the selection at 50', () => {
    const many = Array.from({ length: 55 }, (_, i) => `user${i}`)
    expect(normalizeSubmitters(many, [])).toHaveLength(50)
  })
})
