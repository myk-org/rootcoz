import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '@/lib/api'
import { GITHUB_REPO_URL } from '@/lib/constants'
import type { DashboardJob, DashboardJobWithMetadata } from '@/types'
import { Button } from '@/components/ui/button'
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { Skeleton } from '@/components/ui/skeleton'
import { parseApiTimestamp, isAnalysisTimeout, formatDuration, formatTimestamp } from '@/lib/utils'
import { utcStartOfDateInput, utcEndOfDateInput } from '@/lib/dateRange'
import { StatusChip } from '@/components/shared/StatusChip'
import { SearchInput } from '@/components/shared/SearchInput'
import { Pagination } from '@/components/shared/Pagination'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { SortableHeader } from '@/components/shared/SortableHeader'
import { DateRangeFilter } from '@/components/shared/DateRangeFilter'
import { useTableSort } from '@/lib/useTableSort'
import { Trash2, MessageSquare, CheckCircle2, GitFork, AlertTriangle, Github } from 'lucide-react'
import { useAuth } from '@/lib/auth'
import { useMetadataOptions, MetadataDropdowns, MetadataLabelChips, MetadataClearButton } from '@/components/shared/MetadataFilterBar'
import { MetadataBadges } from '@/components/shared/MetadataBadges'
import { NotificationPrompt } from '@/components/shared/NotificationPrompt'
import { WhatsNewDialog } from '@/components/shared/WhatsNewDialog'

const STATUS_FILTER_ALL = 'ALL'
const STATUS_FILTER_OPTIONS = [STATUS_FILTER_ALL, 'completed', 'running', 'waiting', 'pending', 'failed', 'timeout'] as const
const BULK_DELETE_LIMIT = 500

const BULK_SELECT_CHECKBOX_CLASS =
  "h-4 w-4 cursor-pointer appearance-none rounded border border-text-tertiary bg-surface-elevated checked:bg-signal-blue checked:border-signal-blue checked:bg-[url('data:image/svg+xml;charset=utf-8,%3Csvg%20viewBox%3D%220%200%2016%2016%22%20fill%3D%22white%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cpath%20d%3D%22M12.207%204.793a1%201%200%20010%201.414l-5%205a1%201%200%2001-1.414%200l-2-2a1%201%200%20011.414-1.414L6.5%209.086l4.293-4.293a1%201%200%20011.414%200z%22%2F%3E%3C%2Fsvg%3E')] checked:bg-no-repeat checked:bg-center transition-all"

function MetricCell({ value, displayValue, icon, tone, tooltipText }: {
  value: number | null | undefined
  displayValue?: ReactNode
  icon: ReactNode
  tone: string
  tooltipText: string
}) {
  if (!value || value <= 0) {
    return (
      <TableCell className="text-center">
        <span className="text-xs text-text-tertiary">—</span>
      </TableCell>
    )
  }
  return (
    <TableCell className="text-center">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={`inline-flex items-center gap-1 font-mono text-xs ${tone}`}>
            {icon}
            {displayValue ?? value}
          </span>
        </TooltipTrigger>
        <TooltipContent>{tooltipText}</TooltipContent>
      </Tooltip>
    </TableCell>
  )
}

const STATUS_BORDER: Record<string, string> = {
  completed: 'border-l-signal-green',
  running: 'border-l-signal-blue',
  waiting: 'border-l-signal-blue',
  pending: 'border-l-border-default',
  failed: 'border-l-signal-red',
  timeout: 'border-l-signal-orange',
}

