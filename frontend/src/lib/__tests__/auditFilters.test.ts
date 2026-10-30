import { describe, expect, it } from 'vitest'
import {
  EMPTY_FILTERS,
  activeTimePreset,
  detailsSearchTexts,
  matchesFilters,
  presetSince,
  type AuditEvent,
} from '@/lib/auditFilters'

function event(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    created_at: '2024-01-02 10:00:00',
    actor: 'alice',
    actor_role: 'admin',
    is_admin: true,
    action: 'settings.update',
    target_type: 'setting',
    target_id: 'ai_call_timeout',
    outcome: 'success',
    details: { key: 'ai_call_timeout', changed: true },
    request_ip: '10.0.0.7',
    ...overrides,
  }
}

describe('matchesFilters', () => {
  it('treats a date-only until as the whole day and matches since inclusively', () => {
    expect(
      matchesFilters(event({ created_at: '2024-01-02 23:59:59' }), {
        ...EMPTY_FILTERS,
        until: '2024-01-02',
      }),
    ).toBe(true)
    expect(
      matchesFilters(event({ created_at: '2024-01-03 00:00:00' }), {
        ...EMPTY_FILTERS,
        until: '2024-01-02',
      }),
    ).toBe(false)
    expect(
      matchesFilters(event({ created_at: '2024-01-02 00:00:00' }), {
        ...EMPTY_FILTERS,
        since: '2024-01-02',
      }),
    ).toBe(true)
  })

  it('matches a live (T-separated) event on a date-only until bound and rejects the next day', () => {
    const today = new Date().toISOString().slice(0, 10)
    expect(
      matchesFilters(event({ created_at: `${today}T23:00:00Z` }), {
        ...EMPTY_FILTERS,
        until: today,
      }),
    ).toBe(true)

    const tomorrow = new Date(Date.parse(`${today}T00:00:00Z`) + 86_400_000)
      .toISOString()
      .slice(0, 10)
    expect(
      matchesFilters(event({ created_at: `${tomorrow}T00:00:01Z` }), {
        ...EMPTY_FILTERS,
        until: today,
      }),
    ).toBe(false)
  })

  it('compares an unchanged since/until pair alike for spaced and T-separated stamps', () => {
    const filters = { ...EMPTY_FILTERS, since: '2024-01-02', until: '2024-01-02' }
    expect(matchesFilters(event({ created_at: '2024-01-02 10:00:00' }), filters)).toBe(true)
    expect(matchesFilters(event({ created_at: '2024-01-02T10:00:00Z' }), filters)).toBe(true)
    expect(matchesFilters(event({ created_at: '2024-01-01T23:59:59Z' }), filters)).toBe(false)
    expect(matchesFilters(event({ created_at: '2024-01-03T00:00:00Z' }), filters)).toBe(false)
  })

  it('matches q case-insensitively inside the serialized details', () => {
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'AI_CALL_TIMEOUT' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'not-there' })).toBe(false)
  })

  it('searches actor, target id and action the way the widened server search does', () => {
    // A streamed row is filtered here too: it must survive the same `q` that
    // found it on the server, wherever the needle sits.
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'ALICE' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'ai_call_TIMEOUT' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'settings.update' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'carol' })).toBe(false)
  })

  it('matches q written with the server-side (spaced) JSON or the compact JSON', () => {
    const e = event({ details: { key: 'value' } })
    expect(matchesFilters(e, { ...EMPTY_FILTERS, q: '"key": "value"' })).toBe(true)
    expect(matchesFilters(e, { ...EMPTY_FILTERS, q: '"key":"value"' })).toBe(true)
  })

  it('offers both serializations of details for search', () => {
    expect(detailsSearchTexts({ key: 'value' })).toEqual(['{"key":"value"}', '{"key": "value"}'])
    expect(detailsSearchTexts({ key: 'value' })).toHaveLength(2)
  })

  it('applies actor, action set and outcome the way the server does', () => {
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, actor: 'ALICE' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, actor: 'bob' })).toBe(false)
    expect(
      matchesFilters(event(), { ...EMPTY_FILTERS, action: 'user.role.change, settings.update' }),
    ).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, action: 'user.role.change' })).toBe(false)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, outcome: 'denied' })).toBe(false)
  })

  it('keeps a pinned row alone in the view', () => {
    expect(matchesFilters(event({ id: 42 }), { ...EMPTY_FILTERS, event: '42' })).toBe(true)
    expect(matchesFilters(event({ id: 43 }), { ...EMPTY_FILTERS, event: '42' })).toBe(false)
  })
})

describe('activeTimePreset', () => {
  const now = Date.parse('2024-03-04T12:00:00Z')

  it('names the preset the URL was written with', () => {
    expect(activeTimePreset({ ...EMPTY_FILTERS, since: presetSince(1, now) }, now)).toBe('1h')
    expect(activeTimePreset({ ...EMPTY_FILTERS, since: presetSince(24, now) }, now)).toBe('24h')
    expect(activeTimePreset({ ...EMPTY_FILTERS, since: presetSince(7 * 24, now) }, now)).toBe('7d')
  })

  it('still recognises a preset a few minutes after the URL was written', () => {
    expect(
      activeTimePreset({ ...EMPTY_FILTERS, since: presetSince(24, now) }, now + 4 * 60_000),
    ).toBe('24h')
  })

  it('calls no bounds All time and a hand-picked window nothing at all', () => {
    expect(activeTimePreset(EMPTY_FILTERS, now)).toBe('all')
    expect(activeTimePreset({ ...EMPTY_FILTERS, since: presetSince(3, now) }, now)).toBeNull()
    // An `until` is precision the presets never write, so they cannot claim it.
    expect(activeTimePreset({ ...EMPTY_FILTERS, until: '2024-01-31' }, now)).toBeNull()
  })
})
