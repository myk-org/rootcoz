import { parseApiTimestamp } from '@/lib/utils'

export interface AuditEvent {
  id: number
  created_at: string
  actor: string
  actor_role: string | null
  is_admin: boolean
  action: string
  target_type: string | null
  target_id: string | null
  outcome: string
  details: Record<string, unknown>
  request_ip: string | null
}

export interface AuditFilters {
  actor: string
  action: string
  outcome: string
  since: string
  until: string
  q: string
}

export const EMPTY_FILTERS: AuditFilters = {
  actor: '',
  action: '',
  outcome: '',
  since: '',
  until: '',
  q: '',
}

/**
 * A filter bound in epoch millis, or null when unbounded. Stored timestamps and
 * the bounds are both UTC; a date-only bound covers that whole UTC day.
 */
function boundMillis(value: string, endOfDay: boolean): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const iso = /^\d{4}-\d{2}-\d{2}$/.test(trimmed)
    ? `${trimmed}T${endOfDay ? '23:59:59.999' : '00:00:00'}`
    : trimmed
  const millis = parseApiTimestamp(iso).getTime()
  return Number.isNaN(millis) ? null : millis
}

/**
 * Serializations of `details` the list endpoint can match: the server stores Python's
 * `json.dumps` output (spaced after ":" and ","), the client would produce compact JSON.
 */
export function detailsSearchTexts(details: Record<string, unknown> | null | undefined): string[] {
  const compact = JSON.stringify(details ?? {})
  return [compact, compact.replace(/","/g, '", "').replace(/":"/g, '": "')]
}

/**
 * True when a streamed event satisfies the filters shown in the UI.
 * The list endpoint filters server-side but the SSE stream does not, so the same
 * rules have to be applied here before a live row enters the table.
 */
export function matchesFilters(event: AuditEvent, filters: AuditFilters): boolean {
  const actor = filters.actor.trim().toLowerCase()
  if (actor && event.actor.toLowerCase() !== actor) return false

  const actions = filters.action
    .split(',')
    .map((action) => action.trim())
    .filter(Boolean)
  if (actions.length > 0 && !actions.includes(event.action)) return false

  const outcome = filters.outcome.trim()
  if (outcome && event.outcome !== outcome) return false

  // Compare instants, not strings: live events carry a `T`-separated UTC stamp
  // while a date-only bound used to be space-separated, and `T` sorts after ` `
  // — that dropped every event on the bound's own day.
  const createdAt = parseApiTimestamp(event.created_at).getTime()
  if (!Number.isNaN(createdAt)) {
    const since = boundMillis(filters.since, false)
    if (since !== null && createdAt < since) return false
    const until = boundMillis(filters.until, true)
    if (until !== null && createdAt > until) return false
  }

  const needle = filters.q.trim().toLowerCase()
  if (needle && !detailsSearchTexts(event.details).some((text) => text.toLowerCase().includes(needle))) {
    return false
  }

  return true
}
