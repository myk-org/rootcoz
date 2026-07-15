import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useSSE } from '@/lib/SSEProvider'
import { api, ApiError } from '@/lib/api'
import { formatTimestamp, isAnalysisTimeout, INVALID_DATE_FALLBACK, ciSourceLabel, resolveBuildUrl, resolveBuildDisplayId } from '@/lib/utils'
import type { ResultResponse } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { AlertTriangle, Clock, ExternalLink, Loader2, RotateCw, XCircle } from 'lucide-react'
import { StatusChip } from '@/components/shared/StatusChip'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { ReAnalyzeDialog } from './report/ReAnalyzeDialog'
import { OriginJobBanner } from '@/components/shared/OriginJobBanner'
import { originJobLabel } from '@/lib/originJobLabel'
import { useAuth } from '@/lib/auth'

const phaseLabels: Record<string, string> = {
  waiting_for_jenkins: 'Waiting for Jenkins build to complete...',
  analyzing: 'Analyzing test failures with AI...',
  analyzing_child_jobs: 'Analyzing child job failures...',
  analyzing_failures: 'Analyzing test failures...',
  enriching_jira: 'Searching Jira for matching bugs...',
  saving: 'Saving results...',
}

function getPhaseLabel(phase: string | undefined): string | undefined {
  if (!phase) return undefined
  if (phaseLabels[phase]) return phaseLabels[phase]

  // Handle peer_review_round_N or peer_review_round_N (group X/Y)
  const peerMatch = phase.match(/^peer_review_round_(\d+)(?:\s*\(group (.+)\))?$/)
  if (peerMatch) {
    const groupInfo = peerMatch[2] ? ` \u2014 group ${peerMatch[2]}` : ''
    return `Peer review \u2014 round ${peerMatch[1]}${groupInfo}...`
  }

  // Handle orchestrator_revising_round_N or orchestrator_revising_round_N (group X/Y)
  const reviseMatch = phase.match(/^orchestrator_revising_round_(\d+)(?:\s*\(group (.+)\))?$/)
  if (reviseMatch) {
    const groupInfo = reviseMatch[2] ? ` \u2014 group ${reviseMatch[2]}` : ''
    return `Main AI revising \u2014 round ${reviseMatch[1]}${groupInfo}...`
  }

  return undefined
}

const statusMessages: Record<string, { title: string; subtitle: string }> = {
  waiting: {
    title: 'Waiting for CI job',
    subtitle: 'Monitoring build until it completes...',
  },
  pending: {
    title: 'Analysis queued',
    subtitle: 'Waiting in the analysis queue...',
  },
  running: {
    title: 'Analysis in progress',
    subtitle: 'Crunching test results with AI...',
  },
  completed: {
    title: 'Analysis complete',
    subtitle: 'Analysis finished.',
  },
  aborted: {
    title: 'Analysis aborted',
    subtitle: 'This analysis was cancelled by a user.',
  },
}

/** Title text for terminal error states rendered via the error branch. */
const terminalErrorTitles: Record<string, string> = {
  not_found: 'Job not found',
  unauthorized: 'Access denied',
  failed: 'Analysis failed',
  aborted: 'Analysis aborted',
}

interface StepLogEntry {
  phase: string
  label: string
  timestamp: string
}

