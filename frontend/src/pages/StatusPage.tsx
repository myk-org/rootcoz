import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '@/lib/api'
import { formatTimestamp, isAnalysisTimeout, INVALID_DATE_FALLBACK } from '@/lib/utils'
import type { ResultResponse } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Clock, ExternalLink, Loader2, RotateCw } from 'lucide-react'
import { StatusChip } from '@/components/shared/StatusChip'
import { ReAnalyzeDialog } from './report/ReAnalyzeDialog'

const POLL_MS = 10_000

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
    title: 'Waiting for Jenkins job',
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
}

/** Title text for terminal error states rendered via the error branch. */
const terminalErrorTitles: Record<string, string> = {
  not_found: 'Job not found',
  unauthorized: 'Access denied',
  failed: 'Analysis failed',
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
  const [terminalErrorKind, setTerminalErrorKind] = useState<'not_found' | 'unauthorized' | 'failed' | null>(null)
  const [reAnalyzeOpen, setReAnalyzeOpen] = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval>>(null)
  const prevLogLenRef = useRef(0)
  const logEndRef = useRef<HTMLDivElement>(null)
  const logContainerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!jobId) return

    let cancelled = false
    let inFlight = false
    const stopPolling = () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
    setData(null)
    setError('')
    setTerminalErrorKind(null)
    setReAnalyzeOpen(false)
    prevLogLenRef.current = 0

    async function poll() {
      if (inFlight || cancelled) return
      inFlight = true
      try {
        const res = await api.get<ResultResponse>(`/results/${jobId}`)
        if (cancelled) return
        setError('')
        setData(res)

        if (res.status === 'completed') {
          stopPolling()
          navigate(`/results/${jobId}`, { replace: true })
        } else if (res.status === 'failed') {
          stopPolling()
          setTerminalErrorKind('failed')
          setError(res.result?.error ?? 'Analysis failed')
        }
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
            // Permanent error — stop polling
            stopPolling()
            setTerminalErrorKind(err.status === 404 ? 'not_found' : 'unauthorized')
            setError(
              err.status === 404
                ? 'Job not found. It may have been deleted.'
                : 'Access denied. You are not authorized to view this job.'
            )
            setData(null)
          } else {
            // Transient transport error — keep polling, don't clear data
            setTerminalErrorKind(null)
            setError('Failed to reach the server. Retrying...')
          }
        }
      } finally {
        inFlight = false
      }
    }

    poll()
    intervalRef.current = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      stopPolling()
    }
  }, [jobId, navigate])

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
  const msg = statusMessages[displayStatus] ?? statusMessages.running
  const statusBadgeLabel = displayStatus.replace(/_/g, ' ').toUpperCase()

  return (
    <>
      {/* Sticky header for failed jobs — matches report page layout */}
      {terminalErrorKind === 'failed' && data?.result && (
        <div className="sticky top-14 z-40 w-full bg-surface-page/95 backdrop-blur-sm border-b border-border-muted">
          <div className="mx-auto max-w-[1400px] px-4 py-3 sm:px-6 lg:px-8">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="font-display text-lg font-bold text-text-primary truncate">
                {data.result.job_name || jobId}
              </h1>
              {data.result.build_number > 0 && (
                data.jenkins_url ? (
                  <a href={String(data.jenkins_url)} target="_blank" rel="noopener noreferrer" className="font-mono text-sm text-text-link hover:underline">
                    #{data.result.build_number}
                  </a>
                ) : (
                  <span className="font-mono text-sm text-text-tertiary">#{data.result.build_number}</span>
                )
              )}
              <StatusChip status={displayStatus} />
              {data.result.request_params?.ai_provider && (
                <Badge variant="outline" className="text-[10px]">
                  {data.result.request_params.ai_provider}{data.result.request_params.ai_model ? ` / ${data.result.request_params.ai_model}` : ''}
                </Badge>
              )}
              <div className="ml-auto flex items-center gap-3">
                {data.result.request_params && (
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
                {data.jenkins_url && (
                  <a
                    href={String(data.jenkins_url)}
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
                  className={`text-signal-blue ${isActive ? 'animate-spin-slow' : 'animate-pulse-ring'}`}
                  style={{ transformOrigin: 'center' }}
                />
              </svg>

              {/* Center icon: Clock for waiting, spinner for running, dot for pending */}
              {isWaiting ? (
                <Clock className="h-6 w-6 text-signal-blue animate-pulse-ring" />
              ) : isRunning ? (
                <Loader2 className="h-6 w-6 text-signal-blue animate-spin" />
              ) : (
                <div className="h-3 w-3 rounded-full bg-signal-blue animate-pulse-ring" />
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
                ) : (
                  <>
                    <h2 className="font-display text-lg font-semibold text-signal-red">
                      {terminalErrorTitles[terminalErrorKind] ?? terminalErrorTitles.failed}
                    </h2>
                    <p className="mt-2 text-sm text-signal-red/80 bg-signal-red/10 rounded-md px-3 py-2">
                      Analysis failed. You can re-analyze or check server logs for details.
                    </p>

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
              {data?.result?.build_number != null && (
                <Row
                  label="BUILD"
                  value={
                    data?.jenkins_url ? (
                      <a href={String(data.jenkins_url)} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline font-mono">
                        #{data.result.build_number}
                      </a>
                    ) : (
                      `#${data.result.build_number}`
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

      {jobId && terminalErrorKind === 'failed' && data?.result?.request_params && (
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