function relativeTime(iso: string): string {
  // SQLite timestamps are UTC but lack timezone marker — normalize to ISO 8601
  const parsed = parseApiTimestamp(iso)
  const timestamp = parsed.getTime()
  if (Number.isNaN(timestamp)) return '\u2014'
  const diff = Date.now() - timestamp
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function getJobDisplayName(job: DashboardJob | null | undefined): string {
  return job?.job_name || job?.job_id || ''
}

export function DashboardPage() {
  const navigate = useNavigate()
  const { isAdmin } = useAuth()
  const [jobs, setJobs] = useState<DashboardJobWithMetadata[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState(STATUS_FILTER_ALL)
  const { sortKey, sortDir, handleSort } = useTableSort('dash', 'created_at', 'desc', ['created_at'])
  const [page, setPage] = useState(1)
  const [perPage, setPerPage] = useState(20)
  const [searchParams, setSearchParams] = useSearchParams()
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''

  // Metadata filter state — persisted in URL query params
  const metaTeam = searchParams.get('team') ?? ''
  const metaTier = searchParams.get('tier') ?? ''
  const metaVersion = searchParams.get('version') ?? ''
  const metaLabels = useMemo(() => searchParams.getAll('label'), [searchParams])
  const setMetaParam = useCallback((key: string, value: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    }, { replace: true })
  }, [setSearchParams])
  const setMetaLabels = useCallback((labels: string[]) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('label')
      for (const l of labels) next.append('label', l)
      return next
    }, { replace: true })
  }, [setSearchParams])
  const clearMetadataFilters = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('team')
      next.delete('tier')
      next.delete('version')
      next.delete('label')
      return next
    }, { replace: true })
  }, [setSearchParams])
  const hasMetadataFilters = !!(metaTeam || metaTier || metaVersion || metaLabels.length > 0)
  const { options: metadataOptions } = useMetadataOptions()
  const setDateFrom = useCallback((value: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set('date_from', value)
      else next.delete('date_from')
      return next
    }, { replace: true })
  }, [setSearchParams])
  const setDateTo = useCallback((value: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set('date_to', value)
      else next.delete('date_to')
      return next
    }, { replace: true })
  }, [setSearchParams])
  const clearDates = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('date_from')
      next.delete('date_to')
      return next
    }, { replace: true })
  }, [setSearchParams])
  const [deleteTarget, setDeleteTarget] = useState<DashboardJob | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [bulkDeleteConfirm, setBulkDeleteConfirm] = useState(false)
  const [bulkResultMessage, setBulkResultMessage] = useState<string | null>(null)
  const selectAllRef = useRef<HTMLInputElement>(null)

  const showCheckboxes = selectedIds.size > 0

  useEffect(() => {
    if (!isAdmin && selectedIds.size > 0) {
      clearSelection()
      setBulkDeleteConfirm(false)
    }
  }, [isAdmin, selectedIds])

  const fetchSeqRef = useRef(0)

  const fetchJobs = useCallback(async () => {
    const thisSeq = ++fetchSeqRef.current
    try {
      let url = '/api/dashboard/filtered'
      const params = new URLSearchParams()
      if (metaTeam) params.set('team', metaTeam)
      if (metaTier) params.set('tier', metaTier)
      if (metaVersion) params.set('version', metaVersion)
      for (const l of metaLabels) params.append('label', l)
      const qs = params.toString()
      if (qs) url += `?${qs}`
      const data = await api.get<DashboardJobWithMetadata[]>(url)
      if (thisSeq === fetchSeqRef.current) {
        setError(null)
        setJobs(data)
      }
    } catch (err) {
      if (thisSeq === fetchSeqRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load dashboard')
      }
    } finally {
      if (thisSeq === fetchSeqRef.current) {
        setLoading(false)
      }
    }
  }, [metaTeam, metaTier, metaVersion, metaLabels])

  useEffect(() => {
    fetchJobs()
    const interval = setInterval(fetchJobs, 10_000)
    return () => clearInterval(interval)
  }, [fetchJobs])
  useEffect(() => { setPage(1) }, [search, statusFilter, perPage, dateFrom, dateTo, metaTeam, metaTier, metaVersion, metaLabels])

  const filtered = useMemo(() => {
    const fromBound = dateFrom ? utcStartOfDateInput(dateFrom) : null
    const toBound = dateTo ? utcEndOfDateInput(dateTo) : null

    return jobs.filter((j) => {
      const displayStatus = isAnalysisTimeout(j.status, j.error, j.summary) ? 'timeout' : j.status
      if (statusFilter !== STATUS_FILTER_ALL && displayStatus !== statusFilter) return false

      if (fromBound || toBound) {
        const jobDate = parseApiTimestamp(j.created_at)
        if (Number.isNaN(jobDate.getTime())) return false
        if (fromBound && jobDate < fromBound) return false
        if (toBound && jobDate > toBound) return false
      }

      if (!search) return true
      const q = search.toLowerCase()
      return (j.job_name ?? '').toLowerCase().includes(q) || j.job_id.toLowerCase().includes(q)
    })
  }, [jobs, search, statusFilter, dateFrom, dateTo])

  const sorted = useMemo(() => {
    const copy = [...filtered]
    const dir = sortDir === 'asc' ? 1 : -1
    copy.sort((a, b) => {
      let cmp = 0
      switch (sortKey) {
        case 'job_name': cmp = (a.job_name ?? '').localeCompare(b.job_name ?? ''); break
        case 'status': {
          const sa = isAnalysisTimeout(a.status, a.error, a.summary) ? 'timeout' : a.status
          const sb = isAnalysisTimeout(b.status, b.error, b.summary) ? 'timeout' : b.status
          cmp = sa.localeCompare(sb)
          break
        }
        case 'failure_count': cmp = (a.failure_count ?? 0) - (b.failure_count ?? 0); break
        case 'reviewed_count': cmp = a.reviewed_count - b.reviewed_count; break
        case 'comment_count': cmp = a.comment_count - b.comment_count; break
        case 'child_job_count': cmp = (a.child_job_count ?? 0) - (b.child_job_count ?? 0); break
        case 'created_at': cmp = a.created_at.localeCompare(b.created_at); break
        default: cmp = 0
      }
      return cmp * dir
    })
    return copy
  }, [filtered, sortKey, sortDir])

  const totalPages = Math.max(1, Math.ceil(sorted.length / perPage))
  const safePage = Math.min(page, totalPages)
  const pageJobs = sorted.slice((safePage - 1) * perPage, safePage * perPage)

  const pageJobIds = useMemo(() => pageJobs.map(j => j.job_id), [pageJobs])
  const allPageSelected = pageJobIds.length > 0 && pageJobIds.every(id => selectedIds.has(id))
  const somePageSelected = pageJobIds.some(id => selectedIds.has(id))

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = somePageSelected && !allPageSelected
    }
  }, [somePageSelected, allPageSelected])

  useEffect(() => {
    if (page !== safePage) setPage(safePage)
  }, [page, safePage])

  function toggleSelect(jobId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  function toggleSelectAll() {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allPageSelected) {
        pageJobIds.forEach(id => next.delete(id))
      } else {
        pageJobIds.forEach(id => next.add(id))
      }
      return next
    })
  }

  function clearSelection() {
    setSelectedIds(new Set())
    setBulkResultMessage(null)
  }

  const bulkDeleteDescription = useMemo(() => {
    const names = jobs.filter((j) => selectedIds.has(j.job_id)).map((j) => j.job_name || j.job_id)
    return `Permanently delete ${names.join(', ')}? This cannot be undone.`
  }, [jobs, selectedIds])

  function enforceBulkDeleteLimit(count: number, closeConfirm = false): boolean {
    if (count <= BULK_DELETE_LIMIT) return true
    setBulkResultMessage(`Select ${BULK_DELETE_LIMIT} or fewer jobs to bulk delete.`)
    if (closeConfirm) setBulkDeleteConfirm(false)
    return false
  }

  async function handleBulkDelete() {
    const jobIdsToDelete = [...selectedIds]
    if (jobIdsToDelete.length === 0) return
    if (!enforceBulkDeleteLimit(jobIdsToDelete.length, true)) return
    setBulkDeleting(true)
    try {
      const data = await api.delete<{ deleted: string[]; failed: { job_id: string; reason: string }[]; total?: number }>(
        '/api/results/bulk',
        { job_ids: jobIdsToDelete },
      )
      const deletedSet = new Set(data.deleted)
      fetchSeqRef.current += 1
      setJobs(prev => prev.filter(j => !deletedSet.has(j.job_id)))
      if (data.failed.length > 0) {
        const details = data.failed.map(f => `${f.job_id}: ${f.reason}`).join('; ')
        setSelectedIds(new Set(data.failed.map(f => f.job_id)))
        setBulkResultMessage(
          `Deleted ${data.deleted.length} of ${data.total ?? jobIdsToDelete.length}. Failed: ${details}`
        )
      } else {
        clearSelection()
      }
    } catch (err) {
      setBulkResultMessage(err instanceof Error ? err.message : 'Failed to bulk delete')
    } finally {
      setBulkDeleting(false)
      setBulkDeleteConfirm(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.delete(`/results/${deleteTarget.job_id}`)
      fetchSeqRef.current += 1
      setJobs((prev) => prev.filter((j) => j.job_id !== deleteTarget.job_id))
      setDeleteTarget(null)
    } catch (err) {
      console.error('Failed to delete job:', err)
    } finally {
      setDeleting(false)
    }
  }

  function getJobRoute(job: DashboardJob): string {
    return ['waiting', 'pending', 'running', 'failed'].includes(job.status)
      ? `/status/${job.job_id}`
      : `/results/${job.job_id}`
  }

  function handleRowClick(job: DashboardJob) {
    navigate(getJobRoute(job))
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="space-y-6">
        {/* Filters — single wrapping row */}
        <div className="flex flex-wrap gap-3 items-center">
          <SearchInput value={search} onChange={setSearch} placeholder="Filter jobs..." className="w-full sm:w-64" />
          <MetadataDropdowns
            options={metadataOptions}
            team={metaTeam}
            tier={metaTier}
            version={metaVersion}
            onTeamChange={(v) => setMetaParam('team', v)}
            onTierChange={(v) => setMetaParam('tier', v)}
            onVersionChange={(v) => setMetaParam('version', v)}
          />
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger aria-label="Filter by status" className="w-full sm:w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_FILTER_OPTIONS.map((s) => (
                <SelectItem key={s} value={s}>
                  {s === STATUS_FILTER_ALL ? 'All statuses' : s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DateRangeFilter from={dateFrom} to={dateTo} onFromChange={setDateFrom} onToChange={setDateTo} onClear={clearDates} />
          <Select value={String(perPage)} onValueChange={(v) => setPerPage(Number(v))}>
            <SelectTrigger aria-label="Rows per page" className="w-full sm:w-20">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="10">10</SelectItem>
              <SelectItem value="20">20</SelectItem>
              <SelectItem value="50">50</SelectItem>
            </SelectContent>
          </Select>
          <MetadataClearButton hasFilters={hasMetadataFilters} onClearAll={clearMetadataFilters} />
        </div>

        {/* Tag filter chips — only shown when labels exist */}
        <MetadataLabelChips
          allLabels={metadataOptions.allLabels}
          labels={metaLabels}
          onLabelsChange={setMetaLabels}
        />

        {/* Summary row: count + View Issues */}
        <div className="flex items-center gap-3">
          <p className="text-sm text-text-tertiary">
            {filtered.length} analysis {filtered.length === 1 ? 'run' : 'runs'}
          </p>
          <a
            href={`${GITHUB_REPO_URL}/issues`}
            target="_blank"
            rel="noopener noreferrer"
            title="View GitHub issues"
            className="flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
          >
            <Github className="h-3 w-3" />
            View Issues
          </a>
        </div>

        {/* Bulk result message */}
        {bulkResultMessage && (
          <div role="alert" className="flex items-center justify-between rounded-lg border border-signal-red/30 bg-signal-red/10 px-4 py-3 text-sm text-signal-red">
            <span>{bulkResultMessage}</span>
            <button
              type="button"
              onClick={() => setBulkResultMessage(null)}
              className="ml-4 shrink-0 rounded p-1 hover:bg-signal-red/20 transition-colors"
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
        )}

        {/* Error */}
        {error && (
          <p role="alert" className="text-center text-signal-red py-8">
            {error}
          </p>
        )}

        {/* Table */}
        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-11 w-full" />
            ))}
          </div>
        ) : pageJobs.length === 0 && (!error || jobs.length > 0) ? (
          <div className="flex flex-col items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 text-center animate-fade-in">
            <p className="text-text-secondary">
              {search || statusFilter !== STATUS_FILTER_ALL || dateFrom || dateTo || hasMetadataFilters
                ? 'No jobs match your filters.'
                : 'No analysis runs yet.'}
            </p>
          </div>
        ) : error && jobs.length === 0 ? null : (
          <Table>
            <TableHeader>
              <TableRow className="bg-surface-card hover:bg-surface-card">
                {isAdmin && (
                  <TableHead className="w-10">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      checked={allPageSelected}
                      onChange={toggleSelectAll}
                      className={BULK_SELECT_CHECKBOX_CLASS}
                      aria-label="Select all"
                    />
                  </TableHead>
                )}
                <SortableHeader label="Job" sortKey="job_name" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="w-[40%]" />
                <SortableHeader label="Status" sortKey="status" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} />
                <SortableHeader label="Failures" sortKey="failure_count" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-center" />
                <SortableHeader label="Reviewed" sortKey="reviewed_count" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-center" />
                <SortableHeader label="Comments" sortKey="comment_count" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-center" />
                <SortableHeader label="Children" sortKey="child_job_count" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-center" />
                <SortableHeader label="Created" sortKey="created_at" currentSort={sortKey} currentDirection={sortDir} onSort={handleSort} className="text-right" />
                {isAdmin && (
                  <TableHead className="w-10">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageJobs.map((job, i) => {
                const displayStatus = isAnalysisTimeout(job.status, job.error, job.summary) ? 'timeout' : job.status
                const borderColor = STATUS_BORDER[displayStatus] ?? 'border-l-border-default'
                const failureCount = job.failure_count ?? 0
                const failureHint = job.summary || job.error
                const rowDest = getJobRoute(job)

                return (
                  <TableRow
                    key={job.job_id}
                    className={`group cursor-pointer animate-slide-up ${selectedIds.has(job.job_id) ? 'bg-accent-blue/10' : i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}`}
                    style={{ animationDelay: `${i * 30}ms`, animationFillMode: 'backwards' }}
                    onClick={() => isAdmin && selectedIds.size > 0 ? toggleSelect(job.job_id) : handleRowClick(job)}
                  >
                    {isAdmin && (
                      <TableCell className="w-10">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(job.job_id)}
                          onChange={(e) => { e.stopPropagation(); toggleSelect(job.job_id) }}
                          onClick={(e) => e.stopPropagation()}
                          className={`${BULK_SELECT_CHECKBOX_CLASS} ${showCheckboxes ? '' : 'opacity-0 group-hover:opacity-100'}`}
                          aria-label={`Select ${getJobDisplayName(job)}`}
                        />
                      </TableCell>
                    )}
                    {/* Job name + build (with left accent border) */}
                    <TableCell className={`border-l-4 ${borderColor}`}>
                      <div>
                        <Link
                          to={rowDest}
                          className="font-display text-sm font-medium text-text-primary hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-blue"
                          onClick={(e) => e.stopPropagation()}
                          aria-label={`Open ${getJobDisplayName(job)}${job.build_number !== undefined ? ` #${job.build_number}` : ''}`}
                        >
                          {getJobDisplayName(job)}
                        </Link>
                        {job.build_number !== undefined && (
                          <span className="ml-2 font-mono text-xs text-text-tertiary">#{job.build_number}</span>
                        )}
                      </div>
                      <MetadataBadges metadata={job.metadata} />
                      {failureHint && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <p className="mt-0.5 max-w-xs truncate text-xs text-text-tertiary">{failureHint}</p>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-sm whitespace-pre-wrap">{failureHint}</TooltipContent>
                        </Tooltip>
                      )}
                    </TableCell>

                    {/* Status */}
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span><StatusChip status={displayStatus} /></span>
                        </TooltipTrigger>
                        <TooltipContent>{displayStatus === 'timeout' ? 'AI analysis timed out' : `Analysis status: ${job.status}`}</TooltipContent>
                      </Tooltip>
                    </TableCell>

                    {/* Failures */}
                    <MetricCell
                      value={failureCount}
                      icon={<AlertTriangle className="h-3 w-3" />}
                      tone="text-signal-red"
                      tooltipText={`${failureCount} test ${failureCount === 1 ? 'failure' : 'failures'}`}
                    />

                    {/* Reviewed */}
                    <MetricCell
                      value={failureCount}
                      displayValue={<>{job.reviewed_count}/{failureCount}</>}
                      icon={<CheckCircle2 className="h-3 w-3" />}
                      tone={
                        job.reviewed_count === failureCount
                          ? 'text-signal-green'
                          : job.reviewed_count > 0
                            ? 'text-signal-orange'
                            : 'text-signal-red'
                      }
                      tooltipText={`${job.reviewed_count} of ${failureCount} failures reviewed`}
                    />

                    {/* Comments */}
                    <MetricCell
                      value={job.comment_count}
                      icon={<MessageSquare className="h-3 w-3" />}
                      tone="text-text-secondary"
                      tooltipText={`${job.comment_count} ${job.comment_count === 1 ? 'comment' : 'comments'}`}
                    />

                    {/* Children */}
                    <MetricCell
                      value={job.child_job_count}
                      icon={<GitFork className="h-3 w-3" />}
                      tone="text-text-secondary"
                      tooltipText={`${job.child_job_count ?? 0} child ${job.child_job_count === 1 ? 'job' : 'jobs'}`}
                    />

                    {/* Created */}
                    <TableCell className="text-right">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="font-mono text-xs text-text-tertiary">{relativeTime(job.created_at)}</span>
                        </TooltipTrigger>
                        <TooltipContent>
                          <span>Created: {formatTimestamp(job.created_at)}</span>
                          {(() => {
                            const startTime = job.analysis_started_at || job.created_at
                            const start = startTime ? parseApiTimestamp(startTime) : null
                            const end = job.completed_at ? parseApiTimestamp(job.completed_at) : null
                            const duration = start && end && !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime())
                              ? formatDuration(start, end)
                              : null
                            return duration ? (
                              <>
                                <br />
                                <span>Analysis took: {duration}</span>
                              </>
                            ) : null
                          })()}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>

                    {/* Delete */}
                    {isAdmin && (
                      <TableCell>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          aria-label={`Delete analysis ${getJobDisplayName(job)}`}
                          className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                          onClick={(e) => {
                            e.stopPropagation()
                            setDeleteTarget(job)
                          }}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-red" />
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}

        {(!error || jobs.length > 0) && (
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
        )}

        {isAdmin && selectedIds.size > 0 && (
          <div className="fixed bottom-0 left-0 right-0 z-50 border-t border-border-muted bg-surface-card/95 backdrop-blur-sm px-6 py-3 animate-slide-up">
            <div className="mx-auto flex max-w-screen-xl items-center justify-between">
              <span className="text-sm text-text-secondary">
                {selectedIds.size} {selectedIds.size === 1 ? 'job' : 'jobs'} selected
              </span>
              <div className="flex items-center gap-3">
                <Button variant="ghost" size="sm" onClick={clearSelection}>
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => {
                    if (!enforceBulkDeleteLimit(selectedIds.size)) return
                    setBulkDeleteConfirm(true)
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5 mr-1.5" />
                  Delete ({selectedIds.size})
                </Button>
              </div>
            </div>
          </div>
        )}

        <ConfirmDialog
          open={deleteTarget !== null}
          onOpenChange={(open) => { if (!open) setDeleteTarget(null) }}
          title="Delete analysis"
          description={`Permanently delete "${getJobDisplayName(deleteTarget)}"? This cannot be undone.`}
          confirmLabel="Delete"
          variant="destructive"
          onConfirm={handleDelete}
          loading={deleting}
        />

        <ConfirmDialog
          open={isAdmin && bulkDeleteConfirm}
          onOpenChange={(open) => { if (!open) setBulkDeleteConfirm(false) }}
          title="Delete selected analyses"
          description={bulkDeleteDescription}
          confirmLabel={`Delete ${selectedIds.size}`}
          variant="destructive"
          onConfirm={handleBulkDelete}
          loading={bulkDeleting}
        />

        <NotificationPrompt />

        <WhatsNewDialog />
      </div>
    </TooltipProvider>
  )
}