export function StatusPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const [data, setData] = useState<ResultResponse | null>(null)
  const [error, setError] = useState('')
  const [terminalErrorKind, setTerminalErrorKind] = useState<'not_found' | 'unauthorized' | 'failed' | 'aborted' | null>(null)
  const [reAnalyzeOpen, setReAnalyzeOpen] = useState(false)
  const [isAborting, setIsAborting] = useState(false)
  const [abortConfirmOpen, setAbortConfirmOpen] = useState(false)
  const prevLogLenRef = useRef(0)
  const logEndRef = useRef<HTMLDivElement>(null)
  const logContainerRef = useRef<HTMLDivElement>(null)

  const [sseTopic, setSseTopic] = useState<string | null>(null)

  useEffect(() => {
    if (!jobId) return

    let cancelled = false
    let inFlight = false
    let pendingRefresh = false
    setData(null)
    setError('')
    setTerminalErrorKind(null)
    setReAnalyzeOpen(false)
    prevLogLenRef.current = 0
    async function fetchStatus() {
      if (inFlight || cancelled) {
        pendingRefresh = true
        return 'continue'
      }
      inFlight = true
      pendingRefresh = false
      try {
        const res = await api.get<ResultResponse>(`/results/${jobId}`)
        if (cancelled) return
        setError('')
        setData(res)

        if (res.status === 'completed') {
          navigate(`/results/${jobId}`, { replace: true })
          setSseTopic(null) // disconnect SSE
          return 'terminal'
        } else if (res.status === 'failed') {
          setTerminalErrorKind('failed')
          setError(res.error || res.result?.error || 'Analysis failed')
          setSseTopic(null)
          return 'terminal'
        } else if (res.status === 'aborted') {
          setTerminalErrorKind('aborted')
          setError(res.error || res.result?.error || 'Analysis was aborted')
          setSseTopic(null)
          return 'terminal'
        }
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
            setTerminalErrorKind(err.status === 404 ? 'not_found' : 'unauthorized')
            setError(
              err.status === 404
                ? 'Job not found. It may have been deleted.'
                : 'Access denied. You are not authorized to view this job.'
            )
            setData(null)
            setSseTopic(null)
            return 'terminal'
          } else {
            setTerminalErrorKind(null)
            setError('Failed to reach the server. Retrying...')
          }
        }
      } finally {
        inFlight = false
        if (pendingRefresh && !cancelled) {
          pendingRefresh = false
          fetchStatus()
        }
      }
      return 'continue'
    }

    statusFetchRef.current = fetchStatus

    // Initial fetch
    fetchStatus()

    // Enable SSE stream for real-time updates
    setSseTopic(`results:${jobId}`)

    return () => {
      cancelled = true
      setSseTopic(null)
    }
  }, [jobId, navigate])

  // Ref-based fetch function accessible by SSE callback
  const statusFetchRef = useRef<() => Promise<string | undefined>>(async () => undefined)

  const statusEvents = useMemo(() => ({
    'status-changed': () => {
      statusFetchRef.current()
    },
  }), [])

  useSSE(sseTopic, statusEvents)

  async function handleAbort() {
    setIsAborting(true)
    try {
      await api.post(`/results/${jobId}/abort`)
      await statusFetchRef.current()
    } catch (err) {
      console.error('Failed to abort analysis:', err)
      setError('Failed to abort analysis. Please try again.')
    } finally {
      setIsAborting(false)
      setAbortConfirmOpen(false)
    }
  }

  // Derive stepLog from server-persisted progress_log (survives F5 refresh)
  const rawProgressLog = data?.result?.progress_log
  const progressLog = Array.isArray(rawProgressLog) ? rawProgressLog : []
  const stepLog: StepLogEntry[] = useMemo(
    () => progressLog.map(entry => ({
      phase: entry.phase,
      label: getPhaseLabel(entry.phase) ?? entry.phase,
      timestamp: new Date(entry.timestamp * 1000).toLocaleTimeString(),
    })),
    [progressLog],
  )

  useEffect(() => {
    if (stepLog.length > prevLogLenRef.current) {
      const container = logContainerRef.current
      if (container) {
        // On first load (prevLogLenRef was 0), always scroll to bottom
        const isFirstLoad = prevLogLenRef.current === 0
        const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50
        if (isFirstLoad || isNearBottom) {
          logEndRef.current?.scrollIntoView({ behavior: isFirstLoad ? 'instant' : 'smooth' })
        }
      }
      prevLogLenRef.current = stepLog.length
    }
  }, [stepLog.length])

  const status = data?.status ?? terminalErrorKind ?? 'pending'
  const isTimeout = isAnalysisTimeout(status, error, data?.result?.summary)
  const displayStatus = isTimeout ? 'timeout' : status
  const queuedAtDisplay = data?.created_at ? formatTimestamp(data.created_at) : null

  const params = data?.result?.request_params
  const mainAi = params?.ai_provider && params?.ai_model
    ? `${params.ai_provider} / ${params.ai_model}`
    : null
  const peers = params?.peer_ai_configs
  const hasPeers = !!peers?.length
  const progressPhase = data?.result?.progress_phase
  const isRunning = displayStatus === 'running'
  const isWaiting = displayStatus === 'waiting'
  const isActive = isRunning || isWaiting
  const isAbortable = !!data && ['running', 'waiting', 'pending'].includes(data.status)
  const msg = statusMessages[displayStatus] ?? statusMessages.running
  const statusBadgeLabel = displayStatus.replace(/_/g, ' ').toUpperCase()

  const { isAdmin, username, role, isOperator } = useAuth()
  const isViewer = role === 'viewer'
  const submitter = data?.result?.request_params?.submitted_by ?? ''
  const canAbort = isAdmin || (!!username && username === submitter)
  const buildUrl = resolveBuildUrl(data?.result) ?? resolveBuildUrl(data)
  const buildDisplayId = resolveBuildDisplayId(data?.result)

  return (
    <>
      {/* Header for failed jobs — matches report page layout */}
      {(terminalErrorKind === 'failed' || terminalErrorKind === 'aborted') && data?.result && (
        <div className="w-full border-b border-border-muted">
          <div className="mx-auto max-w-[1400px] px-4 py-3 sm:px-6 lg:px-8">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="font-display text-lg font-bold text-text-primary truncate">
                {data.result.job_name || jobId}
              </h1>
              {buildDisplayId && (
                buildUrl ? (
                  <a href={buildUrl} target="_blank" rel="noopener noreferrer" className="font-mono text-sm text-text-link hover:underline">
                    #{buildDisplayId}
                  </a>
                ) : (
                  <span className="font-mono text-sm text-text-tertiary">#{buildDisplayId}</span>
                )
              )}
              <StatusChip status={displayStatus} />
              {data.result.request_params?.ai_provider && (
                <Badge variant="outline" className="text-[10px]">
                  {data.result.request_params.ai_provider}{data.result.request_params.ai_model ? ` / ${data.result.request_params.ai_model}` : ''}
                </Badge>
              )}
              <div className="ml-auto flex items-center gap-3">
                {isOperator && data.result.request_params && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-xs"
                    onClick={() => setReAnalyzeOpen(true)}
                  >
                    <RotateCw className="h-3.5 w-3.5" />
                    Re-Analyze
                  </Button>
                )}
                {buildUrl && (
                  <a
                    href={buildUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 text-xs text-text-link hover:underline"
                  >
                    {ciSourceLabel(data.result?.request_params)} <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="relative flex min-h-screen items-start justify-center overflow-x-hidden overflow-y-auto bg-surface-page py-8 sm:items-center">
        {/* Scan-line overlay */}
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.015]"
          style={{
            backgroundImage:
              'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(56,139,253,.3) 2px, rgba(56,139,253,.3) 4px)',
          }}
        />

        <div className="relative z-10 w-full max-w-xl px-4">
        <Card className="animate-slide-up border-border-muted">
          <CardContent className="flex flex-col items-center gap-6 p-8">
            {/* Pulsing / spinning indicator */}
            <div className="relative flex h-24 w-24 items-center justify-center">
              {/* Outer ring */}
              <svg className="absolute inset-0 h-full w-full" viewBox="0 0 96 96">
                {/* Track */}
                <circle
                  cx="48" cy="48" r="42"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1"
                  className="text-border-muted"
                />
                {/* Active arc */}
                <circle
                  cx="48" cy="48" r="42"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeDasharray={isActive ? '80 184' : '264 0'}
                  className={`text-signal-blue ${isActive ? 'animate-spin-slow' : ''}`}
                  style={{ transformOrigin: 'center' }}
                />
              </svg>

              {/* Center icon: Clock for waiting, spinner for running, dot for pending */}
              {isWaiting ? (
                <Clock className="h-6 w-6 text-signal-blue animate-pulse-ring" />
              ) : isRunning ? (
                <Loader2 className="h-6 w-6 text-signal-blue animate-spin" />
              ) : terminalErrorKind === 'aborted' ? (
                <XCircle className="h-6 w-6 text-signal-orange" />
              ) : terminalErrorKind ? (
                <div className="h-3 w-3 rounded-full bg-signal-red" />
              ) : (
                <div className="h-3 w-3 rounded-full bg-signal-blue" />
              )}

              {/* Ambient glow */}
              <div className="pointer-events-none absolute inset-0 rounded-full bg-signal-blue/[0.06] blur-xl" />
            </div>

            {/* Status label */}
            <div className="text-center" aria-live="polite" aria-atomic="true">
              {error && terminalErrorKind ? (
                isTimeout ? (
                  <>
                    <div className="flex items-center justify-center gap-2">
                      <Clock className="h-5 w-5 text-signal-orange" />
                      <h2 className="font-display text-lg font-semibold text-signal-orange">
                        AI Analysis Timed Out
                      </h2>
                    </div>
                    <p className="mt-2 text-sm text-signal-orange/80 bg-signal-orange/10 rounded-md px-3 py-2">
                      The AI analysis timed out. You can re-analyze with a longer timeout.
                    </p>
                  </>
                ) : terminalErrorKind === 'aborted' ? (
                  <>
                    <h2 className="font-display text-lg font-semibold text-signal-orange">
                      {terminalErrorTitles.aborted}
                    </h2>
                    <p className="mt-2 text-sm text-signal-orange/80 bg-signal-orange/10 rounded-md px-3 py-2">
                      This analysis was cancelled by a user. You can re-analyze if needed.
                    </p>
                  </>
                ) : (
                  <>
                    <h2 className="font-display text-lg font-semibold text-signal-red">
                      {terminalErrorTitles[terminalErrorKind] ?? terminalErrorTitles.failed}
                    </h2>
                    {data?.result?.error ? (
                      <div className="mt-3 w-full rounded-lg border border-signal-red/20 bg-signal-red/5 p-4 text-left">
                        <div className="flex items-start gap-2.5">
                          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-signal-red/70" />
                          <div className="min-w-0 space-y-1">
                            <p className="text-xs font-medium uppercase tracking-wide text-signal-red/70">Error Details</p>
                            <p className="text-sm text-text-secondary whitespace-pre-wrap break-words">
                              {data.result.error}
                            </p>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <p className="mt-2 text-sm text-signal-red/80 bg-signal-red/10 rounded-md px-3 py-2">
                        {error}
                      </p>
                    )}
                  </>
                )
              ) : (
                <>
                  <h2 className="font-display text-lg font-semibold text-text-primary">
                    {msg.title}
                  </h2>
                  <p className="mt-1 text-sm text-text-tertiary">
                    {getPhaseLabel(progressPhase) ?? progressPhase ?? msg.subtitle}
                  </p>
                </>
              )}
            </div>

            {/* Metadata rows */}
            <div className="w-full space-y-2 rounded-md border border-border-muted bg-surface-elevated/50 p-4 text-sm">
              <Row label="JOB ID" value={jobId ?? '—'} mono />
              {data?.result?.job_name && (
                <Row label="JOB" value={data.result.job_name} mono />
              )}
              {buildDisplayId && (
                <Row
                  label="BUILD"
                  value={
                    buildUrl ? (
                      <a href={buildUrl} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline font-mono">
                        #{buildDisplayId}
                      </a>
                    ) : (
                      `#${buildDisplayId}`
                    )
                  }
                  mono
                />
              )}
              <Row
                label="STATUS"
                value={
                  <Badge variant={isRunning || isWaiting ? 'default' : displayStatus === 'timeout' ? 'warning' : displayStatus === 'failed' ? 'destructive' : 'outline'}>
                    {statusBadgeLabel}
                  </Badge>
                }
              />
              {mainAi && (
                <Row label="MAIN AI" value={mainAi} mono />
              )}
              {hasPeers && (
                <Row
                  label="PEERS"
                  alignTop
                  value={
                    <div className="flex flex-col items-end gap-0.5">
                      {peers!.map((p, i) => (
                        <span key={i} className="font-mono text-xs">
                          {p.ai_provider} / {p.ai_model}
                        </span>
                      ))}
                    </div>
                  }
                />
              )}
              {queuedAtDisplay && queuedAtDisplay !== INVALID_DATE_FALLBACK && (
                <Row
                  label="QUEUED"
                  value={queuedAtDisplay}
                />
              )}
            </div>

            {data?.result?.source_warnings && data.result.source_warnings.length > 0 && (
              <div className="w-full rounded-lg border border-border-default bg-bg-secondary p-4 text-left">
                <h3 className="text-xs font-display uppercase tracking-widest text-signal-orange mb-2">
                  Source Warnings
                </h3>
                <ul className="list-disc pl-4 space-y-1 text-sm text-text-secondary">
                  {data.result.source_warnings.map((warning, i) => (
                    <li key={`sw-${i}`}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Origin job reference for re-analyses */}
            {data?.reanalyzed_from_job_id && (
              <OriginJobBanner
                originJobId={data.reanalyzed_from_job_id}
                originJobName={originJobLabel(data.origin_job_name, data.reanalyzed_from_job_id)}
              />
            )}

            {isAbortable && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>
                      <Button
                        variant="destructive"
                        size="sm"
                        className="gap-1.5"
                        disabled={isAborting || !canAbort}
                        onClick={() => setAbortConfirmOpen(true)}
                      >
                        <XCircle className="h-3.5 w-3.5" />
                        {isAborting ? 'Aborting\u2026' : 'Abort'}
                      </Button>
                    </span>
                  </TooltipTrigger>
                  {!canAbort && (
                    <TooltipContent>Only the submitter or an admin can abort this analysis</TooltipContent>
                  )}
                </Tooltip>
              </TooltipProvider>
            )}

            {stepLog.length > 0 && (
              <div className="w-full rounded-md border border-border-muted bg-surface-elevated/30 overflow-hidden">
                <div className="px-3 py-1.5 border-b border-border-muted">
                  <span className="font-display text-[10px] font-medium uppercase tracking-widest text-text-tertiary">
                    Progress
                  </span>
                </div>
                <div ref={logContainerRef} className="max-h-64 overflow-y-auto px-3 py-2 space-y-1">
                  {stepLog.map((step, i) => {
                    const isLatest = i === stepLog.length - 1
                    return (
                      <div key={i} className={`flex items-start gap-2 text-xs ${isLatest ? 'text-signal-blue' : 'text-text-tertiary'}`}>
                        <span className="shrink-0 font-mono text-[10px] text-text-tertiary/60">
                          {step.timestamp}
                        </span>
                        {isLatest && isActive ? (
                          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-signal-blue mt-0.5" />
                        ) : isLatest && (displayStatus === 'failed' || displayStatus === 'timeout') ? (
                          <span className="shrink-0 text-signal-red mt-0.5">!</span>
                        ) : (
                          <span className="shrink-0 text-signal-green mt-0.5">{'\u2713'}</span>
                        )}
                        <span className={isLatest ? 'font-medium' : ''}>
                          {step.label}
                        </span>
                      </div>
                    )
                  })}
                  <div ref={logEndRef} />
                </div>
              </div>
            )}

            {error && !terminalErrorKind && (
              <p
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className="text-xs text-signal-orange animate-fade-in"
              >
                {error}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      </div>

      <ConfirmDialog
        open={abortConfirmOpen}
        onOpenChange={setAbortConfirmOpen}
        title="Abort analysis"
        description="Are you sure you want to abort this analysis? This cannot be undone."
        confirmLabel="Abort"
        variant="destructive"
        onConfirm={handleAbort}
        loading={isAborting}
      />

      {jobId && (terminalErrorKind === 'failed' || terminalErrorKind === 'aborted') && data?.result?.request_params && (
        <ReAnalyzeDialog
          open={reAnalyzeOpen}
          onOpenChange={setReAnalyzeOpen}
          result={data.result}
          jobId={jobId}
        />
      )}
    </>
  )
}

function Row({
  label,
  value,
  mono,
  alignTop,
}: {
  label: string
  value: ReactNode
  mono?: boolean
  alignTop?: boolean
}) {
  return (
    <div className={`flex justify-between gap-4 ${alignTop ? 'items-start' : 'items-center'}`}>
      <span className="whitespace-nowrap font-display text-[10px] font-medium uppercase tracking-widest text-text-tertiary">
        {label}
      </span>
      <div
        className={`text-right text-text-secondary break-all ${mono ? 'font-mono text-xs' : ''}`}
      >
        {value}
      </div>
    </div>
  )
}
