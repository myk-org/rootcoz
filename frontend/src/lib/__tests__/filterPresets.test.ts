import { describe, it, expect } from 'vitest'
import {
  applyFiltersToParams,
  filtersEqual,
  filtersToQueryString,
  searchParamsToFilters,
} from '../filterPresets'

function params(entries: Record<string, string | string[]>): URLSearchParams {
  const p = new URLSearchParams()
  for (const [key, value] of Object.entries(entries)) {
    for (const v of Array.isArray(value) ? value : [value]) p.append(key, v)
  }
  return p
}

describe('searchParamsToFilters', () => {
  it('collects allowlisted keys, collapsing single values', () => {
    const sp = params({ team: ['a', 'b'], tier: ['2'], search: ['zzz'], bogus: ['x'] })
    expect(searchParamsToFilters(sp, ['team', 'tier'])).toEqual({
      team: ['a', 'b'],
      tier: '2',
    })
  })

  it('skips empty values', () => {
    const sp = params({ team: [''] })
    expect(searchParamsToFilters(sp, ['team'])).toEqual({})
  })
})

describe('applyFiltersToParams', () => {
  it('replaces allowlisted keys and leaves others untouched', () => {
    const sp = params({ team: ['old'], status: ['failed'], search: ['keepme'] })
    const next = applyFiltersToParams(sp, { team: ['new'], tier: '2' }, ['team', 'tier', 'status'])
    expect(next.getAll('team')).toEqual(['new'])
    expect(next.get('tier')).toBe('2')
    expect(next.get('status')).toBeNull()
    expect(next.get('search')).toBe('keepme')
  })

  it('drops keys not in the allowlist', () => {
    const next = applyFiltersToParams(new URLSearchParams(), { bogus: 'x' }, ['team'])
    expect(next.toString()).toBe('')
  })
})

describe('filtersToQueryString', () => {
  it('expands lists into repeated params', () => {
    expect(filtersToQueryString({ status: ['failed', 'aborted'], tier: '2' })).toBe(
      'status=failed&status=aborted&tier=2',
    )
  })
})

describe('filtersEqual', () => {
  it('matches identical maps regardless of list order', () => {
    expect(filtersEqual({ team: ['a', 'b'] }, { team: ['b', 'a'] })).toBe(true)
  })

  it('distinguishes different keys, values, and arities', () => {
    expect(filtersEqual({ team: ['a'] }, { tier: ['a'] })).toBe(false)
    expect(filtersEqual({ team: ['a'] }, { team: ['a', 'b'] })).toBe(false)
    expect(filtersEqual({ team: 'a' }, { team: 'b' })).toBe(false)
    expect(filtersEqual({ team: 'a' }, { tier: 'a', team: 'a' })).toBe(false)
  })

  it('treats a scalar and its single-item list as equal', () => {
    expect(filtersEqual({ team: 'a' }, { team: ['a'] })).toBe(true)
  })
})
