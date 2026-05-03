import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { api } from '@/lib/api'
import { useClipboard } from '@/lib/useClipboard'
import { parseApiTimestamp, isAnalysisTimeout, formatDuration, formatTimestamp } from '@/lib/utils'
import { buildRepoUrls, type RepoUrl } from '@/lib/autoLink'
import { groupFailures } from '@/lib/grouping'
import { useExpandCollapseAll } from '@/lib/useExpandCollapseAll'
import type { ResultResponse, CommentsAndReviews, AiConfig } from '@/types'
import { ReportProvider, useReportState, useReportDispatch, useRefreshEnrichments } from './report/ReportContext'
import { FailureCard } from './report/FailureCard'
import { ChildJobSection } from './report/ChildJobSection'
import { PeerAnalysisSummary } from './report/PeerAnalysisSummary'
import { AllReviewedPrompt } from './report/AllReviewedPrompt'
import { collectChildExpandKeys } from '@/lib/childJobHash'
import { collectAllTestKeys, countAllFailures } from '@/lib/failureKeys'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Skeleton } from '@/components/ui/skeleton'
import { StatusChip } from '@/components/shared/StatusChip'
import { ExpandCollapseButtons } from '@/components/shared/ExpandCollapseButtons'
import { Button } from '@/components/ui/button'
import { ExternalLink, CheckCircle2, Clock, Calendar, Cpu, Timer, FolderGit2, RotateCw, Copy, Check } from 'lucide-react'
import { ReAnalyzeDialog } from './report/ReAnalyzeDialog'
import { ReportPortalButton } from './report/ReportPortalButton'
import { TokenUsageBadge } from './report/TokenUsageBadge'
import { reviewKey } from './report/ReportContext'
import type { ChildJobAnalysis } from '@/types'

/** Interval in milliseconds between comment poll requests.
 *  Override at build time via VITE_COMMENT_POLL_MS (e.g. 60000 for 1 minute).
 *  Clamped to [5 000, 300 000] ms to avoid accidental runaway polling. */
const COMMENT_POLL_MS = Math.max(5_000, Math.min(300_000,
  Number(import.meta.env.VITE_COMMENT_POLL_MS) || 30_000,
))

export function ReportPage() {
  const { jobId } = useParams<{ jobId: string }>()
  return (
    <ReportProvider key={jobId ?? 'unknown'}>
      <ReportContent />
    </ReportProvider>
  )
}

