import { Fragment, useCallback, useEffect, useReducer, useRef, useState } from 'react'
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

const PAGE_SIZE = 50
const SEARCH_DEBOUNCE_MS = 300

const OUTCOME_FILTER_OPTIONS = ['success', 'failure', 'denied'] as const

const OUTCOME_BADGE_VARIANT: Record<string, 'success' | 'destructive' | 'warning'> = {
  success: 'success',
  failure: 'destructive',
  denied: 'warning',
}

interface AuditState {
  filters: AuditFilters
  offset: number
  live: boolean
}

type AuditAction =
  | { type: 'filter'; key: keyof AuditFilters; value: string }
  | { type: 'offset'; value: number }
  | { type: 'clear' }
  | { type: 'live'; value: boolean }

function reducer(state: AuditState, action: AuditAction): AuditState {
  switch (action.type) {
    case 'filter':
      if (state.filters[action.key] === action.value) return state
      return { ...state, filters: { ...state.filters, [action.key]: action.value }, offset: 0 }
    case 'offset':
      return { ...state, offset: action.value }
    case 'clear':
      return { ...state, filters: EMPTY_FILTERS, offset: 0 }
    case 'live':
      return { ...state, live: action.value }
  }
}

/** Query string for GET /api/admin/audit — empty filters are omitted. */
function buildAuditQuery(filters: AuditFilters, offset: number): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
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
  const [state, dispatch] = useReducer(reducer, { filters: EMPTY_FILTERS, offset: 0, live: false })
  const { filters, offset, live } = state

  // Text inputs are drafts; the reducer only sees them after the debounce.
  const [actorDraft, setActorDraft] = useState('')
  const [queryDraft, setQueryDraft] = useState('')

  const [result, setResult] = useState<{ key: string; data: AuditListResponse } | null>(null)
  const [errorState, setErrorState] = useState<{ key: string; text: string }>({ key: '', text: '' })
  const [actions, setActions] = useState<AuditActionEntry[]>([])
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [liveConnected, setLiveConnected] = useState(false)

  // The request key doubles as the loading flag: a response for the current
  // key means the shown page matches the filters, anything else is stale.
  const query = buildAuditQuery(filters, offset)
  const loaded = result?.key === query ? result.data : null
  const loading = loaded === null
  const error = errorState.key === query ? errorState.text : ''

  useEffect(() => {
    const timer = setTimeout(() => {
      dispatch({ type: 'filter', key: 'actor', value: actorDraft })
      dispatch({ type: 'filter', key: 'q', value: queryDraft })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [actorDraft, queryDraft])

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
  const viewRef = useRef({ filters, offset })
  useEffect(() => {
    viewRef.current = { filters, offset }
  }, [filters, offset])

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
          events: [event, ...prev.data.events.filter((e) => e.id !== event.id)].slice(
            0,
            prev.data.limit,
          ),
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
    source.onopen = () => setLiveConnected(true)
    source.onerror = () => setLiveConnected(false)

    return () => {
      source.close()
      setLiveConnected(false)
    }
  }, [live, prependEvent])

  const clearFilters = () => {
    setActorDraft('')
    setQueryDraft('')
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
          onChange={(e) => setActorDraft(e.target.value)}
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
          value={queryDraft}
          onChange={(e) => setQueryDraft(e.target.value)}
          className="sm:w-52"
        />
        {hasFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear
          </Button>
        )}
        <div className="ml-auto flex items-center gap-2">
          <span
            className={`h-2 w-2 rounded-full ${live && liveConnected ? 'animate-pulse bg-signal-green' : 'bg-border-default'}`}
          />
          <span className="text-xs text-text-secondary">Live</span>
          <Switch
            aria-label="Live tail"
            checked={live}
            onCheckedChange={(checked) => dispatch({ type: 'live', value: checked })}
          />
        </div>
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
              {!loading && events.length === 0 && (
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
                  return (
                    <Fragment key={event.id}>
                      <TableRow
                        aria-expanded={expanded}
                        tabIndex={0}
                        onClick={() => setExpandedId(expanded ? null : event.id)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setExpandedId(expanded ? null : event.id)
                          }
                        }}
                        className="cursor-pointer"
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
                        <TableCell className="text-sm">{label}</TableCell>
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
            onClick={() => dispatch({ type: 'offset', value: Math.max(0, offset - PAGE_SIZE) })}
          >
            <ChevronLeft className="h-4 w-4" />
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={to >= total}
            onClick={() => dispatch({ type: 'offset', value: offset + PAGE_SIZE })}
          >
            Next
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
