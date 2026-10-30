import { Fragment, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { Link, useNavigationType, useSearchParams } from 'react-router-dom'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { useSignOut } from '@/lib/useSignOut'
import { EMPTY_FILTERS, activeTimePreset, matchesFilters, presetSince, TIME_PRESETS, type AuditEvent, type AuditFilters } from '@/lib/auditFilters'
import { parsePageSize } from '@/lib/pageParams'
import { CONTROL_CLASS } from '@/lib/controlStyles'
import { cn, formatRelativeTime, formatTimestamp, toIntInRange } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ChevronDown, ChevronLeft, ChevronRight, Check, Download } from 'lucide-react'

interface AuditListResponse {
  events: AuditEvent[]
  total: number
  limit: number
  offset: number
}

interface AuditActionEntry {
  action: string
  count: number
  last_seen: string
  label: string
}

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250, 500] as const
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number]
const DEFAULT_PAGE_SIZE: PageSize = 50
const SEARCH_DEBOUNCE_MS = 300

const OUTCOME_FILTER_OPTIONS = ['success', 'failure', 'denied'] as const

/** The one look for a single-line control in the filter bar. */
const FILTER_CONTROL = `${CONTROL_CLASS} text-sm text-text-primary transition-colors`

const EXPORT_FORMATS = ['csv', 'json'] as const
type ExportFormat = (typeof EXPORT_FORMATS)[number]

/** A `type="date"` input can only show a day — a preset's ISO `since` shows its UTC day. */
function dateInputValue(value: string): string {
  return value.slice(0, 10)
}

const OUTCOME_BADGE_VARIANT: Record<string, 'success' | 'destructive' | 'warning'> = {
  success: 'success',
  failure: 'destructive',
  denied: 'warning',
}

/** ?limit is read against the sizes this page offers (see parsePageSize). */
const pageSizeFrom = (raw: string | null) => parsePageSize(raw, PAGE_SIZE_OPTIONS, DEFAULT_PAGE_SIZE)

/** ?offset is a row index: a negative or non-numeric value is the first page. */
const pageOffsetFrom = (raw: string | null) =>
  toIntInRange(raw ?? '', 0, Number.MAX_SAFE_INTEGER, 0)

/** The filters kept in the URL, so a copied link shows the same rows. */
const FILTER_KEYS = Object.keys(EMPTY_FILTERS) as Array<keyof AuditFilters>

function filtersFromParams(params: URLSearchParams): AuditFilters {
  const filters = { ...EMPTY_FILTERS }
  for (const key of FILTER_KEYS) filters[key] = params.get(key) ?? ''
  // A pin is an event id, never arbitrary text that could only 422 the request.
  if (filters.event && !/^\d+$/.test(filters.event.trim())) filters.event = ''
  return filters
}

/** Do these filters already match the filter params of this query? */
function filtersMatchParams(params: URLSearchParams, filters: AuditFilters): boolean {
  return FILTER_KEYS.every((key) => (params.get(key) ?? '').trim() === filters[key].trim())
}

/**
 * One value drives the whole live-tail UI: the switch, the dot and the label.
 * `failed` is the switch-off state that still has to look red, so it cannot be
 * derived from the switch alone.
 */
type LivePhase = 'off' | 'connecting' | 'streaming' | 'failed'

interface AuditState {
  filters: AuditFilters
  offset: number
  limit: PageSize
  livePhase: LivePhase
  /** Text inputs are drafts; the reducer only sees them after the debounce. */
  actorDraft: string
  qDraft: string
}

type AuditAction =
  | { type: 'filter'; key: keyof AuditFilters; value: string }
  | { type: 'offset'; value: number }
  | { type: 'limit'; value: PageSize }
  /** The URL changed under us (Back/Forward, an outside link). */
  | { type: 'sync'; filters: AuditFilters; limit: PageSize; offset: number; live: boolean }
  | { type: 'draft'; field: 'actorDraft' | 'qDraft'; value: string }
  | { type: 'live'; phase: LivePhase }
  | { type: 'clear' }

function reducer(state: AuditState, action: AuditAction): AuditState {
  switch (action.type) {
    case 'filter':
      if (state.filters[action.key] === action.value) return state
      return { ...state, filters: { ...state.filters, [action.key]: action.value }, offset: 0 }
    case 'offset':
      if (state.offset === action.value) return state
      return { ...state, offset: action.value }
    // A new page size makes the old offset meaningless.
    case 'limit':
      return { ...state, limit: action.value, offset: 0 }
    // An adopted query describes the page the URL names, not always the first.
    case 'sync':
      return {
        ...state,
        filters: action.filters,
        limit: action.limit,
        offset: action.offset,
        actorDraft: action.filters.actor,
        qDraft: action.filters.q,
        // A streaming tail keeps its phase; a live URL re-arms a dead one.
        livePhase: action.live
          ? state.livePhase === 'off' || state.livePhase === 'failed'
            ? 'connecting'
            : state.livePhase
          : 'off',
      }
    case 'draft':
      if (state[action.field] === action.value) return state
      return { ...state, [action.field]: action.value }
    case 'live':
      if (state.livePhase === action.phase) return state
      return { ...state, livePhase: action.phase }
    case 'clear':
      return { ...state, filters: EMPTY_FILTERS, offset: 0, actorDraft: '', qDraft: '' }
  }
}

