import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '@/lib/api'
import { cn, parseApiTimestamp, resolveBuildDisplayId } from '@/lib/utils'

/** Render build ID badge using the shared resolver (handles Prow build_id fallback). */
function BuildBadge({ buildNumber, buildId, className }: { buildNumber?: number; buildId?: string; className?: string }) {
  const bid = resolveBuildDisplayId({ build_number: buildNumber, build_id: buildId })
  if (!bid) return null
  return <span className={cn('ml-1 font-mono', className)}>#{bid}</span>
}
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from '@/components/ui/collapsible'
import { TooltipProvider } from '@/components/ui/tooltip'
import { MetadataLabelChips } from '@/components/shared/MetadataFilterBar'
import { JobFilterBar } from '@/components/shared/JobFilterBar'
import { useMetadataOptions } from '@/components/shared/useMetadataOptions'
import { useSubmitters } from '@/components/shared/useSubmitters'
import { ClassificationBadge } from '@/components/shared/ClassificationBadge'
import { Pagination } from '@/components/shared/Pagination'
import { ExpandCollapseButtons } from '@/components/shared/ExpandCollapseButtons'
import { parseReviewStatus, type ReviewStatusFilter as ReviewStatusValue } from '@/lib/review-status'
import { parseAnalysisState, type AnalysisStateFilter } from '@/lib/analysis-state'
import { FILTER_PRESET_KEYS } from '@/lib/filterPresets'
import {
  applyFiltersToParams,
  EMPTY_JOB_FILTERS,
  jobFiltersFromFilters,
  jobFiltersFromParams,
  jobFiltersToApiQuery,
  jobFiltersToFilters,
  normalizeSubmitters,
  type JobFilterValues,
  type TestNameMode,
} from '@/lib/jobFilters'
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
    build_id?: string
    failure_count: number
    reviewed_count: number
    created_at: string
  }>
}

