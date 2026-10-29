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

/** DB timestamps are naive UTC "YYYY-MM-DD HH:MM:SS"; a date-only bound covers that day. */
function boundValue(value: string, endOfDay: boolean): string {
  const trimmed = value.trim()
  if (!trimmed) return ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
    return `${trimmed} ${endOfDay ? '23:59:59' : '00:00:00'}`
  }
  // shortcut: only the local `YYYY-MM-DD` inputs are used, other bounds compare as strings
  return trimmed.replace('T', ' ')
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

  const since = boundValue(filters.since, false)
  if (since && event.created_at < since) return false
  const until = boundValue(filters.until, true)
  if (until && event.created_at > until) return false

  const needle = filters.q.trim().toLowerCase()
  if (needle && !JSON.stringify(event.details ?? {}).toLowerCase().includes(needle)) return false

  return true
}
