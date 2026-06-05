import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '@/lib/api'
import { cn, parseApiTimestamp } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from '@/components/ui/collapsible'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useMetadataOptions, MetadataDropdowns, MetadataLabelChips, MetadataClearButton } from '@/components/shared/MetadataFilterBar'
import { DateRangePresetFilter } from '@/components/shared/DateRangePresetFilter'
import { ClassificationBadge } from '@/components/shared/ClassificationBadge'
import { MultiSelectFilter } from '@/components/shared/MultiSelectFilter'
import { Pagination } from '@/components/shared/Pagination'
import { ExpandCollapseButtons } from '@/components/shared/ExpandCollapseButtons'
import { SearchInput } from '@/components/shared/SearchInput'
import { BarChart3, ArrowRightLeft, Bug, ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'

// ─── Types ──────────────────────────────────────────────────────────

interface TotalsData {
  total_jobs: number
  total_failures: number
  total_reviewed: number
  total_details: number
  jobs: Array<{
    job_id: string
    job_name: string
    build_number: number
    failure_count: number
    reviewed_count: number
    created_at: string
  }>
}

interface OverridesData {
  total: number
  groups: Array<{ from: string; to: string; count: number }>
  details: Array<{
    test_name: string
    job_name: string
    job_id: string
    build_number: number
    from_classification: string
    to_classification: string
    overridden_by: string
    overridden_at: string
  }>
}

interface IssuesData {
  total: number
  github_total: number
  jira_total: number
  issues: Array<{
    issue_type: string
    title: string
    url: string
    test_name: string
    job_name: string
    job_id: string
    build_number: number
    created_by: string
    created_at: string
  }>
}

type ReportTab = 'totals' | 'overrides' | 'issues'

const TABS: { key: ReportTab; label: string; icon: typeof BarChart3 }[] = [
  { key: 'totals', label: 'Total Failures', icon: BarChart3 },
  { key: 'overrides', label: 'Classification Overrides', icon: ArrowRightLeft },
  { key: 'issues', label: 'Issues Created', icon: Bug },
]

/** URL-friendly slug ↔ internal tab key */
const TAB_URL_KEY: Record<ReportTab, string> = {
  totals: 'totals',
  overrides: 'classification-overrides',
  issues: 'issues-created',
}
const URL_KEY_TO_TAB: Record<string, ReportTab> = Object.fromEntries(
  Object.entries(TAB_URL_KEY).map(([k, v]) => [v, k as ReportTab]),
)

const PAGE_SIZE = 20
const STATUS_FILTER_OPTIONS = ['completed', 'running', 'waiting', 'pending', 'failed', 'timeout', 'aborted'] as const

// ─── Reducer ────────────────────────────────────────────────────────

interface ReportsState {
  activeTab: ReportTab
  loading: boolean
  error: string | null
  search: string
  teams: Set<string>
  tiers: Set<string>
  versions: Set<string>
  dateFrom: string
  dateTo: string
  statuses: Set<string>
  labels: string[]
  excludeLabels: string[]
  totalsData: TotalsData | null
  overridesData: OverridesData | null
  issuesData: IssuesData | null
  totalsExpanded: boolean
  overridesExpandedGroups: Set<string>
}

type ReportsAction =
  | { type: 'SET_TAB'; tab: ReportTab }
  | { type: 'SYNC_FROM_URL'; params: URLSearchParams }
  | { type: 'FETCH_START' }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'FETCH_TOTALS'; data: TotalsData }
  | { type: 'FETCH_OVERRIDES'; data: OverridesData }
  | { type: 'FETCH_ISSUES'; data: IssuesData }
  | { type: 'SET_SEARCH'; value: string }
  | { type: 'TOGGLE_META'; field: 'teams' | 'tiers' | 'versions'; value: string }
  | { type: 'CLEAR_META'; field: 'teams' | 'tiers' | 'versions' }
  | { type: 'CLEAR_ALL_META' }
  | { type: 'SET_DATE_RANGE'; from: string; to: string }
  | { type: 'TOGGLE_STATUS'; value: string }
  | { type: 'CLEAR_STATUSES' }
  | { type: 'TOGGLE_LABEL'; label: string; action: 'include' | 'exclude' | 'off' }
  | { type: 'TOGGLE_TOTALS_EXPANDED' }
  | { type: 'SET_TOTALS_EXPANDED'; value: boolean }
  | { type: 'TOGGLE_OVERRIDE_GROUP'; key: string }
  | { type: 'EXPAND_ALL_OVERRIDES'; keys: string[] }
  | { type: 'COLLAPSE_ALL_OVERRIDES' }