/** Fuel for the reducer, from the URL the page was opened or navigated on. */
function initialState(params: URLSearchParams): AuditState {
  const filters = filtersFromParams(params)
  return {
    filters,
    offset: pageOffsetFrom(params.get('offset')),
    limit: pageSizeFrom(params.get('limit')),
    livePhase: params.get('live') === '1' ? 'connecting' : 'off',
    actorDraft: filters.actor,
    qDraft: filters.q,
  }
}

// No text label: the dot plus its tooltip carry the state (off / connecting /
// streaming / failed). A label of varying length shifted the switch around.
const LIVE_VIEW: Record<LivePhase, { dot: string; tooltip: string }> = {
  off: { dot: 'bg-border-default', tooltip: 'Live tail is off' },
  connecting: {
    dot: 'bg-signal-amber',
    tooltip: 'Connecting to the live audit stream',
  },
  streaming: {
    dot: 'animate-pulse bg-signal-green',
    tooltip: 'Receiving live audit events',
  },
  failed: {
    dot: 'bg-signal-red',
    tooltip: 'Live stream failed — switch Live on to try again',
  },
}

/** Query string for GET /api/admin/audit — empty filters are omitted. */
function buildAuditQuery(filters: AuditFilters, offset: number, limit: PageSize): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  // A pinned row goes alone: the server calls it `id`, and the other filters are
  // dropped so they cannot hide the event the admin followed a link for.
  const pinned = filters.event.trim()
  if (pinned) {
    params.set('id', pinned)
    return params.toString()
  }
  for (const [key, value] of Object.entries(filters)) {
    if (value.trim()) params.set(key, value.trim())
  }
  return params.toString()
}

function targetLabel(event: AuditEvent): string {
  if (event.target_type && event.target_id) return `${event.target_type}:${event.target_id}`
  return event.target_type ?? event.target_id ?? '—'
}

/**
 * Query string for GET /api/admin/audit/export — the list's filters, never its
 * page bounds, so no page size can cap what the admin came to retain.
 */
function buildAuditExportUrl(filters: AuditFilters, format: ExportFormat): string {
  const params = new URLSearchParams({ format })
  for (const key of FILTER_KEYS) {
    const value = filters[key].trim()
    // The pin is a list-only `id` filter; the export endpoint has no such bound.
    if (key !== 'event' && value) params.set(key, value)
  }
  return `/api/admin/audit/export?${params.toString()}`
}

