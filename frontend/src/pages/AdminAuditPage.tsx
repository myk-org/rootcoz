import { Fragment, useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigationType, useSearchParams } from 'react-router-dom'
import { api, extractApiDetail } from '@/lib/api'
import { EMPTY_FILTERS, matchesFilters, type AuditEvent, type AuditFilters } from '@/lib/auditFilters'
import { formatRelativeTime, formatTimestamp } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
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
import { ChevronDown, ChevronLeft, ChevronRight, Check } from 'lucide-react'

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

const OUTCOME_BADGE_VARIANT: Record<string, 'success' | 'destructive' | 'warning'> = {
  success: 'success',
  failure: 'destructive',
  denied: 'warning',
}

/** ?limit must be one of the offered page sizes — anything else means 50. */
function parsePageSize(raw: string | null): PageSize {
  const value = Number(raw)
  return PAGE_SIZE_OPTIONS.includes(value as PageSize) ? (value as PageSize) : DEFAULT_PAGE_SIZE
}

/** The filters kept in the URL, so a copied link shows the same rows. */
const FILTER_KEYS = Object.keys(EMPTY_FILTERS) as Array<keyof AuditFilters>

function filtersFromParams(params: URLSearchParams): AuditFilters {
  const filters = { ...EMPTY_FILTERS }
  for (const key of FILTER_KEYS) filters[key] = params.get(key) ?? ''
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
  | { type: 'sync'; filters: AuditFilters; limit: PageSize; live: boolean }
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
    // An adopted query describes rows from the start of the list.
    case 'sync':
      return {
        ...state,
        filters: action.filters,
        limit: action.limit,
        offset: 0,
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
    offset: 0,
    limit: parsePageSize(params.get('limit')),
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
  for (const [key, value] of Object.entries(filters)) {
    if (value.trim()) params.set(key, value.trim())
  }
  return params.toString()
}

function targetLabel(event: AuditEvent): string {
  if (event.target_type && event.target_id) return `${event.target_type}:${event.target_id}`
  return event.target_type ?? event.target_id ?? '—'
}

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
          className="flex h-9 w-full items-center justify-between gap-2 rounded-full border border-border-default bg-surface-elevated px-4 text-sm text-text-primary transition-colors hover:bg-surface-hover sm:w-52"
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
  const stateRef = useRef({ filters, limit, live })
  useEffect(() => {
    stateRef.current = { filters, limit, live }
  })

  // Mirror the filters back into the URL. Unrelated params (limit, live) stay,
  // and empty filters are removed rather than stored as empty strings.
  useEffect(() => {
    writeUrl((next) => {
      for (const key of FILTER_KEYS) {
        const value = filters[key].trim()
        if (value) next.set(key, value)
        else next.delete(key)
      }
    })
  }, [filters, writeUrl])

  // Back/Forward or an outside link can replace the query while this page stays
  // mounted. This page only ever writes with `replace`, so any other navigation
  // type is somebody else's — adopt it, and the next request follows it.
  useEffect(() => {
    // A `replace` is this page's own write, already accounted for in urlRef.
    if (navigationType === 'REPLACE') return
    urlRef.current = searchParams.toString()
    const current = stateRef.current
    const adopted = filtersFromParams(searchParams)
    const adoptedLimit = parsePageSize(searchParams.get('limit'))
    const adoptedLive = searchParams.get('live') === '1'
    if (
      adoptedLimit === current.limit &&
      adoptedLive === current.live &&
      filtersMatchParams(searchParams, current.filters)
    ) {
      return
    }
    dispatch({ type: 'sync', filters: adopted, limit: adoptedLimit, live: adoptedLive })
  }, [searchParams, navigationType, dispatch])

  const [result, setResult] = useState<{ key: string; data: AuditListResponse } | null>(null)
  const [errorState, setErrorState] = useState<{ key: string; text: string }>({ key: '', text: '' })
  const [actions, setActions] = useState<AuditActionEntry[]>([])
  const [expandedId, setExpandedId] = useState<number | null>(null)

  // The request key doubles as the loading flag: a response for the current
  // key means the shown page matches the filters, anything else is stale.
  const query = buildAuditQuery(filters, offset, limit)
  const loaded = result?.key === query ? result.data : null
  const error = errorState.key === query ? errorState.text : ''
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
        setErrorState({ key: '', text: '' })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setErrorState({
          key: query,
          text:
            extractApiDetail(err) ??
            (err instanceof Error ? err.message : 'Failed to load audit events'),
        })
      })
    return () => {
      cancelled = true
    }
  }, [query])

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
    source.onopen = () => dispatch({ type: 'live', phase: 'streaming' })
    // No automatic reconnect: the switch goes off and the admin re-enables it.
    source.onerror = () => {
      source.close()
      dispatch({ type: 'live', phase: 'failed' })
      writeParam('live', null)
    }

    return () => source.close()
  }, [live, prependEvent, writeParam, dispatch])

  const setLive = (checked: boolean) => {
    dispatch({ type: 'live', phase: checked ? 'connecting' : 'off' })
    writeParam('live', checked ? '1' : null)
  }

  const clearFilters = () => {
    dispatch({ type: 'clear' })
  }

  const hasFilters = Object.values(filters).some((value) => value.trim() !== '')
  const events = loaded?.events ?? []
  const total = loaded?.total ?? 0
  const from = total === 0 ? 0 : offset + 1
  const to = offset + events.length

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Audit</h1>
        <p className="text-sm text-muted-foreground">
          Who did what, when. Redacted — values of sensitive settings are never recorded.
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
        <Input
          type="date"
          aria-label="Since date"
          value={filters.since}
          onChange={(e) => dispatch({ type: 'filter', key: 'since', value: e.target.value })}
          className="sm:w-40"
        />
        <Input
          type="date"
          aria-label="Until date"
          value={filters.until}
          onChange={(e) => dispatch({ type: 'filter', key: 'until', value: e.target.value })}
          className="sm:w-40"
        />
        <Input
          placeholder="Search details…"
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
        <TooltipProvider delayDuration={200}>
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
        </TooltipProvider>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-sm text-signal-red">
          {error}
        </div>
      )}

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Actor</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Target</TableHead>
                <TableHead>Outcome</TableHead>
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
                  // A row with no details has nothing to expand; making it look
                  // clickable only to reveal "{}" is a dead affordance.
                  const hasDetails = Object.keys(event.details ?? {}).length > 0
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
                          className="whitespace-nowrap text-sm text-text-secondary"
                          title={formatRelativeTime(event.created_at)}
                        >
                          {formatTimestamp(event.created_at)}
                        </TableCell>
                        <TableCell className="text-sm">
                          <span className="font-medium text-text-primary">{event.actor}</span>
                          {event.actor_role && (
                            <span className="ml-2 text-xs text-text-tertiary">{event.actor_role}</span>
                          )}
                          {event.is_admin && event.actor_role !== 'admin' && (
                            <span className="ml-2 text-xs font-medium text-signal-blue">admin</span>
                          )}
                        </TableCell>
                        <TableCell className="text-sm">
                          {label}
                          {!hasDetails && (
                            <span className="mt-0.5 block text-xs text-text-tertiary">
                              No details recorded
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-text-secondary">
                          {targetLabel(event)}
                        </TableCell>
                        <TableCell>
                          <Badge variant={OUTCOME_BADGE_VARIANT[event.outcome] ?? 'outline'}>
                            {event.outcome}
                          </Badge>
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow>
                          <TableCell colSpan={5} className="bg-surface-elevated/40">
                            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-text-secondary">
                              {JSON.stringify(event.details ?? {}, null, 2)}
                            </pre>
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
  )
}