const INITIAL_STATE: ReportsState = {
  activeTab: 'totals',
  loading: false,
  error: null,
  search: '',
  teams: new Set(),
  tiers: new Set(),
  versions: new Set(),
  dateFrom: '',
  dateTo: '',
  statuses: new Set(),
  labels: [],
  excludeLabels: [],
  totalsData: null,
  overridesData: null,
  issuesData: null,
  totalsExpanded: true,
  overridesExpandedGroups: new Set(),
}

function initStateFromParams(sp: URLSearchParams): ReportsState {
  const reportSlug = sp.get('report') ?? ''
  return {
    ...INITIAL_STATE,
    activeTab: URL_KEY_TO_TAB[reportSlug] ?? 'totals',
    teams: new Set(sp.getAll('team')),
    tiers: new Set(sp.getAll('tier')),
    versions: new Set(sp.getAll('version')),
    dateFrom: sp.get('from') ?? '',
    dateTo: sp.get('to') ?? '',
    statuses: new Set(sp.getAll('status')),
    labels: sp.getAll('label'),
    excludeLabels: sp.getAll('exclude_label'),
    search: sp.get('search') ?? '',
  }
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const v of a) if (!b.has(v)) return false
  return true
}

function toggleInSet(set: Set<string>, value: string): Set<string> {
  const next = new Set(set)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return next
}

function reportsReducer(state: ReportsState, action: ReportsAction): ReportsState {
  switch (action.type) {
    case 'SET_TAB':
      return { ...state, activeTab: action.tab }
    case 'SET_SEARCH':
      return { ...state, search: action.value }
    case 'SYNC_FROM_URL':
      return {
        ...initStateFromParams(action.params),
        totalsExpanded: state.totalsExpanded,
        overridesExpandedGroups: state.overridesExpandedGroups,
        totalsData: state.totalsData,
        overridesData: state.overridesData,
        issuesData: state.issuesData,
      }
    case 'FETCH_START':
      return { ...state, loading: true, error: null }
    case 'FETCH_ERROR':
      return { ...state, loading: false, error: action.error }
    case 'FETCH_TOTALS':
      return { ...state, loading: false, totalsData: action.data }
    case 'FETCH_OVERRIDES':
      return { ...state, loading: false, overridesData: action.data }
    case 'FETCH_ISSUES':
      return { ...state, loading: false, issuesData: action.data }
    case 'TOGGLE_META':
      return { ...state, [action.field]: toggleInSet(state[action.field], action.value) }
    case 'CLEAR_META':
      return { ...state, [action.field]: new Set<string>() }
    case 'CLEAR_ALL_META':
      return { ...state, teams: new Set(), tiers: new Set(), versions: new Set(), statuses: new Set<string>(), labels: [], excludeLabels: [] }
    case 'SET_DATE_RANGE':
      return { ...state, dateFrom: action.from, dateTo: action.to }
    case 'TOGGLE_STATUS':
      return { ...state, statuses: toggleInSet(state.statuses, action.value) }
    case 'CLEAR_STATUSES':
      return { ...state, statuses: new Set<string>() }
    case 'TOGGLE_LABEL': {
      const curLabels = state.labels.filter(l => l !== action.label)
      const curExclude = state.excludeLabels.filter(l => l !== action.label)
      return {
        ...state,
        labels: action.action === 'include' ? [...curLabels, action.label] : curLabels,
        excludeLabels: action.action === 'exclude' ? [...curExclude, action.label] : curExclude,
      }
    }
    case 'TOGGLE_TOTALS_EXPANDED':
      return { ...state, totalsExpanded: !state.totalsExpanded }
    case 'SET_TOTALS_EXPANDED':
      return { ...state, totalsExpanded: action.value }
    case 'TOGGLE_OVERRIDE_GROUP': {
      const next = new Set(state.overridesExpandedGroups)
      if (next.has(action.key)) next.delete(action.key)
      else next.add(action.key)
      return { ...state, overridesExpandedGroups: next }
    }
    case 'EXPAND_ALL_OVERRIDES':
      return { ...state, overridesExpandedGroups: new Set(action.keys) }
    case 'COLLAPSE_ALL_OVERRIDES':
      return { ...state, overridesExpandedGroups: new Set() }
  }
}