/** The current filtered set as a file; the format is this click's only trace. */
function ExportMenu({
  disabled,
  onExport,
}: {
  disabled: boolean
  onExport: (format: ExportFormat) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="Export filtered audit events"
          className={cn(
            'flex items-center gap-1.5 px-4 disabled:cursor-not-allowed disabled:opacity-50',
            FILTER_CONTROL,
          )}
        >
          <Download className="h-3.5 w-3.5 opacity-50" />
          Export
          <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-30" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-24 p-1">
        {EXPORT_FORMATS.map((format) => (
          <button
            key={format}
            type="button"
            onClick={() => {
              setOpen(false)
              onExport(format)
            }}
            className="flex w-full items-center rounded-lg px-2 py-1.5 text-left text-sm text-text-secondary hover:bg-surface-hover hover:text-text-primary"
          >
            {format.toUpperCase()}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  )
}

/** A job report URL, optionally scrolled to one of its comments. */
function reportHref(jobId: string, commentId?: string): string {
  return commentId ? `/results/${jobId}#comment-${commentId}` : `/results/${jobId}`
}

/** Job ids are uuids; a job *name* (metadata events store the name in
 *  ``target_id``) addresses no report, so it never becomes a link. */
const JOB_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * Does the read model say this target no longer exists?
 *
 * A job or comment answers through the report id it resolved: an explicit null
 * is a target the server found no row for, so linking it would 404 (a
 * live-streamed event arrives without the field at all). Every other type has
 * no such id, so the list states the answer outright in ``target_gone``.
 */
function targetGone(event: AuditEvent): boolean {
  if (event.target_gone === true) return true
  return (
    (event.target_type === 'job' || event.target_type === 'comment') &&
    event.target_job_id === null
  )
}

/** Actions that write a setting; such a row names its keys in details only. */
const SETTING_ACTION_PREFIXES = ['settings.', 'admin.setting_']

/**
 * The setting keys a row changed. A settings update names what it touched in
 * `details.changed_keys` (there is no single target setting to put in
 * `target_id`), so the row can still link to the first key it changed.
 */
function changedSettingKeys(event: AuditEvent): string[] {
  const { target_type: type, action, target_id: id, details } = event
  if (id) return []
  if (!(type === 'setting' || SETTING_ACTION_PREFIXES.some((prefix) => action.startsWith(prefix))))
    return []
  const keys = details?.changed_keys
  return Array.isArray(keys)
    ? keys.filter((key): key is string => typeof key === 'string' && key.trim() !== '')
    : []
}

/**
 * Where the target lives, derived from the ids — never from the label, never
 * stored, and never a dead link: a gone target has no destination.
 *
 * ``target_job_id`` is the report the server resolved for this row, whatever
 * shape ``target_id`` has (uuid or job name); a uuid ``target_id`` already is
 * its report on a streamed event, a name is not. Every other type is addressed
 * by its own id in a query param the destination page honours.
 */
function targetHref(event: AuditEvent): string | null {
  const { target_type: type, target_id: id, target_job_id: jobId, details } = event
  if (targetGone(event)) return null
  // A settings update has no target id: its changed key is the destination.
  if (!id) {
    const [first] = changedSettingKeys(event)
    return first ? `/admin/settings?highlight=${encodeURIComponent(first)}` : null
  }
  switch (type) {
    case 'job': {
      const report = jobId === undefined ? (JOB_UUID.test(id) ? id : null) : jobId
      return report ? reportHref(report) : null
    }
    case 'comment': {
      const report = jobId === undefined ? details?.job_id : jobId
      return report ? reportHref(String(report), id) : null
    }
    case 'user':
      return `/admin/users?user=${encodeURIComponent(id)}`
    case 'provider':
      return '/admin/settings'
    case 'setting':
      return `/admin/settings?highlight=${encodeURIComponent(id)}`
    case 'transcript':
      return `/admin/transcripts?session=${encodeURIComponent(id)}`
    case 'feedback': {
      const url = details?.issue_url
      return typeof url === 'string' && url ? url : null
    }
    case 'failure': {
      const job = details?.job_id
      return job ? reportHref(String(job)) : null
    }
    default:
      return null
  }
}

interface GoneTarget {
  /** Marker text under the target name. */
  text: string
  /** The audit row that recorded the deletion, or null when nothing did. */
  href: string | null
  tooltip: string
}

/**
 * What happened to a target that is gone.
 *
 * A live target is a link; a gone one has only the delete event, and a removal
 * the trail never recorded is said to be unknown — never claimed without
 * evidence, and never attributed to a person who did not do it.
 */
function goneTarget(event: AuditEvent): GoneTarget | null {
  if (!targetGone(event)) return null
  const deletion = event.target_delete_event_id
  if (deletion === null || deletion === undefined) {
    return {
      text: 'not found',
      href: null,
      // The one type whose removal is nobody's audited act: say so plainly
      // instead of implying a person or blaming the trail for losing it.
      tooltip:
        event.target_type === 'transcript'
          ? "Transcripts are removed by the sidecar's retention, and that removal is not audited."
          : 'No destination and no removal recorded — it may predate the audit trail.',
    }
  }
  return {
    text: `deleted by ${event.target_deleted_by ?? 'unknown'}`,
    href: `/admin/audit?event=${deletion}`,
    tooltip: `${formatTimestamp(event.target_deleted_at)} — open the deletion event`,
  }
}

/** The muted marker under a gone target: the evidence link, or a plain note. */
function GoneTargetMarker({ target }: { target: GoneTarget }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="mt-0.5 block text-text-tertiary">
          {target.href ? (
            <Link
              to={target.href}
              onClick={(e) => e.stopPropagation()}
              className="select-text hover:underline"
            >
              {target.text}
            </Link>
          ) : (
            target.text
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent>{target.tooltip}</TooltipContent>
    </Tooltip>
  )
}

/** The visible text: the label when one resolved, else the bare id. */
function targetText(event: AuditEvent): string {
  const { target_type: type, target_id: id, target_label: label, details } = event
  const [changed] = changedSettingKeys(event)
  if (!id || !type) return changed ?? targetLabel(event)
  switch (type) {
    case 'job':
      return label ?? id
    case 'comment':
      return `comment on ${label ?? id}`
    case 'feedback': {
      const issue = details?.issue_number
      return issue === undefined || issue === null || issue === '' ? id : `issue #${issue}`
    }
    case 'user':
    case 'provider':
    case 'setting':
    case 'transcript':
    case 'failure':
      return label ?? id
    default:
      return targetLabel(event)
  }
}

/** Details values that point at a report; unknown keys and values stay plain. */
function detailHref(key: string, value: unknown, parentJobId: unknown): string | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const id = String(value)
  if (!id) return null
  if (key === 'job_id') return reportHref(id)
  if (typeof parentJobId !== 'string' && typeof parentJobId !== 'number') return null
  if (!String(parentJobId)) return null
  if (key === 'failure_uuid') return reportHref(String(parentJobId))
  if (key === 'comment_id') return reportHref(String(parentJobId), id)
  return null
}

/** The one look for a target link, internal or external. */
const TARGET_LINK_CLASS = 'select-text break-all text-text-link hover:underline'

/** Searchable single-line picker fed by GET /api/admin/audit/actions. */
function ActionFilter({
  value,
  actions,
  onChange,
}: {
  value: string
  actions: AuditActionEntry[]
  onChange: (value: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')

  const selected = actions.find((a) => a.action === value)
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? actions.filter(
        (a) =>
          a.action.toLowerCase().includes(needle) || a.label.toLowerCase().includes(needle),
      )
    : actions

  const pick = (next: string) => {
    onChange(next)
    setOpen(false)
    setQuery('')
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Action filter"
          className={cn(
            'flex w-full items-center justify-between gap-2 px-4 sm:w-52',
            FILTER_CONTROL,
          )}
        >
          <span className="truncate">{selected?.label ?? 'All actions'}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-30" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-2">
        <Input
          autoFocus
          placeholder="Search actions…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="h-8"
        />
        <div className="mt-2 max-h-64 overflow-y-auto">
          <button
            type="button"
            onClick={() => pick('')}
            className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-text-secondary hover:bg-surface-hover hover:text-text-primary"
          >
            <Check className={`h-3.5 w-3.5 ${value ? 'opacity-0' : ''}`} />
            All actions
          </button>
          {filtered.map((entry) => (
            <button
              key={entry.action}
              type="button"
              onClick={() => pick(entry.action)}
              className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-text-secondary hover:bg-surface-hover hover:text-text-primary"
            >
              <Check className={`h-3.5 w-3.5 shrink-0 ${value === entry.action ? '' : 'opacity-0'}`} />
              <span className="truncate">{entry.label}</span>
              <span className="ml-auto shrink-0 font-mono text-xs text-text-tertiary">
                {entry.count}
              </span>
            </button>
          ))}
          {filtered.length === 0 && (
            <p className="px-2 py-1.5 text-sm text-text-tertiary">No matching actions</p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}

export function AdminAuditPage() {
  const signOut = useSignOut()
  const [searchParams, setSearchParams] = useSearchParams()
  // ?limit, ?live=1 and every filter survive a refresh and a copied link.
  const [state, dispatch] = useReducer(reducer, searchParams, initialState)
  const { filters, offset, limit, livePhase, actorDraft, qDraft } = state
  const live = livePhase === 'connecting' || livePhase === 'streaming'

  // setSearchParams changes identity with the query string; a ref keeps the
  // stream effect from tearing the stream down every time the URL changes.
  const setSearchParamsRef = useRef(setSearchParams)
  useEffect(() => {
    setSearchParamsRef.current = setSearchParams
  }, [setSearchParams])

  // The query string this page believes is in the address bar. Writes start
  // from here, not from the router's copy, so two edits in a row cannot clobber
  // each other while a navigation is still committing.
  const urlRef = useRef(searchParams.toString())
  const navigationType = useNavigationType()

  const writeUrl = useCallback((mutate: (params: URLSearchParams) => void) => {
    const next = new URLSearchParams(urlRef.current)
    mutate(next)
    const query = next.toString()
    // Already there — writing it again would only push router work around.
    if (query === urlRef.current) return
    urlRef.current = query
    setSearchParamsRef.current(next, { replace: true })
  }, [])

  const writeParam = useCallback(
    (key: string, value: string | null) => {
      writeUrl((next) => {
        if (value === null) next.delete(key)
        else next.set(key, value)
      })
    },
    [writeUrl],
  )

  // Latest state for the URL-sync effect, which must not re-run on every
  // keystroke — a stale read there would adopt the URL over a fresh edit.
  const stateRef = useRef({ filters, offset, limit, live })
  useEffect(() => {
    stateRef.current = { filters, offset, limit, live }
  })

  // Mirror the filters and the page window back into the URL. Unrelated params
  // (limit, live) stay, and empty filters are removed rather than stored as
  // empty strings — a reload or a shared link then shows the page being read.
  useEffect(() => {
    writeUrl((next) => {
      for (const key of FILTER_KEYS) {
        const value = filters[key].trim()
        if (value) next.set(key, value)
        else next.delete(key)
      }
      if (offset > 0) next.set('offset', String(offset))
      else next.delete('offset')
    })
  }, [filters, offset, writeUrl])

  // Back/Forward or an outside link can replace the query while this page stays
  // mounted. This page only ever writes with `replace`, so any other navigation
  // type is somebody else's — adopt it, and the next request follows it.
  useEffect(() => {
    // A `replace` is this page's own write, already accounted for in urlRef.
    if (navigationType === 'REPLACE') return
    urlRef.current = searchParams.toString()
    const current = stateRef.current
    const adopted = filtersFromParams(searchParams)
    const adoptedLimit = pageSizeFrom(searchParams.get('limit'))
    const adoptedOffset = pageOffsetFrom(searchParams.get('offset'))
    const adoptedLive = searchParams.get('live') === '1'
    if (
      adoptedLimit === current.limit &&
      adoptedOffset === current.offset &&
      adoptedLive === current.live &&
      filtersMatchParams(searchParams, current.filters)
    ) {
      return
    }
    dispatch({
      type: 'sync',
      filters: adopted,
      limit: adoptedLimit,
      offset: adoptedOffset,
      live: adoptedLive,
    })
  }, [searchParams, navigationType, dispatch])

  const [result, setResult] = useState<{ key: string; data: AuditListResponse } | null>(null)
  const [errorState, setErrorState] = useState<{ key: string; text: string; unauthorized: boolean }>({
    key: '',
    text: '',
    unauthorized: false,
  })
  const [actions, setActions] = useState<AuditActionEntry[]>([])
  const [expandedId, setExpandedId] = useState<number | null>(null)
  // Events the server dropped for this subscriber; the list below has holes.
  const [dropped, setDropped] = useState(0)
  // Bumped by the notice's refresh action to refetch the current query.
  const [reloadToken, setReloadToken] = useState(0)
  // The export confirmation belongs to the filter set it counted: a filter
  // change hides it instead of reporting the previous set's rows.
  const [exported, setExported] = useState<{
    filters: string
    count: number
    format: ExportFormat
  } | null>(null)

  // The request key doubles as the loading flag: a response for the current
  // key means the shown page matches the filters, anything else is stale.
  const query = buildAuditQuery(filters, offset, limit)
  const loaded = result?.key === query ? result.data : null
  const error = errorState.key === query ? errorState.text : ''
  // A 401 is not a generic failure: the admin cannot fix it from this page.
  const unauthorized = errorState.key === query && errorState.unauthorized
  // A failed request has no data for this query, but it is not still loading it.
  const loading = loaded === null && !error

  useEffect(() => {
    const timer = setTimeout(() => {
      dispatch({ type: 'filter', key: 'actor', value: actorDraft })
      dispatch({ type: 'filter', key: 'q', value: qDraft })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [actorDraft, qDraft])

  useEffect(() => {
    api
      .get<{ actions: AuditActionEntry[] }>('/api/admin/audit/actions')
      .then((payload) => setActions(payload.actions ?? []))
      .catch(() => setActions([]))
  }, [])

  useEffect(() => {
    let cancelled = false
    api
      .get<AuditListResponse>(`/api/admin/audit?${query}`)
      .then((payload) => {
        if (cancelled) return
        setResult({ key: query, data: payload })
        // A query that failed earlier is recovered — do not keep its banner.
        setErrorState({ key: '', text: '', unauthorized: false })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const sessionExpired = err instanceof ApiError && err.status === 401
        setErrorState({
          key: query,
          unauthorized: sessionExpired,
          text: sessionExpired
            ? 'Your session expired — sign in again to load the audit log.'
            : extractApiDetail(err) ??
              (err instanceof Error ? err.message : 'Failed to load audit events'),
        })
      })
    return () => {
      cancelled = true
    }
  }, [query, reloadToken])

  // Latest view for the stable stream handler: reconnecting SSE on every filter
  // or page change would drop the events emitted in the gap.
  const viewRef = useRef({ filters, offset, limit })
  useEffect(() => {
    viewRef.current = { filters, offset, limit }
  }, [filters, offset, limit])

  // Newest events stream in from the live tail; the buffer stays at one page
  // so "showing X–Y of N" keeps describing what is on screen.
  const prependEvent = useCallback((event: AuditEvent) => {
    const view = viewRef.current
    if (!matchesFilters(event, view.filters)) return
    setResult((prev) => {
      if (!prev) return prev
      // A newer event belongs before the current window: keep the rows, move
      // only the count so "of N" stays truthful.
      if (view.offset > 0) {
        return { key: prev.key, data: { ...prev.data, total: prev.data.total + 1 } }
      }
      const duplicate = prev.data.events.some((e) => e.id === event.id)
      return {
        key: prev.key,
        data: {
          ...prev.data,
          total: prev.data.total + (duplicate ? 0 : 1),
          events: [event, ...prev.data.events.filter((e) => e.id !== event.id)].slice(0, view.limit),
        },
      }
    })
  }, [])

  useEffect(() => {
    if (!live) return
    const source = new EventSource('/api/admin/audit/stream')

    source.addEventListener('audit:event', (message) => {
      try {
        prependEvent(JSON.parse((message as MessageEvent).data) as AuditEvent)
      } catch {
        // A malformed frame must not kill the stream.
      }
    })
    source.addEventListener('audit-gap', (message) => {
      try {
        // Each frame counts only the drops since the previous one, so the
        // notice adds them up: it states the total missed since the refresh.
        const count = (JSON.parse((message as MessageEvent).data) as { dropped?: number }).dropped
        if (typeof count === 'number' && count > 0) {
          setDropped((total) => total + count)
        }
      } catch {
        // A malformed frame must not kill the stream.
      }
    })
    source.onopen = () => dispatch({ type: 'live', phase: 'streaming' })
    // No automatic reconnect: the switch goes off and the admin re-enables it.
    source.onerror = () => {
      source.close()
      dispatch({ type: 'live', phase: 'failed' })
      writeParam('live', null)
    }

    return () => source.close()
  }, [live, prependEvent, writeParam, dispatch])

  // Refetch what is on screen with the same filters and offset, and drop the
  // gap notice: the fresh page has the events the stream skipped.
  const refreshView = useCallback(() => {
    setDropped(0)
    setReloadToken((token) => token + 1)
  }, [])

  const setLive = (checked: boolean) => {
    dispatch({ type: 'live', phase: checked ? 'connecting' : 'off' })
    writeParam('live', checked ? '1' : null)
  }

  const filtersKey = JSON.stringify(filters)
  const pinned = filters.event.trim() !== ''
  const activePreset = activeTimePreset(filters)
  const total = loaded?.total ?? 0
  // A pinned row is one event, and the export endpoint cannot narrow to it:
  // exporting then would hand back a file the count below does not describe.
  const canExport = !pinned && loaded !== null
  const exportNote = pinned
    ? 'A pinned event is shown alone — clear the pin to export a set.'
    : exported && exported.filters === filtersKey
      ? `Exported ${exported.count} ${exported.count === 1 ? 'event' : 'events'} as ${exported.format.toUpperCase()}.`
      : ''

  const clearFilters = () => {
    dispatch({ type: 'clear' })
  }

  // A preset is a `since` stamp taken at click time; `until` goes back to
  // unbounded. Both land in the URL like any other filter, so Back, Forward and
  // a copied link reproduce the window.
  const applyTimePreset = (hours: number) => {
    dispatch({ type: 'filter', key: 'since', value: hours ? presetSince(hours) : '' })
    dispatch({ type: 'filter', key: 'until', value: '' })
  }

  const runExport = (format: ExportFormat) => {
    const link = document.createElement('a')
    link.href = buildAuditExportUrl(filters, format)
    link.download = `audit-export.${format}`
    link.click()
    setExported({ filters: filtersKey, count: total, format })
  }

  // One click from a row to everything that actor did; the box and the URL follow.
  const filterByActor = (actor: string) => {
    dispatch({ type: 'draft', field: 'actorDraft', value: actor })
    dispatch({ type: 'filter', key: 'actor', value: actor })
  }

  const hasFilters = Object.values(filters).some((value) => value.trim() !== '')
  const events = loaded?.events ?? []
  const from = total === 0 ? 0 : offset + 1
  const to = offset + events.length

  return (
    <TooltipProvider delayDuration={200}>
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold">Audit</h1>
          <p className="text-sm text-muted-foreground">
            Who did what, when. Usernames are recorded verbatim; credential values never are.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Input
            placeholder="Actor…"
            aria-label="Actor filter"
            value={actorDraft}
            onChange={(e) => dispatch({ type: 'draft', field: 'actorDraft', value: e.target.value })}
            className="sm:w-40"
          />
          <ActionFilter
            value={filters.action}
            actions={actions}
            onChange={(value) => dispatch({ type: 'filter', key: 'action', value })}
          />
          <Select
            value={filters.outcome || 'all'}
            onValueChange={(value) =>
              dispatch({ type: 'filter', key: 'outcome', value: value === 'all' ? '' : value })
            }
          >
            <SelectTrigger aria-label="Outcome filter" className="sm:w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All outcomes</SelectItem>
              {OUTCOME_FILTER_OPTIONS.map((outcome) => (
                <SelectItem key={outcome} value={outcome}>
                  {outcome}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {/* Shortcuts over the same `since` the date inputs below set: a preset
              never removes the precise bound from the URL. */}
          <div
            role="group"
            aria-label="Time range presets"
            className={cn(FILTER_CONTROL, 'flex items-center gap-0.5 p-0.5 hover:bg-surface-elevated')}
          >
            {TIME_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                aria-pressed={activePreset === preset.id}
                onClick={() => applyTimePreset(preset.hours)}
                className={cn(
                  'h-8 rounded-md px-3 text-xs transition-colors',
                  activePreset === preset.id
                    ? 'bg-signal-blue/10 text-signal-blue'
                    : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
                )}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <Input
            type="date"
            aria-label="Since date"
            value={dateInputValue(filters.since)}
            onChange={(e) => dispatch({ type: 'filter', key: 'since', value: e.target.value })}
            className="sm:w-40"
          />
          <Input
            type="date"
            aria-label="Until date"
            value={dateInputValue(filters.until)}
            onChange={(e) => dispatch({ type: 'filter', key: 'until', value: e.target.value })}
            className="sm:w-40"
          />
          <Input
            placeholder="Search actor, target, action or details…"
            aria-label="Search filter"
            value={qDraft}
            onChange={(e) => dispatch({ type: 'draft', field: 'qDraft', value: e.target.value })}
            className="sm:w-52"
          />
          {hasFilters && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear
            </Button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <ExportMenu disabled={!canExport} onExport={runExport} />
            <Select
              value={String(limit)}
              onValueChange={(value) => {
                const next = Number(value) as PageSize
                dispatch({ type: 'limit', value: next })
                writeParam('limit', String(next))
              }}
            >
              <SelectTrigger aria-label="Rows per page" className="h-8 w-20">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <SelectItem key={size} value={String(size)}>
                    {size}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <div data-testid="live-status" className="flex items-center gap-1.5">
                <span
                  data-testid="live-dot"
                  className={`h-2 w-2 rounded-full ${LIVE_VIEW[livePhase].dot}`}
                />
                <Switch
                  aria-label={`Live tail — ${LIVE_VIEW[livePhase].tooltip}`}
                  checked={live}
                  onCheckedChange={setLive}
                />
              </div>
            </TooltipTrigger>
            <TooltipContent>{LIVE_VIEW[livePhase].tooltip}</TooltipContent>
          </Tooltip>
        </div>

        {/* Always rendered, so a confirmation or a pinned-row note cannot push
            the table down when it appears. */}
        <p role="status" data-testid="export-status" className="min-h-5 text-sm text-text-secondary">
          {exportNote}
        </p>

        {error && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-sm text-signal-red">
            <span>{error}</span>
            {unauthorized && (
              // Ends the session first: linking to /login while `authenticated`
              // is still true sends RegisterPage straight back to the dashboard.
              <button
                type="button"
                onClick={() => void signOut()}
                className="font-medium underline underline-offset-2"
              >
                Sign in
              </button>
            )}
          </div>
        )}

        {/* Outside the toolbar and the table: no control reorders and no row resizes
            when the notice appears. */}
        {dropped > 0 && (
          <CustomMessageBanner
            message={`Missed ${dropped} live ${dropped === 1 ? 'event' : 'events'} since the last refresh — the stream fell behind, so this view has a gap.`}
            action={
              <Button variant="ghost" size="sm" onClick={refreshView}>
                Refresh
              </Button>
            }
          />
        )}

        <Card>
          <CardContent className="p-0">
            {/* Nothing may push the table sideways: the columns are fixed and
                every cell wraps, so a pathological value only makes a row taller. */}
            <div className="w-full overflow-x-hidden">
              <Table className="table-fixed">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[9rem]">
                      {/* Stored values and the date filters are UTC; the cells show local time. */}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span
                            data-testid="time-basis"
                            tabIndex={0}
                            aria-label="Stored times and the date filters are UTC — this column is shown in your local time."
                            className="cursor-help"
                          >
                            Time <span className="text-xs font-normal text-text-tertiary">UTC</span>
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          Stored times and the date filters are UTC — this column is shown in your local
                          time.
                        </TooltipContent>
                      </Tooltip>
                    </TableHead>
                    <TableHead className="w-[22ch]">Actor</TableHead>
                    <TableHead className="w-[26ch]">Action</TableHead>
                    <TableHead className="w-[26ch]">Target</TableHead>
                    <TableHead className="w-[8rem]">Outcome</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loading &&
                    Array.from({ length: 5 }, (_, i) => (
                      <TableRow key={`skeleton-${i}`}>
                        <TableCell colSpan={5}>
                          <Skeleton className="h-4 w-full" />
                        </TableCell>
                      </TableRow>
                    ))}
                  {!loading && !error && events.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                        No audit events yet
                      </TableCell>
                    </TableRow>
                  )}
                  {!loading &&
                    events.map((event) => {
                      const expanded = expandedId === event.id
                      const label = actions.find((a) => a.action === event.action)?.label ?? event.action
                      const href = targetHref(event)
                      const text = targetText(event)
                      // A settings update can change several keys: the link names
                      // the first, the rest stay plain text beside it.
                      const otherKeys = changedSettingKeys(event).slice(1)
                      const gone = goneTarget(event)
                      // A feedback target lives on GitHub, not in this app.
                      const external = href !== null && /^https?:\/\//i.test(href)
                      const details = event.details ?? {}
                      // A row with no details has nothing to expand; making it look
                      // clickable only to reveal "{}" is a dead affordance.
                      const hasDetails = Object.keys(details).length > 0
                      const toggle = () => {
                        if (hasDetails) setExpandedId(expanded ? null : event.id)
                      }
                      return (
                        <Fragment key={event.id}>
                          <TableRow
                            {...(hasDetails ? { 'aria-expanded': expanded, tabIndex: 0 } : {})}
                            onClick={toggle}
                            onKeyDown={(e) => {
                              if (!hasDetails) return
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault()
                                toggle()
                              }
                            }}
                            className={hasDetails ? 'cursor-pointer' : undefined}
                          >
                            <TableCell
                              className="max-w-[9rem] whitespace-normal break-words align-top text-sm text-text-secondary"
                              title={formatRelativeTime(event.created_at)}
                            >
                              {formatTimestamp(event.created_at)}
                            </TableCell>
                            <TableCell className="max-w-[22ch] break-all align-top text-sm">
                              <button
                                type="button"
                                onClick={(e) => {
                                  // The row toggles its details: filtering is this click's job.
                                  e.stopPropagation()
                                  filterByActor(event.actor)
                                }}
                                className="block text-left font-medium text-text-primary hover:text-text-link hover:underline"
                              >
                                {event.actor}
                              </button>
                              {event.actor_role && (
                                <span className="mt-0.5 block text-xs text-text-tertiary">
                                  {event.actor_role}
                                </span>
                              )}
                              {event.is_admin && event.actor_role !== 'admin' && (
                                <span className="mt-0.5 block text-xs font-medium text-signal-blue">
                                  admin
                                </span>
                              )}
                            </TableCell>
                            <TableCell className="max-w-[26ch] whitespace-normal break-words align-top text-sm">
                              <span className="block max-w-[20ch]">{label}</span>
                              {!hasDetails && (
                                <span className="mt-0.5 block text-xs text-text-tertiary">
                                  No details recorded
                                </span>
                              )}
                            </TableCell>
                            <TableCell className="max-w-[26ch] whitespace-normal break-words align-top font-mono text-xs text-text-secondary">
                              {href ? (
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    {/* select-text: the link must not swallow copying the id. */}
                                    {external ? (
                                      <a
                                        href={href}
                                        target="_blank"
                                        rel="noreferrer"
                                        onClick={(e) => e.stopPropagation()}
                                        className={TARGET_LINK_CLASS}
                                      >
                                        {text}
                                      </a>
                                    ) : (
                                      <Link
                                        to={href}
                                        onClick={(e) => e.stopPropagation()}
                                        className={TARGET_LINK_CLASS}
                                      >
                                        {text}
                                      </Link>
                                    )}
                                  </TooltipTrigger>
                                  <TooltipContent className="font-mono">{targetLabel(event)}</TooltipContent>
                                </Tooltip>
                              ) : (
                                text
                              )}
                              {otherKeys.length > 0 && (
                                <span className="text-text-tertiary"> {otherKeys.join(', ')}</span>
                              )}
                              {/* Same cell, same name: only a gone target adds a line. */}
                              {gone && <GoneTargetMarker target={gone} />}
                            </TableCell>
                            <TableCell className="max-w-[8rem] whitespace-nowrap align-top">
                              <Badge variant={OUTCOME_BADGE_VARIANT[event.outcome] ?? 'outline'}>
                                {event.outcome}
                              </Badge>
                            </TableCell>
                          </TableRow>
                          {expanded && (
                            <TableRow>
                              <TableCell colSpan={5} className="bg-surface-elevated/40">
                                <dl
                                  data-testid="event-details"
                                  className="grid max-h-80 grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1 overflow-auto font-mono text-xs"
                                >
                                  {Object.entries(details).map(([key, value]) => {
                                    const detailLink = detailHref(key, value, details.job_id)
                                    const valueText =
                                      typeof value === 'string' ? value : JSON.stringify(value)
                                    return (
                                      <Fragment key={key}>
                                        <dt className="text-text-tertiary">{key}</dt>
                                        <dd className="break-all text-text-secondary">
                                          {detailLink ? (
                                            <Link
                                              to={detailLink}
                                              className="select-text text-text-link hover:underline"
                                            >
                                              {valueText}
                                            </Link>
                                          ) : (
                                            valueText
                                          )}
                                        </dd>
                                      </Fragment>
                                    )
                                  })}
                                </dl>
                                {event.request_ip && (
                                  <p className="mt-2 font-mono text-xs text-text-tertiary">
                                    request_ip: {event.request_ip}
                                  </p>
                                )}
                              </TableCell>
                            </TableRow>
                          )}
                        </Fragment>
                      )
                    })}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        <div className="flex items-center justify-between">
          <span className="text-sm text-text-secondary">
            Showing {from}–{to} of {total}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset === 0}
              onClick={() => dispatch({ type: 'offset', value: Math.max(0, offset - limit) })}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={to >= total}
              onClick={() => dispatch({ type: 'offset', value: offset + limit })}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>
    </TooltipProvider>
  )
}
