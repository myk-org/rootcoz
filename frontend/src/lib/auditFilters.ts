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
  /** Read-time display name of the target job (null for other targets or a gone job).
   *  Absent on live-streamed events, which keep their ids only. */
  target_label?: string | null
  /** Read-time job uuid behind ``target_id`` (which may be a job name), null for
   *  other targets or a gone job. Absent on live-streamed events. */
  target_job_id?: string | null
  /** Actor of the audit event that deleted the target job. Null when the job is
   *  live or its deletion predates the audit trail. Absent on streamed events. */
  target_deleted_by?: string | null
  /** UTC timestamp of that delete event, with the same null rules as
   *  ``target_deleted_by``. */
  target_deleted_at?: string | null
  /** Id of that delete event — the row that proves the deletion happened. */
  target_delete_event_id?: number | null
  /** Read-time answer to "does this target still exist". ``true`` means the
   *  read model resolved it and found nothing, so the row shows a gone marker
   *  instead of a link. Absent on live-streamed events, which are not resolved. */
  target_gone?: boolean | null
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
  /** Id of one pinned row (`?event=`), not a match rule for any other row. */
  event: string
}

export const EMPTY_FILTERS: AuditFilters = {
  actor: '',
  action: '',
  outcome: '',
  since: '',
  until: '',
  q: '',
  event: '',
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
  // A pinned row is the whole view: a streamed event would be a second one.
  const pinned = filters.event.trim()
  if (pinned && String(event.id) !== pinned) return false

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

  // The server searches actor, target id, action and details; a live row has to
  // pass the same test or the tail would drop rows the list would show.
  const needle = filters.q.trim().toLowerCase()
  if (needle) {
    const haystack = [
      event.actor,
      event.target_id ?? '',
      event.action,
      ...detailsSearchTexts(event.details),
    ]
    if (!haystack.some((text) => text.toLowerCase().includes(needle))) return false
  }

  return true
}

/** Relative time shortcuts: `hours` of window behind the click, 0 = no bound. */
export const TIME_PRESETS = [
  { id: '1h', label: 'Last hour', hours: 1 },
  { id: '24h', label: 'Last 24 hours', hours: 24 },
  { id: '7d', label: 'Last 7 days', hours: 7 * 24 },
  { id: 'all', label: 'All time', hours: 0 },
] as const

export type TimePresetId = (typeof TIME_PRESETS)[number]['id']

/**
 * How far `since` may sit from the live window start and still be that preset.
 * A preset stamps `since` once and the URL keeps it, so "now" moves on: a
 * reload minutes later must still recognise the choice, and a window that has
 * genuinely aged out of its preset stops claiming it.
 */
const PRESET_DRIFT_MS = 5 * 60_000

/** The `since` a preset writes, from the click instant — UTC, and fixed from then on. */
export function presetSince(hours: number, now: number = Date.now()): string {
  return new Date(now - hours * 3_600_000).toISOString()
}

/**
 * Which preset the URL's bounds describe, or null for a hand-picked range.
 * A preset never carries `until`, so an `until` means the admin overrode it.
 */
export function activeTimePreset(
  filters: AuditFilters,
  now: number = Date.now(),
): TimePresetId | null {
  if (filters.until.trim()) return null
  const since = filters.since.trim()
  if (!since) return 'all'
  const millis = parseApiTimestamp(since).getTime()
  if (Number.isNaN(millis)) return null
  for (const preset of TIME_PRESETS) {
    if (preset.hours && Math.abs(now - millis - preset.hours * 3_600_000) <= PRESET_DRIFT_MS) {
      return preset.id
    }
  }
  return null
}
