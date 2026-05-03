import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '@/lib/api'
import { parseApiTimestamp } from '@/lib/utils'
import type { FailureHistoryEntry } from '@/types'
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { SearchInput } from '@/components/shared/SearchInput'
import { Pagination } from '@/components/shared/Pagination'
import { ClassificationBadge } from '@/components/shared/ClassificationBadge'
import { SortableHeader } from '@/components/shared/SortableHeader'
import { useTableSort } from '@/lib/useTableSort'
import { CLASSIFICATIONS } from '@/constants/classifications'

const CLASSIFICATION_FILTER_OPTIONS = ['ALL', ...CLASSIFICATIONS] as const

const LIMIT = 50

/* ================================================================== */
/*  HistoryPage                                                        */
/* ================================================================== */

export function HistoryPage() {
  return (
    <div className="space-y-6">
      <h1 className="font-display text-xl font-bold text-text-primary">
        Failure History
      </h1>

      <FailureHistoryTab />
    </div>
  )
}

/* ================================================================== */
/*  Failure History Tab                                                 */
/* ================================================================== */

function FailureHistoryTab() {
  const navigate = useNavigate()
  const [data, setData] = useState<FailureHistoryEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [inputValue, setInputValue] = useState('')
  const [search, setSearch] = useState('')
  const [classification, setClassification] = useState('ALL')
  const { sortKey, sortDir, handleSort } = useTableSort('hist', 'analyzed_at', 'desc', ['analyzed_at'])
  const [page, setPage] = useState(1)
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null)
  const requestSeqRef = useRef(0)

  const clearDebounce = useCallback(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }
  }, [])

  const fetchData = useCallback(
    async (s: string, cls: string, p: number) => {
      const seq = ++requestSeqRef.current
      setLoading(true)
      setError(null)
      try {
        const params = new URLSearchParams({
          limit: String(LIMIT),
          offset: String((p - 1) * LIMIT),
        })
        if (s) params.set('search', s)
        if (cls && cls !== 'ALL') params.set('classification', cls)

        const res = await api.get<{ failures: FailureHistoryEntry[]; total: number }>(
          `/history/failures?${params}`,
        )
        if (seq === requestSeqRef.current) {
          setData(res.failures)
          setTotal(res.total)
        }
      } catch (err) {
        if (seq === requestSeqRef.current) {
          setError(err instanceof Error ? err.message : 'Failed to load history')
        }
      } finally {
        if (seq === requestSeqRef.current) {
          setLoading(false)
        }
      }
    },
    [],
  )

  // Fetch on page/search/classification change
  useEffect(() => {
    fetchData(search, classification, page)
  }, [page, classification, fetchData, search])

  // Cleanup debounce on unmount
  useEffect(() => {
    return () => {
      clearDebounce()
      requestSeqRef.current += 1
    }
  }, [clearDebounce])

  // Debounce search — only updates state after delay
  function handleSearch(v: string) {
    setInputValue(v)
    clearDebounce()
    debounceRef.current = setTimeout(() => {
      setSearch(v)
      setPage(1)
    }, 300)
  }

  function handleClassification(v: string) {
    clearDebounce()
    setSearch(inputValue)
    setClassification(v)
    setPage(1)
  }

  // Client-side sort applies to the current page only. Server-side sorting
  // would require adding sort_key/sort_dir query params to the API.
  const sorted = useMemo(() => {
    const copy = [...data]
    const dir = sortDir === 'asc' ? 1 : -1
    copy.sort((a, b) => {
      let cmp = 0
      switch (sortKey) {
        case 'test_name': cmp = a.test_name.localeCompare(b.test_name); break
        case 'job_name': cmp = a.job_name.localeCompare(b.job_name); break
        case 'classification': cmp = (a.classification ?? '').localeCompare(b.classification ?? ''); break
        case 'analyzed_at': cmp = a.analyzed_at.localeCompare(b.analyzed_at); break
        default: cmp = 0
      }
      return cmp * dir
    })
    return copy
  }, [data, sortKey, sortDir])

  const totalPages = Math.max(1, Math.ceil(total / LIMIT))

  return (
    <div className="space-y-4 pt-4">
      {/* Filters */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <SearchInput
          value={inputValue}
          onChange={handleSearch}
          placeholder="Search test names..."
          className="w-full sm:w-72"
        />
        <Select value={classification} onValueChange={handleClassification}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="Classification" />
          </SelectTrigger>
          <SelectContent>
            {CLASSIFICATION_FILTER_OPTIONS.map((c) => (
              <SelectItem key={c} value={c}>
                {c}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="ml-auto text-xs text-text-tertiary font-mono">
          {total} result{total !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Error */}
      {error && <p className="text-center text-signal-red py-8">{error}</p>}

      {/* Table */}
      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : !error && data.length === 0 ? (
        <div className="flex items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 animate-fade-in">
          <p className="text-text-secondary">No failures found.</p>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="bg-surface-card">
              <SortableHeader label="Test Name" sortKey="test_name" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="w-[40%]" />
              <SortableHeader label="Job" sortKey="job_name" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} />
              <SortableHeader label="Classification" sortKey="classification" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} />
              <SortableHeader label="Date" sortKey="analyzed_at" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((entry, i) => (
              <TableRow
                key={entry.id}
                className={`cursor-pointer animate-slide-up ${i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}`}
                style={{
                  animationDelay: `${i * 30}ms`,
                  animationFillMode: 'backwards',
                }}
                onClick={() => navigate(`/results/${entry.job_id}`)}
                tabIndex={0}
                role="link"
                aria-label={`Open results for ${entry.job_name} #${entry.build_number}`}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    navigate(`/results/${entry.job_id}`)
                  }
                }}
              >
                <TableCell className="font-mono text-xs text-text-primary max-w-[400px]">
                  <Link
                    to={`/history/test/${encodeURIComponent(entry.test_name)}`}
                    className="text-text-link hover:underline truncate block"
                    onClick={(e) => e.stopPropagation()}
                  >
                    {entry.test_name}
                  </Link>
                </TableCell>
                <TableCell>
                  <span className="font-display text-xs text-text-secondary">
                    {entry.job_name}
                  </span>
                  <span className="ml-1 font-mono text-[10px] text-text-tertiary">
                    #{entry.build_number}
                  </span>
                </TableCell>
                <TableCell>
                  {entry.classification && (
                    <ClassificationBadge classification={entry.classification} />
                  )}
                </TableCell>
                <TableCell className="text-right font-mono text-xs text-text-tertiary whitespace-nowrap">
                  {parseApiTimestamp(entry.analyzed_at).toLocaleDateString()}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
    </div>
  )
}