function CliCommand({ jobId }: { jobId: string }) {
  const { isCopied, copy } = useClipboard()
  const command = `jji results show ${jobId} --json`

  return (
    <p className="flex items-center gap-1.5">
      CLI:{' '}
      <code className="font-mono bg-background-offset px-1.5 py-0.5 rounded">{command}</code>
      <button
        type="button"
        onClick={() => copy(command)}
        className="inline-flex items-center hover:text-text-secondary transition-colors"
        title="Copy CLI command"
        aria-label={isCopied() ? 'CLI command copied' : 'Copy CLI command'}
      >
        {isCopied() ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </p>
  )
}

function ReportContent() {
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const state = useReportState()
  const dispatch = useReportDispatch()
  const refreshEnrichments = useRefreshEnrichments()

  // Capture hash fragment on mount and listen for changes (child-job deep linking)
  const [activeHash, setActiveHash] = useState(() => window.location.hash.replace(/^#/, ''))

  useEffect(() => {
    const onHashChange = () => setActiveHash(window.location.hash.replace(/^#/, ''))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  // Single-flight comment fetcher shared by initial hydrate and polling.
  // Prevents overlapping /comments requests: if a request is in-flight,
  // subsequent calls are queued and only the latest is executed when the
  // current one finishes.
  const commentSeqRef = useRef(0)
  const prevCommentsJsonRef = useRef<string>('')
  const commentInFlightRef = useRef(false)
  const pendingCommentFetchRef = useRef<string | null>(null)
  const latestLocalMutationRevRef = useRef(state.localMutationRev)
  latestLocalMutationRevRef.current = state.localMutationRev

  const fetchComments = useCallback((fetchJobId: string) => {
    if (commentInFlightRef.current) {
      pendingCommentFetchRef.current = fetchJobId
      commentSeqRef.current += 1
      return
    }
    commentInFlightRef.current = true
    pendingCommentFetchRef.current = null
    const thisSeq = ++commentSeqRef.current
    // Snapshot the current local mutation revision so we can detect
    // optimistic edits that happened while the request was in-flight.
    const mutationRevAtFetch = latestLocalMutationRevRef.current
    api.get<CommentsAndReviews>(`/results/${fetchJobId}/comments`)
      .then((res) => {
        if (thisSeq === commentSeqRef.current && mutationRevAtFetch === latestLocalMutationRevRef.current) {
          const json = JSON.stringify(res)
          if (json !== prevCommentsJsonRef.current) {
            prevCommentsJsonRef.current = json
            dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: res })
            refreshEnrichments(fetchJobId)
          }
        }
      })
      .catch(() => { /* comment fetch is best-effort */ })
      .finally(() => {
        commentInFlightRef.current = false
        const pending = pendingCommentFetchRef.current
        if (pending) {
          pendingCommentFetchRef.current = null
          fetchComments(pending)
        }
      })
  }, [dispatch, refreshEnrichments])

  useEffect(() => {
    if (!jobId) return

    let cancelled = false

    async function load() {
      dispatch({ type: 'SET_LOADING', payload: true })
      try {
        // Result is required
        const resultRes = await api.get<ResultResponse>(`/results/${jobId}`)
        if (cancelled) return

        // Check status first to avoid flash of wrong state
        if (resultRes.status === 'pending' || resultRes.status === 'running' || resultRes.status === 'waiting') {
          navigate(`/status/${jobId}`, { replace: true })
          return
        }

        if (resultRes.status === 'failed') {
          navigate(`/status/${jobId}`, { replace: true })
          return
        }

        if (!resultRes.result) {
          dispatch({ type: 'SET_ERROR', payload: 'No result data found.' })
          return
        }

        dispatch({ type: 'SET_RESULT', payload: { result: resultRes.result, createdAt: resultRes.created_at, completedAt: resultRes.completed_at ?? '', analysisStartedAt: resultRes.analysis_started_at ?? '' } })

        // Use capabilities from the result response (job-scoped, avoids separate call)
        if (resultRes.capabilities) {
          dispatch({ type: 'SET_GITHUB_ISSUES_ENABLED', payload: resultRes.capabilities?.github_issues_enabled ?? false })
          dispatch({ type: 'SET_JIRA_ISSUES_ENABLED', payload: resultRes.capabilities?.jira_issues_enabled ?? false })
          dispatch({ type: 'SET_REPORTPORTAL_AVAILABLE', payload: resultRes.capabilities?.reportportal ?? false })
          dispatch({ type: 'SET_REPORTPORTAL_PROJECT', payload: resultRes.capabilities?.reportportal_project ?? '' })
          dispatch({ type: 'SET_SERVER_JIRA_PROJECT_KEY', payload: resultRes.capabilities?.server_jira_project_key ?? '' })
        }

        // Initial comment fetch via the shared single-flight helper
        fetchComments(jobId)

        // AI configs and classifications are best-effort
        const [aiConfigsResult, classificationsResult] = await Promise.allSettled([
          api.get<AiConfig[]>('/ai-configs'),
          api.get<{ classifications: Array<{ test_name: string; classification: string; job_name: string; parent_job_name: string; reason: string; references_info: string; created_by: string; job_id: string; child_build_number: number; created_at: string }> }>(
            `/history/classifications?job_id=${jobId}`,
          ),
        ])
        if (cancelled) return

        if (aiConfigsResult.status === 'fulfilled') {
          dispatch({ type: 'SET_AI_CONFIGS', payload: aiConfigsResult.value })
        }
        if (classificationsResult.status === 'fulfilled') {
          const classMap: Record<string, string> = {}
          const sorted = [...(classificationsResult.value.classifications ?? [])]
            .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
          for (const c of sorted) {
            // Use composite key to handle same test_name across different child jobs
            const key = reviewKey(c.test_name, c.job_name, c.child_build_number)
            // API returns newest first — only keep the latest classification per key
            if (!(key in classMap)) {
              classMap[key] = c.classification
            }
          }
          dispatch({ type: 'SET_CLASSIFICATIONS', payload: classMap })
        }
      } catch (err) {
        if (!cancelled) {
          dispatch({ type: 'SET_ERROR', payload: err instanceof Error ? err.message : 'Failed to load report' })
        }
      }
    }

    load()
    return () => { cancelled = true }
  }, [jobId, navigate, dispatch, refreshEnrichments, fetchComments])

  // Poll for new comments every 30 seconds (single-flight via fetchComments).
  // Pauses while user is typing in any comment textarea to avoid overwriting draft state.
  useEffect(() => {
    if (!jobId || state.error || !state.result) return

    const interval = setInterval(() => {
      if (state.commentDraftCount > 0) return
      fetchComments(jobId)
    }, COMMENT_POLL_MS)

    return () => {
      clearInterval(interval)
    }
  }, [jobId, fetchComments, state.commentDraftCount, state.error, state.result])

  // Preserve scroll position across F5 refreshes
  const scrollKey = `jji-scroll-${jobId}`

  // Restore scroll after data loads (not on mount — skeleton is too short)
  useEffect(() => {
    if (state.loading || state.error) return
    if (window.location.hash) return
    let saved: string | null = null
    try {
      saved = sessionStorage.getItem(scrollKey)
    } catch {
      /* storage may be blocked in privacy mode */
    }
    let raf1 = 0
    let raf2 = 0
    if (saved) {
      // Double rAF: first lets React commit DOM, second lets browser layout
      raf1 = requestAnimationFrame(() => {
        raf2 = requestAnimationFrame(() => {
          window.scrollTo(0, parseInt(saved, 10))
        })
      })
    }
    return () => {
      cancelAnimationFrame(raf1)
      cancelAnimationFrame(raf2)
    }
  }, [state.loading, state.error, scrollKey])

  // Scroll to comment when ?comment=ID is present in query params
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const commentId = params.get('comment')
    if (!commentId || state.loading || state.error) return

    let cancelled = false
    let attempts = 0
    const maxAttempts = 20 // 20 × 200ms = 4s max wait

    const tryScroll = () => {
      if (cancelled) return
      const el = document.getElementById(`comment-${commentId}`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.classList.add('ring-2', 'ring-accent-blue', 'ring-offset-2')
        setTimeout(() => {
          if (!cancelled) el.classList.remove('ring-2', 'ring-accent-blue', 'ring-offset-2')
        }, 3000)
        return
      }
      attempts++
      if (attempts < maxAttempts) {
        setTimeout(tryScroll, 200)
      }
    }

    tryScroll()
    return () => { cancelled = true }
  }, [location.search, state.loading, state.error])

  // Save scroll position on scroll (debounced)
  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout>
    function handleScroll() {
      clearTimeout(timeout)
      timeout = setTimeout(() => {
        try {
          sessionStorage.setItem(scrollKey, String(window.scrollY))
        } catch {
          /* scroll persistence is best-effort */
        }
      }, 150)
    }
    window.addEventListener('scroll', handleScroll, { passive: true })
    return () => {
      clearTimeout(timeout)
      window.removeEventListener('scroll', handleScroll)
    }
  }, [scrollKey])

  // Derived values (safe to compute even when result is null)
  const result = state.result
  const groups = useMemo(
    () => (result ? groupFailures(result.failures ?? []) : []),
    [result],
  )
  const totalFailures = useMemo(
    () => (result ? countAllFailures(result.failures ?? [], result.child_job_analyses ?? []) : 0),
    [result],
  )
  const allTestKeys = useMemo(
    () => (result ? collectAllTestKeys(result.failures ?? [], result.child_job_analyses ?? []) : []),
    [result],
  )
  const repoUrls = useMemo<RepoUrl[]>(
    () => buildRepoUrls(result?.request_params),
    [result?.request_params],
  )
  const reviewedCount = allTestKeys.filter((k) => state.reviews[k]?.reviewed).length

  /** Format the AI provider/model label for display. */
  const formatAiLabel = (provider: string | undefined, model: string | undefined): string =>
    provider ? (model ? `${provider} / ${model}` : provider) : ''

  // Expand/collapse all for top-level failure cards
  const getFailureKeys = useCallback(
    () => (result ? groups.map((g) => `jji-expand-${result.job_id}-${g.id}`) : []),
    [groups, result],
  )
  const { remountKey: failureRemountKey, expandAll: expandAllFailures, collapseAll: collapseAllFailures } =
    useExpandCollapseAll(getFailureKeys)

  // Expand/collapse all for child job sections (and their nested failure cards)
  const getChildKeys = useCallback(() => {
    if (!result) return []
    return collectChildExpandKeys(result.child_job_analyses ?? [], result.job_id)
  }, [result])
  const { remountKey: childRemountKey, expandAll: expandAllChildren, collapseAll: collapseAllChildren } =
    useExpandCollapseAll(getChildKeys)

  /* ---- Loading skeleton ---- */
  if (state.loading) {
    return (
      <div className="space-y-6" aria-busy="true" aria-label="Loading report">
        <Skeleton className="h-10 w-96" />
        <Skeleton className="h-24 w-full" />
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-32 w-full" />
        ))}
      </div>
    )
  }

  /* ---- Error ---- */
  if (state.error) {
    return (
      <div className="flex flex-col items-center justify-center py-20 animate-fade-in" role="alert">
        <p className="text-signal-red text-sm">{state.error}</p>
      </div>
    )
  }

  // After early returns, result is guaranteed to be non-null
  if (!result) return null

  return (
    <TooltipProvider delayDuration={200}>
    <div className="space-y-6 animate-fade-in">
      {/* ---- Sticky header ---- */}
      <div className="sticky top-14 z-40 -mx-4 bg-surface-page/95 backdrop-blur-sm px-4 py-3 border-b border-border-muted sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-display text-lg font-bold text-text-primary truncate">
            {result.job_name || result.job_id}
          </h1>
          {result.build_number > 0 && (
            result.jenkins_url ? (
              <a href={String(result.jenkins_url)} target="_blank" rel="noopener noreferrer" className="font-mono text-sm text-text-link hover:underline">
                #{result.build_number}
              </a>
            ) : (
              <span className="font-mono text-sm text-text-tertiary">#{result.build_number}</span>
            )
          )}
          <StatusChip status={isAnalysisTimeout(result.status, result.error, result.summary) ? 'timeout' : result.status} />
          <Badge variant="destructive" className="font-mono">
            {totalFailures} {totalFailures === 1 ? 'failure' : 'failures'}
          </Badge>
          {/* Review progress badge */}
          {totalFailures > 0 && (
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold font-display ${
                reviewedCount === totalFailures
                  ? 'bg-signal-green/15 text-signal-green'
                  : reviewedCount > 0
                    ? 'bg-signal-orange/15 text-signal-orange'
                    : 'bg-signal-red/12 text-signal-red'
              }`}
            >
              {reviewedCount === totalFailures ? (
                <><CheckCircle2 className="h-3 w-3" /> Reviewed</>
              ) : reviewedCount > 0 ? (
                `${reviewedCount}/${totalFailures} Reviewed`
              ) : (
                'Needs Review'
              )}
            </span>
          )}
          {result.ai_provider && (
            <Badge variant="outline" className="text-[10px]">
              {formatAiLabel(result.ai_provider, result.ai_model)}
            </Badge>
          )}
          {result.token_usage && (
            <TokenUsageBadge usage={result.token_usage} />
          )}
          <div className="ml-auto flex items-center gap-3">
            {state.reportportalAvailable && (result.child_job_analyses ?? []).length === 0 && (
              <ReportPortalButton jobId={result.job_id} jobName={result.job_name ?? result.job_id} buildNumber={result.build_number} hasFailures={(result.failures ?? []).length > 0} />
            )}
            {result.request_params && (
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={() => dispatch({ type: 'SET_RE_ANALYZE_OPEN', payload: true })}
                disabled={result.status === 'running' || result.status === 'pending' || result.status === 'waiting'}
              >
                <RotateCw className="h-3.5 w-3.5" />
                Re-Analyze
              </Button>
            )}
            {result.jenkins_url && (
              <a
                href={String(result.jenkins_url)}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-text-link hover:underline"
              >
                Jenkins <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        </div>
      </div>

      {/* ---- Metadata detail row ---- */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-text-tertiary animate-slide-up">
          {state.createdAt && (
            <span className="inline-flex items-center gap-1">
              <Calendar className="h-3 w-3" />
              {formatTimestamp(state.createdAt)}
            </span>
          )}
          {state.completedAt && (state.analysisStartedAt || state.createdAt) && (
            (() => {
              const start = parseApiTimestamp(state.analysisStartedAt || state.createdAt)
              const end = parseApiTimestamp(state.completedAt)
              if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null
              return (
                <span className="inline-flex items-center gap-1">
                  <Timer className="h-3 w-3" />
                  {formatDuration(start, end)}
                </span>
              )
            })()
          )}
          {result.ai_provider && (
            <span className="inline-flex items-center gap-1">
              <Cpu className="h-3 w-3" />
              {formatAiLabel(result.ai_provider, result.ai_model)}
            </span>
          )}
          {repoUrls.length > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex items-center gap-1 cursor-default">
                  <FolderGit2 className="h-3 w-3" />
                  {repoUrls.length} repo{repoUrls.length !== 1 ? 's' : ''}: {repoUrls.map(r => r.name).join(', ')}
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-sm">
                <div className="flex flex-col gap-1">
                  {repoUrls.map((r) => (
                    <div key={`${r.name}::${r.url}`} className="flex flex-col">
                      <span className="font-medium">{r.name}</span>
                      <span className="text-text-tertiary break-all">{r.url}</span>
                    </div>
                  ))}
                </div>
              </TooltipContent>
            </Tooltip>
          )}
          {state.completedAt && (
            <span className="inline-flex items-center gap-1">
              <Clock className="h-3 w-3" />
              Completed {formatTimestamp(state.completedAt)}
            </span>
          )}
        </div>

      {/* ---- Key takeaway ---- */}
      {result.summary && (
        <div className="rounded-lg border-l-4 border-l-signal-orange bg-glow-orange p-4 animate-slide-up">
          <h2 className="text-xs font-display uppercase tracking-widest text-signal-orange mb-2">Key Takeaway</h2>
          <p className="text-sm text-text-secondary whitespace-pre-wrap">{result.summary}</p>
        </div>
      )}

      {/* ---- Peer analysis summary ---- */}
      <PeerAnalysisSummary
        failures={result.failures ?? []}
        childJobAnalyses={result.child_job_analyses ?? []}
        repoUrls={repoUrls}
      />

      {/* ---- Top-level failures ---- */}
      {groups.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-display uppercase tracking-widest text-text-tertiary">
              Failures ({(result.failures ?? []).length})
            </h2>
            {groups.length >= 2 && (
              <ExpandCollapseButtons onExpandAll={expandAllFailures} onCollapseAll={collapseAllFailures} />
            )}
          </div>
          <div className="space-y-3" key={failureRemountKey}>
            {groups.map((g, i) => (
              <FailureCard key={g.id} group={g} jobId={result.job_id} index={i} />
            ))}
          </div>
        </section>
      )}

      {/* ---- Child jobs ---- */}
      {(result.child_job_analyses ?? []).length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-display uppercase tracking-widest text-text-tertiary">
              Child Jobs ({(result.child_job_analyses ?? []).length})
            </h2>
            <ExpandCollapseButtons onExpandAll={expandAllChildren} onCollapseAll={collapseAllChildren} />
          </div>
          <div className="space-y-6" key={childRemountKey}>
            {(result.child_job_analyses ?? []).map((child) => (
              <ChildJobSection key={`${child.job_name}-${child.build_number}`} child={child} jobId={result.job_id} activeHash={activeHash} />
            ))}
          </div>
        </section>
      )}

      {/* ---- Footer ---- */}
      <footer className="border-t border-border-muted pt-4 pb-8 text-xs text-text-tertiary space-y-1">
        <p>
          Job ID: <span className="font-mono">{result.job_id}</span>
        </p>
        <CliCommand jobId={result.job_id} />
        {state.completedAt && <p>Completed: {formatTimestamp(state.completedAt)}</p>}
        {result.ai_provider && (
          <p>
            AI: {result.ai_provider}
            {result.ai_model ? ` (${result.ai_model})` : ''}
          </p>
        )}
      </footer>
    </div>
      <AllReviewedPrompt jobId={result.job_id} />
      {result.request_params && (
        <ReAnalyzeDialog
          open={state.reAnalyzeOpen}
          onOpenChange={(v) => dispatch({ type: 'SET_RE_ANALYZE_OPEN', payload: v })}
          result={result}
          jobId={jobId!}
        />
      )}
    </TooltipProvider>
  )
}