// ─── Report Content Components ──────────────────────────────────────

interface TotalsReportProps {
  data: TotalsData
  search: string
  expanded: boolean
  onToggleExpanded: () => void
  onSetExpanded: (v: boolean) => void
}

function TotalsReport({ data, search, expanded, onToggleExpanded, onSetExpanded }: TotalsReportProps) {
  const [page, setPage] = useReducer((_: number, p: number) => p, 1)
  const q = search.toLowerCase()
  const filteredJobs = useMemo(() => q
    ? data.jobs.filter(j => j.job_name.toLowerCase().includes(q))
    : data.jobs,
  [data.jobs, q])
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / PAGE_SIZE))
  const safePage = Math.min(page, totalPages)
  const pageJobs = filteredJobs.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Total Jobs" value={data.total_jobs} />
        <StatCard label="Total Failures" value={data.total_failures} tone="text-signal-red" />
        <StatCard label="Total Reviewed" value={data.total_reviewed} tone="text-signal-green" />
      </div>

      {data.jobs.length > 0 && (
        <Collapsible open={expanded} onOpenChange={onToggleExpanded}>
          <div className="flex items-center gap-3">
            <CollapsibleTrigger asChild>
              <button
                type="button"
                className="flex items-center gap-1.5 text-sm font-medium text-text-secondary hover:text-text-primary transition-colors"
              >
                {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                Job Details ({filteredJobs.length})
              </button>
            </CollapsibleTrigger>
            <ExpandCollapseButtons
              onExpandAll={() => onSetExpanded(true)}
              onCollapseAll={() => onSetExpanded(false)}
            />
          </div>

          <CollapsibleContent>
            <Table className="mt-2">
              <TableHeader>
                <TableRow className="bg-surface-card hover:bg-surface-card">
                  <TableHead>Job</TableHead>
                  <TableHead className="text-center">Failures</TableHead>
                  <TableHead className="text-center">Reviewed</TableHead>
                  <TableHead className="text-right">Date</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pageJobs.map((job, i) => (
                  <TableRow key={job.job_id} className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}>
                    <TableCell>
                      <Link to={`/results/${job.job_id}`} className="text-sm text-text-link hover:underline">
                        {job.job_name}
                      </Link>
                      {job.build_number != null && (
                        <span className="ml-1 font-mono text-xs text-text-tertiary">#{job.build_number}</span>
                      )}
                    </TableCell>
                    <TableCell className="text-center font-mono text-xs">{job.failure_count}</TableCell>
                    <TableCell className="text-center font-mono text-xs">{job.reviewed_count}</TableCell>
                    <TableCell className="text-right font-mono text-xs text-text-tertiary">
                      {parseApiTimestamp(job.created_at).toLocaleDateString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}

interface OverridesReportProps {
  data: OverridesData
  search: string
  expandedGroups: Set<string>
  onToggleGroup: (key: string) => void
  onExpandAll: (keys: string[]) => void
  onCollapseAll: () => void
}

function OverridesReport({ data, search, expandedGroups, onToggleGroup, onExpandAll, onCollapseAll }: OverridesReportProps) {
  const [pageByGroup, setPageByGroup] = useState<Record<string, number>>({})

  const q = search.toLowerCase()
  const filteredDetails = useMemo(() => q
    ? data.details.filter(d => d.test_name.toLowerCase().includes(q) || d.job_name.toLowerCase().includes(q))
    : data.details,
  [data.details, q])

  const detailsByGroup = useMemo(() => {
    const map: Record<string, typeof data.details> = {}
    for (const d of filteredDetails) {
      const key = `${d.from_classification} → ${d.to_classification}`
      ;(map[key] ??= []).push(d)
    }
    return map
  }, [filteredDetails])

  /** When searching, derive groups from filtered details so counts match. */
  const filteredGroups = useMemo(() => {
    if (!q) return data.groups
    const counts: Record<string, { from: string; to: string; count: number }> = {}
    for (const d of filteredDetails) {
      const key = `${d.from_classification} → ${d.to_classification}`
      if (!counts[key]) counts[key] = { from: d.from_classification, to: d.to_classification, count: 0 }
      counts[key].count++
    }
    return Object.values(counts)
  }, [data.groups, filteredDetails, q])

  const allGroupKeys = useMemo(() => filteredGroups.map(g => `${g.from} → ${g.to}`), [filteredGroups])

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4">
        <StatCard label="Total Overrides" value={data.total} />
      </div>

      {filteredGroups.length > 0 && (
        <div className="space-y-2">
          <ExpandCollapseButtons
            onExpandAll={() => onExpandAll(allGroupKeys)}
            onCollapseAll={onCollapseAll}
          />
          {filteredGroups.map((g) => {
            const key = `${g.from} → ${g.to}`
            const isExpanded = expandedGroups.has(key)
            const groupDetails = isExpanded ? (detailsByGroup[key] ?? []) : []
            const groupTotalPages = Math.max(1, Math.ceil(groupDetails.length / PAGE_SIZE))
            const groupPage = Math.min(pageByGroup[key] ?? 1, groupTotalPages)
            const groupPageDetails = groupDetails.slice((groupPage - 1) * PAGE_SIZE, groupPage * PAGE_SIZE)
            return (
              <Collapsible key={key} open={isExpanded} onOpenChange={() => onToggleGroup(key)}>
                <div className="rounded-lg border border-border-muted bg-surface-card overflow-hidden">
                  <CollapsibleTrigger asChild>
                    <button
                      type="button"
                      className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
                    >
                      {isExpanded ? <ChevronDown className="h-4 w-4 text-text-tertiary" /> : <ChevronRight className="h-4 w-4 text-text-tertiary" />}
                      <ClassificationBadge classification={g.from} />
                      <span className="text-text-tertiary">→</span>
                      <ClassificationBadge classification={g.to} />
                      <span className="ml-auto font-mono text-xs text-text-tertiary">{g.count}</span>
                    </button>
                  </CollapsibleTrigger>

                  <CollapsibleContent>
                    <div className="border-t border-border-muted">
                      <Table>
                        <TableHeader>
                          <TableRow className="bg-surface-elevated/40 hover:bg-surface-elevated/40">
                            <TableHead>Test</TableHead>
                            <TableHead>Job</TableHead>
                            <TableHead>By</TableHead>
                            <TableHead className="text-right">Date</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {groupPageDetails.map((d, i) => (
                            <TableRow key={`${d.job_id}-${d.test_name}-${i}`} className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}>
                              <TableCell className="font-mono text-xs max-w-[300px] truncate">{d.test_name}</TableCell>
                              <TableCell>
                                <Link to={`/results/${d.job_id}?highlight=${encodeURIComponent(d.test_name)}`} className="text-xs text-text-link hover:underline">
                                  {d.job_name}
                                  {d.build_number != null && (
                                    <span className="ml-1 font-mono text-[10px]">#{d.build_number}</span>
                                  )}
                                </Link>
                              </TableCell>
                              <TableCell className="text-xs text-text-secondary">{d.overridden_by}</TableCell>
                              <TableCell className="text-right font-mono text-xs text-text-tertiary">
                                {parseApiTimestamp(d.overridden_at).toLocaleDateString()}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                      <Pagination page={groupPage} totalPages={groupTotalPages} onPageChange={(p) => setPageByGroup(prev => ({ ...prev, [key]: p }))} />
                    </div>
                  </CollapsibleContent>
                </div>
              </Collapsible>
            )
          })}
        </div>
      )}

      {filteredGroups.length === 0 && (
        <p className="text-sm text-text-tertiary py-8 text-center">No classification overrides found.</p>
      )}
    </div>
  )
}

function IssuesReport({ data, search }: { data: IssuesData; search: string }) {
  const [page, setPage] = useReducer((_: number, p: number) => p, 1)
  const q = search.toLowerCase()
  const filteredIssues = useMemo(() => q
    ? data.issues.filter(i => i.test_name.toLowerCase().includes(q) || i.job_name.toLowerCase().includes(q) || i.title.toLowerCase().includes(q))
    : data.issues,
  [data.issues, q])
  const totalPages = Math.max(1, Math.ceil(filteredIssues.length / PAGE_SIZE))
  const safePage = Math.min(page, totalPages)
  const pageIssues = filteredIssues.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Total Issues" value={data.total} />
        <StatCard label="GitHub Issues" value={data.github_total} tone="text-signal-blue" />
        <StatCard label="Jira Tickets" value={data.jira_total} tone="text-signal-orange" />
      </div>

      {data.issues.length > 0 ? (
        <>
          <Table>
            <TableHeader>
              <TableRow className="bg-surface-card hover:bg-surface-card">
                <TableHead>Type</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Test</TableHead>
                <TableHead>Job</TableHead>
                <TableHead>By</TableHead>
                <TableHead className="text-right">Date</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageIssues.map((issue, i) => (
                <TableRow key={`${issue.job_id}-${issue.test_name}-${i}`} className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}>
                  <TableCell>
                    <span className={cn(
                      'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium',
                      issue.issue_type === 'GitHub Issue'
                        ? 'bg-signal-blue/10 text-signal-blue'
                        : 'bg-signal-orange/10 text-signal-orange',
                    )}>
                      {issue.issue_type}
                    </span>
                  </TableCell>
                  <TableCell>
                    <a
                      href={issue.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-text-link hover:underline inline-flex items-center gap-1"
                    >
                      {issue.title}
                      <ExternalLink className="h-3 w-3 shrink-0" />
                    </a>
                  </TableCell>
                  <TableCell className="font-mono text-xs max-w-[200px] truncate">{issue.test_name}</TableCell>
                  <TableCell>
                    <Link to={`/results/${issue.job_id}`} className="text-xs text-text-link hover:underline">
                      {issue.job_name}
                    </Link>
                    {issue.build_number != null && (
                      <span className="ml-1 font-mono text-[10px] text-text-tertiary">#{issue.build_number}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-text-secondary">{issue.created_by}</TableCell>
                  <TableCell className="text-right font-mono text-xs text-text-tertiary">
                    {parseApiTimestamp(issue.created_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : (
        <p className="text-sm text-text-tertiary py-8 text-center">No issues found.</p>
      )}
    </div>
  )
}

function StatCard({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border border-border-muted bg-surface-card p-4 text-center">
      <p className="text-sm font-medium text-text-secondary">{label}</p>
      <p className={cn('mt-1 text-2xl font-bold font-mono', tone || 'text-text-primary')}>{value}</p>
    </div>
  )
}

// ─── Main Page ──────────────────────────────────────────────────────

export function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [state, dispatch] = useReducer(reportsReducer, searchParams, initStateFromParams)
  const { activeTab, loading, error, search, teams, tiers, versions, dateFrom, dateTo, statuses, labels, excludeLabels, totalsData, overridesData, issuesData, totalsExpanded, overridesExpandedGroups } = state

  // ─── URL → state sync (external navigation / shared links) ─────
  // Only dispatch when URL actually differs from current state — no flags, no races.
  useEffect(() => {
    const nextState = initStateFromParams(searchParams)

    const differs = nextState.activeTab !== activeTab
      || nextState.dateFrom !== dateFrom || nextState.dateTo !== dateTo
      || !setsEqual(nextState.teams, teams) || !setsEqual(nextState.tiers, tiers)
      || !setsEqual(nextState.versions, versions) || !setsEqual(nextState.statuses, statuses)
      || JSON.stringify(nextState.labels) !== JSON.stringify(labels)
      || JSON.stringify(nextState.excludeLabels) !== JSON.stringify(excludeLabels)
      || nextState.search !== search

    if (differs) {
      dispatch({ type: 'SYNC_FROM_URL', params: searchParams })
    }
  }, [searchParams]) // eslint-disable-line react-hooks/exhaustive-deps -- compare against state inside

  // ─── State → URL sync ──────────────────────────────────────────
  useEffect(() => {
    const params = new URLSearchParams()
    if (activeTab !== 'totals') params.set('report', TAB_URL_KEY[activeTab])
    for (const t of teams) params.append('team', t)
    for (const t of tiers) params.append('tier', t)
    for (const v of versions) params.append('version', v)
    if (dateFrom) params.set('from', dateFrom)
    if (dateTo) params.set('to', dateTo)
    for (const s of statuses) params.append('status', s)
    for (const l of labels) params.append('label', l)
    for (const l of excludeLabels) params.append('exclude_label', l)
    if (search) params.set('search', search)
    setSearchParams(params, { replace: true })
  }, [activeTab, teams, tiers, versions, dateFrom, dateTo, statuses, labels, excludeLabels, search, setSearchParams])

  const { options: metadataOptions } = useMetadataOptions()
  const fetchSeqRef = useRef(0)

  const hasMetadataFilters = teams.size > 0 || tiers.size > 0 || versions.size > 0 || statuses.size > 0 || labels.length > 0 || excludeLabels.length > 0

  // Build query params shared by all endpoints
  const queryString = useMemo(() => {
    const params = new URLSearchParams()
    const teamVal = [...teams].join(',')
    const tierVal = [...tiers].join(',')
    const versionVal = [...versions].join(',')
    if (teamVal) params.set('team', teamVal)
    if (tierVal) params.set('tier', tierVal)
    if (versionVal) params.set('version', versionVal)
    if (dateFrom) params.set('from', dateFrom)
    if (dateTo) params.set('to', dateTo)
    const statusVal = [...statuses].join(',')
    if (statusVal) params.set('status', statusVal)
    const tagsVal = labels.join(',')
    if (tagsVal) params.set('tags', tagsVal)
    const excludeVal = excludeLabels.join(',')
    if (excludeVal) params.set('exclude_tags', excludeVal)
    return params.toString()
  }, [teams, tiers, versions, dateFrom, dateTo, statuses, labels, excludeLabels])

  const fetchReport = useCallback(async () => {
    const seq = ++fetchSeqRef.current
    dispatch({ type: 'FETCH_START' })
    const suffix = queryString ? `?${queryString}` : ''

    try {
      switch (activeTab) {
        case 'totals': {
          const data = await api.get<TotalsData>(`/api/reports/totals${suffix}`)
          if (seq === fetchSeqRef.current) dispatch({ type: 'FETCH_TOTALS', data })
          break
        }
        case 'overrides': {
          const data = await api.get<OverridesData>(`/api/reports/classification-overrides${suffix}`)
          if (seq === fetchSeqRef.current) dispatch({ type: 'FETCH_OVERRIDES', data })
          break
        }
        case 'issues': {
          const data = await api.get<IssuesData>(`/api/reports/issues-created${suffix}`)
          if (seq === fetchSeqRef.current) dispatch({ type: 'FETCH_ISSUES', data })
          break
        }
      }
    } catch (err) {
      if (seq === fetchSeqRef.current) {
        dispatch({ type: 'FETCH_ERROR', error: err instanceof Error ? err.message : 'Failed to load report' })
      }
    }
  }, [activeTab, queryString])

  useEffect(() => {
    fetchReport()
  }, [fetchReport])

  return (
    <TooltipProvider delayDuration={200}>
    <div className="flex gap-4">
      {/* Sidebar */}
      <nav className="w-56 shrink-0 space-y-1">
        {TABS.map(({ key, label, icon: Icon }) => (
          <Button
            key={key}
            variant="ghost"
            onClick={() => dispatch({ type: 'SET_TAB', tab: key })}
            className={cn(
              'w-full justify-start gap-2 text-sm',
              activeTab === key
                ? 'bg-surface-elevated text-text-primary'
                : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
            )}
          >
            <Icon className="h-4 w-4 shrink-0" />
            <span className="truncate">{label}</span>
          </Button>
        ))}
      </nav>

      {/* Main content: filters + report */}
      <div className="flex-1 min-w-0 space-y-6">
        {/* Filters — matching Dashboard order: Search → Metadata → Status → DateRange → Tags */}
        <div className="flex flex-wrap gap-3 items-center">
          <SearchInput value={search} onChange={(v) => dispatch({ type: 'SET_SEARCH', value: v })} placeholder="Filter reports..." className="w-full sm:w-64" />
          <MetadataDropdowns
            options={metadataOptions}
            teams={teams}
            tiers={tiers}
            versions={versions}
            onTeamToggle={(v) => dispatch({ type: 'TOGGLE_META', field: 'teams', value: v })}
            onTierToggle={(v) => dispatch({ type: 'TOGGLE_META', field: 'tiers', value: v })}
            onVersionToggle={(v) => dispatch({ type: 'TOGGLE_META', field: 'versions', value: v })}
            onTeamClear={() => dispatch({ type: 'CLEAR_META', field: 'teams' })}
            onTierClear={() => dispatch({ type: 'CLEAR_META', field: 'tiers' })}
            onVersionClear={() => dispatch({ type: 'CLEAR_META', field: 'versions' })}
          />
          <MultiSelectFilter
            label="All statuses"
            options={[...STATUS_FILTER_OPTIONS]}
            selected={statuses}
            onToggle={(v) => dispatch({ type: 'TOGGLE_STATUS', value: v })}
            onClear={() => dispatch({ type: 'CLEAR_STATUSES' })}
            className="w-full sm:w-40"
          />
          <DateRangePresetFilter
            from={dateFrom}
            to={dateTo}
            onChange={(from, to) => dispatch({ type: 'SET_DATE_RANGE', from, to })}
          />
          <MetadataClearButton hasFilters={hasMetadataFilters} onClearAll={() => dispatch({ type: 'CLEAR_ALL_META' })} />
        </div>

        {/* Tag chips — same pattern as Dashboard */}
        <MetadataLabelChips
          allLabels={metadataOptions.allLabels}
          labels={labels}
          excludeLabels={excludeLabels}
          onLabelToggle={(label, action) => dispatch({ type: 'TOGGLE_LABEL', label, action })}
        />

        {/* Report content */}
        {error && (
          <p className="text-center text-signal-red py-8">{error}</p>
        )}

        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : (
          <>
            {activeTab === 'totals' && totalsData && (
              <TotalsReport
                data={totalsData}
                search={search}
                expanded={totalsExpanded}
                onToggleExpanded={() => dispatch({ type: 'TOGGLE_TOTALS_EXPANDED' })}
                onSetExpanded={(v) => dispatch({ type: 'SET_TOTALS_EXPANDED', value: v })}
              />
            )}
            {activeTab === 'overrides' && overridesData && (
              <OverridesReport
                data={overridesData}
                search={search}
                expandedGroups={overridesExpandedGroups}
                onToggleGroup={(key) => dispatch({ type: 'TOGGLE_OVERRIDE_GROUP', key })}
                onExpandAll={(keys) => dispatch({ type: 'EXPAND_ALL_OVERRIDES', keys })}
                onCollapseAll={() => dispatch({ type: 'COLLAPSE_ALL_OVERRIDES' })}
              />
            )}
            {activeTab === 'issues' && issuesData && <IssuesReport data={issuesData} search={search} />}
          </>
        )}
      </div>
    </div>
    </TooltipProvider>
  )
}