interface OverridesData {
  total: number
  total_reviewed: number
  ai_correct: number
  ai_accuracy_pct: number
  groups: Array<{ from: string; to: string; count: number }>
  details: Array<{
    test_name: string
    job_name: string
    job_id: string
    build_number: number
    build_id?: string
    from_classification: string
    to_classification: string
    override_axis?: 'classification' | 'pattern'
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
    build_id?: string
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
  reviewStatus: ReviewStatusValue
  analysisState: AnalysisStateFilter
  testName: string
  testNameMode: TestNameMode
  submitters: string[]
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
  | { type: 'SET_REVIEW_STATUS'; value: ReviewStatusValue }
  | { type: 'SET_ANALYSIS_STATE'; value: AnalysisStateFilter }
  | { type: 'SET_SUBMITTERS'; values: string[] }
  | { type: 'SET_TEST_NAME'; name: string; mode: TestNameMode }
  | { type: 'APPLY_FILTERS'; filters: Record<string, string | string[]> }
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
  reviewStatus: 'all' as ReviewStatusValue,
  analysisState: 'all' as AnalysisStateFilter,
  testName: '',
  testNameMode: 'contains' as TestNameMode,
  submitters: [],
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
  // One reader for the shared contract: canonical `date_from`/`date_to` with the
  // legacy `from`/`to` aliases still accepted.
  const jobFilters = jobFiltersFromParams(sp, FILTER_PRESET_KEYS.reports)
  return {
    ...INITIAL_STATE,
    activeTab: URL_KEY_TO_TAB[reportSlug] ?? 'totals',
    teams: new Set(sp.getAll('team')),
    tiers: new Set(sp.getAll('tier')),
    versions: new Set(sp.getAll('version')),
    dateFrom: jobFilters.date_from,
    dateTo: jobFilters.date_to,
    statuses: new Set(sp.getAll('status')),
    reviewStatus: parseReviewStatus(sp.get('review_status')),
    analysisState: parseAnalysisState(sp.get('analysis_state')),
    testName: jobFilters.test_name,
    testNameMode: jobFilters.test_name_mode,
    submitters: jobFilters.submitted_by,
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
      return { ...state, teams: new Set(), tiers: new Set(), versions: new Set(), statuses: new Set<string>(), reviewStatus: 'all' as ReviewStatusValue, analysisState: 'all' as AnalysisStateFilter, testName: '', testNameMode: 'contains' as TestNameMode, submitters: [], labels: [], excludeLabels: [] }
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
    case 'SET_REVIEW_STATUS':
      return { ...state, reviewStatus: action.value }
    case 'SET_ANALYSIS_STATE':
      return { ...state, analysisState: action.value }
    case 'SET_SUBMITTERS':
      return { ...state, submitters: action.values }
    case 'SET_TEST_NAME':
      return { ...state, testName: action.name, testNameMode: action.mode }
    case 'APPLY_FILTERS': {
      // Apply a saved filter preset (#179): replace every filter field,
      // keeping tab, search, and already-fetched report data intact.
      const f = jobFiltersFromFilters(action.filters)
      return {
        ...state,
        teams: new Set(f.team),
        tiers: new Set(f.tier),
        versions: new Set(f.version),
        dateFrom: f.date_from,
        dateTo: f.date_to,
        statuses: new Set(f.status),
        reviewStatus: parseReviewStatus(f.review_status),
        analysisState: parseAnalysisState(f.analysis_state),
        testName: f.test_name,
        testNameMode: f.test_name_mode,
        submitters: f.submitted_by,
        labels: f.label,
        excludeLabels: f.exclude_label,
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
                      <BuildBadge buildNumber={job.build_number} buildId={job.build_id} className="text-xs text-text-tertiary" />
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
  const { details, groups } = data
  const allGroups = groups
  const filteredDetails = useMemo(() => q
    ? details.filter(d => d.test_name.toLowerCase().includes(q) || d.job_name.toLowerCase().includes(q))
    : details,
  [details, q])

  const detailsByGroup = useMemo(() => {
    const map: Record<string, typeof details> = {}
    for (const d of filteredDetails) {
      const key = `${d.from_classification} → ${d.to_classification}`
      ;(map[key] ??= []).push(d)
    }
    return map
  }, [filteredDetails])


  /** When searching, derive groups from filtered details so counts match. */
  const filteredGroups = useMemo(() => {
    if (!q) return allGroups
    const counts: Record<string, { from: string; to: string; count: number }> = {}
    for (const d of filteredDetails) {
      const key = `${d.from_classification} → ${d.to_classification}`
      if (!counts[key]) counts[key] = { from: d.from_classification, to: d.to_classification, count: 0 }
      counts[key].count++
    }
    return Object.values(counts)
  }, [allGroups, filteredDetails, q])

  const allGroupKeys = useMemo(() => filteredGroups.map(g => `${g.from} → ${g.to}`), [filteredGroups])

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-4 gap-4">
        <StatCard label="Total Reviewed" value={data.total_reviewed} />
        <StatCard label="AI Correct" value={data.ai_correct} tone="text-signal-green" />
        <StatCard label="Total Overrides" value={data.total} tone="text-signal-orange" />
        <StatCard
          label="AI Accuracy"
          value={data.ai_accuracy_pct}
          suffix="%"
          tone={data.ai_accuracy_pct >= 80 ? 'text-signal-green' : data.ai_accuracy_pct >= 60 ? 'text-signal-orange' : 'text-signal-red'}
        />
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
                            <TableHead>Axis</TableHead>
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
                                <span className={cn(
                                  'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium',
                                  d.override_axis === 'pattern'
                                    ? 'bg-purple-500/10 text-purple-400'
                                    : 'bg-signal-blue/10 text-signal-blue',
                                )}>
                                  {d.override_axis === 'pattern' ? 'Pattern' : 'Root Cause'}
                                </span>
                              </TableCell>
                              <TableCell>
                                <Link to={`/results/${d.job_id}?highlight=${encodeURIComponent(d.test_name)}`} className="text-xs text-text-link hover:underline">
                                  {d.job_name}
                                  <BuildBadge buildNumber={d.build_number} buildId={d.build_id} className="text-[10px]" />
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
                    <BuildBadge buildNumber={issue.build_number} buildId={issue.build_id} className="text-[10px] text-text-tertiary" />
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

function StatCard({ label, value, suffix, tone }: { label: string; value: number; suffix?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-border-muted bg-surface-card p-4 text-center">
      <p className="text-sm font-medium text-text-secondary">{label}</p>
      <p className={cn('mt-1 text-2xl font-bold font-mono', tone || 'text-text-primary')}>{value}{suffix}</p>
    </div>
  )
}

// ─── Main Page ──────────────────────────────────────────────────────

export function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [state, dispatch] = useReducer(reportsReducer, searchParams, initStateFromParams)
  const { activeTab, loading, error, search, teams, tiers, versions, dateFrom, dateTo, statuses, reviewStatus, analysisState, testName, testNameMode, submitters, labels, excludeLabels, totalsData, overridesData, issuesData, totalsExpanded, overridesExpandedGroups } = state

  const { options: metadataOptions } = useMetadataOptions()
  const { submitters: submitterOptions, loading: submittersLoading } = useSubmitters()

  // The URL submitters are read before the spellings arrive, so they are folded
  // onto those spellings (and onto the endpoint's bounds) once here: `ALICE` in a
  // link is the `alice` chip, not a second pick of the same selection, and a value
  // the endpoint would reject never reaches it.
  const normalizedSubmitters = useMemo(
    () => normalizeSubmitters(submitters, submitterOptions),
    [submitters, submitterOptions],
  )

  // One typed view of the filters, so the URL writer and the API query cannot
  // drift apart on a key name or a shape (#546).
  const jobFilterValues = useMemo<JobFilterValues>(() => ({
    ...EMPTY_JOB_FILTERS,
    team: [...teams],
    tier: [...tiers],
    version: [...versions],
    label: labels,
    exclude_label: excludeLabels,
    status: [...statuses],
    review_status: reviewStatus,
    analysis_state: analysisState,
    submitted_by: normalizedSubmitters,
    date_from: dateFrom,
    date_to: dateTo,
    test_name: testName,
    test_name_mode: testNameMode,
  }), [teams, tiers, versions, labels, excludeLabels, statuses, reviewStatus, analysisState, normalizedSubmitters, dateFrom, dateTo, testName, testNameMode])

  // ─── URL → state sync (external navigation / shared links) ─────
  // Only dispatch when URL actually differs from current state — no flags, no races.
  useEffect(() => {
    const nextState = initStateFromParams(searchParams)

    const differs = nextState.activeTab !== activeTab
      || nextState.dateFrom !== dateFrom || nextState.dateTo !== dateTo
      || !setsEqual(nextState.teams, teams) || !setsEqual(nextState.tiers, tiers)
      || !setsEqual(nextState.versions, versions) || !setsEqual(nextState.statuses, statuses)
      || nextState.reviewStatus !== reviewStatus
      || nextState.analysisState !== analysisState
      || nextState.testName !== testName || nextState.testNameMode !== testNameMode
      || JSON.stringify(nextState.submitters) !== JSON.stringify(submitters)
      || JSON.stringify(nextState.labels) !== JSON.stringify(labels)
      || JSON.stringify(nextState.excludeLabels) !== JSON.stringify(excludeLabels)
      || nextState.search !== search

    if (differs) {
      dispatch({ type: 'SYNC_FROM_URL', params: searchParams })
    }
  }, [searchParams]) // eslint-disable-line react-hooks/exhaustive-deps -- compare against state inside

  // ─── State → URL sync ──────────────────────────────────────────
  // Write only when the serialized state differs from the current URL —
  // together with the guarded URL → state effect above this makes an
  // alternation between the two effects impossible: every write strictly
  // reduces the difference to zero (sidebar preset clicks navigate to a
  // same-route query string without remounting this page).
  useEffect(() => {
    const params = new URLSearchParams()
    if (activeTab !== 'totals') params.set('report', TAB_URL_KEY[activeTab])
    // `report` and `search` are not filter dimensions; every filter key goes in
    // through the shared writer, which drops `all`/empty and writes the
    // canonical `date_from`/`date_to` pair instead of the legacy aliases.
    const next = applyFiltersToParams(
      params,
      jobFiltersToFilters(jobFilterValues),
      FILTER_PRESET_KEYS.reports,
    )
    if (search) next.set('search', search)
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true })
    }
  }, [activeTab, jobFilterValues, search]) // eslint-disable-line react-hooks/exhaustive-deps -- searchParams/setSearchParams are read from closure on purpose: react-router gives them a fresh identity on every location change, so listing them re-runs this effect on external navigations and clobbers them

  const fetchSeqRef = useRef(0)

  const hasMetadataFilters = teams.size > 0 || tiers.size > 0 || versions.size > 0 || statuses.size > 0 || reviewStatus !== 'all' || analysisState !== 'all' || submitters.length > 0 || labels.length > 0 || excludeLabels.length > 0 || testName !== ''

  // Build query params shared by all endpoints
  const queryString = useMemo(
    () => jobFiltersToApiQuery('reports', jobFilterValues).toString(),
    [jobFilterValues],
  )

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
        {/* Filters — the same bar the dashboard mounts (#546 A) */}
        <JobFilterBar
          page="reports"
          searchParams={searchParams}
          onApplyPreset={(filters) => dispatch({ type: 'APPLY_FILTERS', filters })}
          search={{
            value: search,
            onChange: (v) => dispatch({ type: 'SET_SEARCH', value: v }),
            placeholder: 'Filter reports...',
          }}
          submitters={{
            values: normalizedSubmitters,
            options: submitterOptions,
            loading: submittersLoading,
            onChange: (values) => dispatch({ type: 'SET_SUBMITTERS', values }),
          }}
          metadata={{
            options: metadataOptions,
            teams,
            tiers,
            versions,
            onTeamToggle: (v) => dispatch({ type: 'TOGGLE_META', field: 'teams', value: v }),
            onTierToggle: (v) => dispatch({ type: 'TOGGLE_META', field: 'tiers', value: v }),
            onVersionToggle: (v) => dispatch({ type: 'TOGGLE_META', field: 'versions', value: v }),
            onTeamClear: () => dispatch({ type: 'CLEAR_META', field: 'teams' }),
            onTierClear: () => dispatch({ type: 'CLEAR_META', field: 'tiers' }),
            onVersionClear: () => dispatch({ type: 'CLEAR_META', field: 'versions' }),
          }}
          statuses={{
            selected: statuses,
            onToggle: (v) => dispatch({ type: 'TOGGLE_STATUS', value: v }),
            onClear: () => dispatch({ type: 'CLEAR_STATUSES' }),
          }}
          reviewStatus={{
            value: reviewStatus,
            onChange: (v) => dispatch({ type: 'SET_REVIEW_STATUS', value: v }),
          }}
          analysisState={{
            value: analysisState,
            onChange: (v) => dispatch({ type: 'SET_ANALYSIS_STATE', value: v }),
          }}
          dateRange={{
            from: dateFrom,
            to: dateTo,
            onChange: (from, to) => dispatch({ type: 'SET_DATE_RANGE', from, to }),
          }}
          clearAll={{
            hasFilters: hasMetadataFilters,
            onClear: () => dispatch({ type: 'CLEAR_ALL_META' }),
          }}
          testName={{
            name: testName,
            mode: testNameMode,
            onChange: (name, mode) => dispatch({ type: 'SET_TEST_NAME', name, mode }),
          }}
        />

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
