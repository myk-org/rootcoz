import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { formatCompactNumber, formatCost } from '@/lib/format'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Card, CardContent } from '@/components/ui/card'
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
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { SortableHeader } from '@/components/shared/SortableHeader'
import { DateRangeFilter } from '@/components/shared/DateRangeFilter'
import { useTableSort } from '@/lib/useTableSort'
import type { TokenUsageDashboard } from '@/types'
import { Zap, TrendingUp, Calendar, DollarSign, Info } from 'lucide-react'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'

interface BreakdownRow {
  group: string
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  avg_duration_ms: number
}

interface TokenUsageBreakdownResponse {
  total_input_tokens: number
  total_output_tokens: number
  total_cache_read_tokens: number
  total_cache_write_tokens: number
  total_cost_usd: number
  total_calls: number
  total_duration_ms: number
  breakdown: Array<{
    group_key: string
    call_count: number
    input_tokens: number
    output_tokens: number
    cache_read_tokens: number
    cache_write_tokens: number
    cost_usd: number
    avg_duration_ms: number
  }>
}

const GROUP_BY_OPTIONS = [
  { value: 'model', label: 'Model' },
  { value: 'provider', label: 'Provider' },
  { value: 'call_type', label: 'Call Type' },
  { value: 'day', label: 'Day' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
  { value: 'job', label: 'Job' },
] as const

type GroupByValue = typeof GROUP_BY_OPTIONS[number]['value']

function SummaryCard({ title, icon, calls, tokens, inputTokens, outputTokens, cost }: {
  title: string
  icon: React.ReactNode
  calls: number
  tokens: number
  inputTokens: number
  outputTokens: number
  cost: number
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center gap-2 mb-3">
          {icon}
          <h3 className="text-sm font-display font-medium text-text-primary">{title}</h3>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Calls</span>
            <span className="font-mono text-sm font-medium text-text-primary">{calls.toLocaleString()}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Tokens</span>
            <span className="font-mono text-sm font-medium text-text-primary">{formatCompactNumber(tokens)}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Input</span>
            <span className="font-mono text-xs text-text-secondary">{formatCompactNumber(inputTokens)}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Output</span>
            <span className="font-mono text-xs text-text-secondary">{formatCompactNumber(outputTokens)}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Cost</span>
            <span className="font-mono text-sm font-medium text-signal-green">
              {formatCostCell(cost)}
            </span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function formatDurationMs(ms: number): string {
  if (ms <= 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function formatCostCell(cost: number): string {
  if (cost <= 0) return '—'
  return formatCost(cost)
}

export function TokenUsagePage() {
  const [summary, setSummary] = useState<TokenUsageDashboard | null>(null)
  const [breakdown, setBreakdown] = useState<BreakdownRow[]>([])
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [breakdownLoading, setBreakdownLoading] = useState(true)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [breakdownError, setBreakdownError] = useState<string | null>(null)

  const [groupBy, setGroupBy] = useState<GroupByValue>('model')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [providerInput, setProviderInput] = useState('')
  const [debouncedProvider, setDebouncedProvider] = useState('')

  // Debounce provider filter input
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedProvider(providerInput), 300)
    return () => clearTimeout(timer)
  }, [providerInput])

  const { sortKey, sortDir, handleSort } = useTableSort('token-usage', 'cost_usd', 'desc', ['cost_usd', 'calls', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'avg_duration_ms'])

  // Guard against stale responses when filters change rapidly
  const latestRequestIdRef = useRef(0)

  // Fetch summary cards
  const fetchSummary = useCallback(() => {
    setSummaryLoading(true)
    api.get<TokenUsageDashboard>('/api/admin/token-usage/summary')
      .then((data) => {
        setSummary(data)
        setSummaryError(null)
      })
      .catch((err) => setSummaryError(err instanceof Error ? err.message : 'Failed to load summary'))
      .finally(() => setSummaryLoading(false))
  }, [])

  useEffect(() => {
    fetchSummary()
    const interval = setInterval(fetchSummary, 30000)
    return () => clearInterval(interval)
  }, [fetchSummary])

  // Fetch breakdown table
  const fetchBreakdown = useCallback(() => {
    const requestId = ++latestRequestIdRef.current
    setBreakdownLoading(true)

    const params = new URLSearchParams()
    params.set('group_by', groupBy)
    if (dateFrom) params.set('start_date', dateFrom)
    if (dateTo) params.set('end_date', dateTo)
    if (debouncedProvider.trim()) params.set('ai_provider', debouncedProvider.trim())

    api.get<TokenUsageBreakdownResponse>(`/api/admin/token-usage?${params.toString()}`)
      .then((data) => {
        if (requestId !== latestRequestIdRef.current) return
        setBreakdown(
          data.breakdown.map((row) => ({
            group: row.group_key,
            calls: row.call_count,
            input_tokens: row.input_tokens,
            output_tokens: row.output_tokens,
            cache_read_tokens: row.cache_read_tokens,
            cache_write_tokens: row.cache_write_tokens,
            cost_usd: row.cost_usd,
            avg_duration_ms: row.avg_duration_ms,
          }))
        )
        setBreakdownError(null)
      })
      .catch((err) => {
        if (requestId !== latestRequestIdRef.current) return
        setBreakdownError(err instanceof Error ? err.message : 'Failed to load breakdown')
      })
      .finally(() => {
        if (requestId === latestRequestIdRef.current) {
          setBreakdownLoading(false)
        }
      })
  }, [groupBy, dateFrom, dateTo, debouncedProvider])

  useEffect(() => {
    fetchBreakdown()
    const interval = setInterval(fetchBreakdown, 30000)
    return () => clearInterval(interval)
  }, [fetchBreakdown])

  const sorted = useMemo(() => {
    const copy = [...breakdown]
    const dir = sortDir === 'asc' ? 1 : -1
    copy.sort((a, b) => {
      let cmp = 0
      switch (sortKey) {
        case 'group': cmp = a.group.localeCompare(b.group); break
        case 'calls': cmp = a.calls - b.calls; break
        case 'input_tokens': cmp = a.input_tokens - b.input_tokens; break
        case 'output_tokens': cmp = a.output_tokens - b.output_tokens; break
        case 'cache_read_tokens': cmp = a.cache_read_tokens - b.cache_read_tokens; break
        case 'cache_write_tokens': cmp = a.cache_write_tokens - b.cache_write_tokens; break
        case 'cost_usd': cmp = a.cost_usd - b.cost_usd; break
        case 'avg_duration_ms': cmp = a.avg_duration_ms - b.avg_duration_ms; break
        default: cmp = 0
      }
      return cmp * dir
    })
    return copy
  }, [breakdown, sortKey, sortDir])

  const clearDates = useCallback(() => {
    setDateFrom('')
    setDateTo('')
  }, [])

  return (
    <TooltipProvider delayDuration={200}>
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-display text-xl font-bold text-text-primary">Token Usage</h1>
        <p className="mt-0.5 text-sm text-text-tertiary inline-flex items-center gap-1">
          AI provider token consumption and costs
          <Tooltip>
            <TooltipTrigger aria-label="About cost calculation">
              <Info className="h-3.5 w-3.5 text-text-tertiary cursor-help" aria-hidden="true" />
            </TooltipTrigger>
            <TooltipContent side="right" className="max-w-xs">
              Cost is calculated from token counts using LiteLLM pricing data when not provided natively by the AI provider.
            </TooltipContent>
          </Tooltip>
        </p>
      </div>

      {/* Errors */}
      {summaryError && (
        <p role="alert" className="text-center text-signal-red py-4">{summaryError}</p>
      )}
      {breakdownError && (
        <p role="alert" className="text-center text-signal-red py-4">{breakdownError}</p>
      )}

      {/* Summary cards */}
      {summaryLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full" />
          ))}
        </div>
      ) : summary && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <SummaryCard
            title="Today"
            icon={<Zap className="h-4 w-4 text-signal-blue" />}
            calls={summary.today.calls}
            tokens={summary.today.tokens}
            inputTokens={summary.today.input_tokens}
            outputTokens={summary.today.output_tokens}
            cost={summary.today.cost_usd}
          />
          <SummaryCard
            title="Last 7 Days"
            icon={<TrendingUp className="h-4 w-4 text-signal-green" />}
            calls={summary.this_week.calls}
            tokens={summary.this_week.tokens}
            inputTokens={summary.this_week.input_tokens}
            outputTokens={summary.this_week.output_tokens}
            cost={summary.this_week.cost_usd}
          />
          <SummaryCard
            title="Last 30 Days"
            icon={<Calendar className="h-4 w-4 text-signal-orange" />}
            calls={summary.this_month.calls}
            tokens={summary.this_month.tokens}
            inputTokens={summary.this_month.input_tokens}
            outputTokens={summary.this_month.output_tokens}
            cost={summary.this_month.cost_usd}
          />
        </div>
      )}

      {/* Top models & top jobs */}
      {summary && (summary.top_models.length > 0 || summary.top_jobs.length > 0) && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {summary.top_models.length > 0 && (
            <Card>
              <CardContent className="p-4">
                <h3 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-3">Top Models</h3>
                <div className="space-y-2">
                  {summary.top_models.map((m) => (
                    <div key={m.model} className="flex items-center justify-between">
                      <span className="font-mono text-xs text-text-secondary truncate">{m.model}</span>
                      <div className="flex items-center gap-3">
                        <span className="font-mono text-xs text-text-tertiary">{m.calls.toLocaleString()} calls</span>
                        <span className="font-mono text-xs text-signal-green">{formatCostCell(m.cost_usd)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}
          {summary.top_jobs.length > 0 && (
            <Card>
              <CardContent className="p-4">
                <h3 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-3">Top Jobs</h3>
                <div className="space-y-2">
                  {summary.top_jobs.map((j) => (
                    <div key={j.job_id} className="flex items-center justify-between">
                      <span className="font-mono text-xs text-text-secondary truncate">{j.job_id}</span>
                      <div className="flex items-center gap-3">
                        <span className="font-mono text-xs text-text-tertiary">{j.calls.toLocaleString()} calls</span>
                        <span className="font-mono text-xs text-signal-green">{formatCostCell(j.cost_usd)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex items-center gap-2">
          <DollarSign className="h-3.5 w-3.5 text-text-tertiary" />
          <Select value={groupBy} onValueChange={(v) => setGroupBy(v as GroupByValue)}>
            <SelectTrigger aria-label="Group by" className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {GROUP_BY_OPTIONS.map(({ value, label }) => (
                <SelectItem key={value} value={value}>{label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <DateRangeFilter from={dateFrom} to={dateTo} onFromChange={setDateFrom} onToChange={setDateTo} onClear={clearDates} />
        <Input
          value={providerInput}
          onChange={(e) => setProviderInput(e.target.value)}
          placeholder="Filter by provider..."
          className="h-9 w-full sm:w-44 text-xs"
          aria-label="Filter by provider"
        />
      </div>

      {/* Breakdown table */}
      {breakdownLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-11 w-full" />
          ))}
        </div>
      ) : sorted.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 text-center">
          <p className="text-text-secondary">No token usage data found.</p>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="bg-surface-card hover:bg-surface-card">
              <SortableHeader label="Group" sortKey="group" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} />
              <SortableHeader label="Calls" sortKey="calls" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Input Tokens" sortKey="input_tokens" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Output Tokens" sortKey="output_tokens" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Cache Read" sortKey="cache_read_tokens" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Cache Write" sortKey="cache_write_tokens" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Cost" sortKey="cost_usd" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
              <SortableHeader label="Avg Duration" sortKey="avg_duration_ms" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((row, i) => (
              <TableRow
                key={row.group}
                className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}
              >
                <TableCell className="font-mono text-sm text-text-primary">{row.group}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-secondary">{row.calls.toLocaleString()}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-secondary">{formatCompactNumber(row.input_tokens)}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-secondary">{formatCompactNumber(row.output_tokens)}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-secondary">{formatCompactNumber(row.cache_read_tokens)}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-secondary">{formatCompactNumber(row.cache_write_tokens)}</TableCell>
                <TableCell className="text-right font-mono text-xs text-signal-green">{formatCostCell(row.cost_usd)}</TableCell>
                <TableCell className="text-right font-mono text-xs text-text-tertiary">{formatDurationMs(row.avg_duration_ms)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
    </TooltipProvider>
  )
}
