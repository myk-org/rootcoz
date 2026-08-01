import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { ChevronDown, ChevronRight, Search } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { TestEntriesResponse } from '@/types'

interface TestListSectionProps {
  jobId: string
  status: 'passed' | 'skipped' | 'failed'
  count: number
  childJobName?: string
  childBuildNumber?: number
  defaultExpanded?: boolean
}

const STATUS_CONFIG = {
  passed: { label: 'Passed Tests', icon: '✓', color: 'text-signal-green', bg: 'bg-signal-green/5' },
  skipped: { label: 'Skipped Tests', icon: '⊘', color: 'text-signal-orange', bg: 'bg-signal-orange/5' },
  failed: { label: 'Failed Tests', icon: '✗', color: 'text-signal-red', bg: 'bg-signal-red/5' },
} as const

const PAGE_SIZE = 50

export function TestListSection({ jobId, status, count, childJobName, childBuildNumber, defaultExpanded = false }: TestListSectionProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [entries, setEntries] = useState<TestEntriesResponse['entries']>([])
  const [total, setTotal] = useState(count)
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [search, setSearch] = useState('')

  const config = STATUS_CONFIG[status]

  const fetchEntries = useCallback(async (offset: number) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        status,
        offset: String(offset),
        limit: String(PAGE_SIZE),
      })
      if (childJobName) {
        params.set('child_job_name', childJobName)
        if (childBuildNumber !== undefined) {
          params.set('child_build_number', String(childBuildNumber))
        }
      }
      const data = await api.get<TestEntriesResponse>(`/api/results/${jobId}/tests?${params}`)
      if (offset === 0) {
        setEntries(data.entries)
      } else {
        setEntries(prev => [...prev, ...data.entries])
      }
      setTotal(data.total)
      setLoaded(true)
    } catch {
      // best-effort
    } finally {
      setLoading(false)
    }
  }, [jobId, status, childJobName, childBuildNumber])

  // Fetch on first expand
  useEffect(() => {
    if (expanded && !loaded) {
      fetchEntries(0)
    }
  }, [expanded, loaded, fetchEntries])

  if (count === 0) return null

  const filtered = search
    ? entries.filter(e => e.test_name.toLowerCase().includes(search.toLowerCase()))
    : entries

  const hasMore = entries.length < total

  return (
    <div className={`rounded-lg border border-border-default ${config.bg} animate-slide-up`}>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-3 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? <ChevronDown className="h-4 w-4 text-text-tertiary" /> : <ChevronRight className="h-4 w-4 text-text-tertiary" />}
        <span className={`text-sm font-medium ${config.color}`}>{config.icon}</span>
        <span className="text-sm font-medium text-text-primary">{config.label}</span>
        <span className="text-xs text-text-tertiary">({count})</span>
      </button>

      {expanded && (
        <div className="border-t border-border-default px-4 pb-3">
          {/* Search */}
          {total > 10 && (
            <div className="relative mt-3 mb-2">
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-text-tertiary" />
              <Input
                type="text"
                placeholder="Filter tests..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-8 text-xs"
              />
            </div>
          )}

          {/* Entries */}
          {loading && entries.length === 0 ? (
            <p className="py-4 text-center text-xs text-text-tertiary">Loading...</p>
          ) : filtered.length === 0 ? (
            <p className="py-4 text-center text-xs text-text-tertiary">
              {search ? 'No matching tests' : 'No entries'}
            </p>
          ) : (
            <div className="mt-2 space-y-0.5 max-h-96 overflow-y-auto">
              {filtered.map((entry, i) => (
                <div key={`${entry.test_name}-${i}`} className="flex items-center justify-between py-1 px-2 rounded text-xs hover:bg-surface-elevated/50">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="truncate text-text-secondary flex-1 mr-2">
                        {entry.test_name}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent>{entry.test_name}</TooltipContent>
                  </Tooltip>
                  <span className="text-text-tertiary whitespace-nowrap">
                    {entry.duration > 0 ? `${entry.duration.toFixed(2)}s` : ''}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Load more */}
          {hasMore && !search && (
            <button
              type="button"
              className="mt-2 text-xs text-text-link hover:underline disabled:opacity-50"
              onClick={() => fetchEntries(entries.length)}
              disabled={loading}
            >
              {loading ? 'Loading...' : `Load more (${total - entries.length} remaining)`}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
