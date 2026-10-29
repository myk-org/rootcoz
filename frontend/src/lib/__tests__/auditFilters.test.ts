import { describe, expect, it } from 'vitest'
import { EMPTY_FILTERS, detailsSearchTexts, matchesFilters, type AuditEvent } from '@/lib/auditFilters'

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

  it('matches q case-insensitively inside the serialized details', () => {
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'AI_CALL_TIMEOUT' })).toBe(true)
    expect(matchesFilters(event(), { ...EMPTY_FILTERS, q: 'not-there' })).toBe(false)
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
})
