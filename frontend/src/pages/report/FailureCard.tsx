import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { useClipboard } from '@/lib/useClipboard'
import type { GroupedFailure, PreviousAnalysis } from '@/types'
import { buildFileUrl, buildRepoUrls, isSafeHref, matchRepo, type RepoUrl } from '@/lib/autoLink'
import { isCommentInScope } from '@/lib/grouping'
import { api } from '@/lib/api'
import { getUsername } from '@/lib/cookies'
import { useSessionState } from '@/lib/useSessionState'
import { unescapeCodeContent } from '@/lib/format'
import { formatRelativeTime } from '@/lib/utils'
import { useReportState, useReportDispatch, reviewKey } from './ReportContext'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ClassificationBadge } from '@/components/shared/ClassificationBadge'
import { LinkedText } from '@/components/shared/LinkedText'
import { PeerDebateSection } from './PeerDebateSection'
import { ReviewToggle } from './ReviewToggle'
import { CommentsSection } from './CommentsSection'
import { ClassificationSelect } from './ClassificationSelect'
import { PatternSelect } from './PatternSelect'
import { BugCreationDialog } from './BugCreationDialog'
import { TrackedInBadge, TrackInDialog } from './TrackedInBadge'
import { ReAnalyzeDialog } from './ReAnalyzeDialog'
import { useReviewSuggestion } from './useReviewSuggestion'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { UuidCopyButton } from '@/components/shared/UuidCopyButton'
import { useAuth } from '@/lib/auth'
import { ChevronDown, ChevronRight, Bug, MessageSquare, CheckCircle2, Copy, Check, RotateCw, Link2 } from 'lucide-react'

function PreviousAnalysisEntry({ prev, index, repoUrls }: {
  prev: PreviousAnalysis
  index: number
  repoUrls: RepoUrl[]
}) {
  const superseded = prev._superseded_by
  const supersededLabel = superseded
    ? ` — re-analyzed with ${superseded.ai_provider}/${superseded.ai_model}${superseded.timestamp ? ` (${formatRelativeTime(superseded.timestamp)})` : ''}`
    : ''

  return (
    <details className="group/prev">
      <summary className="flex items-center gap-2 cursor-pointer text-xs font-display uppercase tracking-widest text-text-tertiary hover:text-text-secondary transition-colors">
        <ChevronRight className="h-3 w-3 group-open/prev:rotate-90 transition-transform" />
        <span>Previous Analysis #{index + 1}</span>
        {prev.classification && (
          <ClassificationBadge classification={prev.classification} className="opacity-60 text-[10px]" />
        )}
        {supersededLabel && <span className="normal-case tracking-normal font-normal text-text-tertiary">{supersededLabel}</span>}
      </summary>
      <div className="mt-2 rounded-md bg-surface-elevated/50 border border-border-muted p-3 opacity-75 space-y-3">
        {prev.details && (
          <div>
            <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">Details</h4>
            <div className="text-sm text-text-tertiary whitespace-pre-wrap">
              <LinkedText text={prev.details} repoUrls={repoUrls} />
            </div>
          </div>
        )}
        {prev.artifacts_evidence && (
          <div>
            <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">Artifacts Evidence</h4>
            <div className="overflow-x-auto rounded-md bg-surface-elevated p-2 text-xs text-text-tertiary font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
              <LinkedText text={prev.artifacts_evidence} repoUrls={repoUrls} />
            </div>
          </div>
        )}
        {prev.code_fix && typeof prev.code_fix === 'object' && (
          <div>
            <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">Suggested Fix</h4>
            <div className="rounded-md bg-surface-elevated p-2 text-sm text-text-tertiary space-y-1">
              {prev.code_fix.file && (
                <p className="font-mono text-xs">{prev.code_fix.file}{prev.code_fix.line && `:${prev.code_fix.line}`}</p>
              )}
              {prev.code_fix.change && <p className="whitespace-pre-wrap text-xs"><LinkedText text={prev.code_fix.change} repoUrls={repoUrls} /></p>}
              {prev.code_fix.original_code != null && (
                <CodeFixLiteralBlock title="Original Code" content={prev.code_fix.original_code} className="text-text-tertiary" />
              )}
              {prev.code_fix.suggested_code != null && (
                <CodeFixLiteralBlock title="Suggested Code" content={prev.code_fix.suggested_code} className="text-text-tertiary" />
              )}
            </div>
          </div>
        )}
        {prev.product_bug_report && typeof prev.product_bug_report === 'object' && (
          <div>
            <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">Bug Report</h4>
            <div className="rounded-md bg-surface-elevated p-2 text-sm text-text-tertiary space-y-1">
              {prev.product_bug_report.title && <p className="font-medium">{prev.product_bug_report.title}</p>}
              {prev.product_bug_report.severity && <Badge variant="outline" className="text-[10px] opacity-75">{prev.product_bug_report.severity}</Badge>}
              {prev.product_bug_report.description && <p className="whitespace-pre-wrap text-xs"><LinkedText text={prev.product_bug_report.description} repoUrls={repoUrls} /></p>}
            </div>
          </div>
        )}
      </div>
    </details>
  )
}

function IssueButton({ disabled, tooltip, label, onClick }: {
  disabled: boolean
  tooltip: string | undefined
  label: string
  onClick: () => void
}) {
  const button = (
    <Button variant="outline" size="sm" onClick={onClick} disabled={disabled}>
      <Bug className="h-3.5 w-3.5 mr-1" /> {label}
    </Button>
  )
  return tooltip ? (
    <Tooltip>
      <TooltipTrigger asChild><span className="inline-flex">{button}</span></TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  ) : button
}

function CopyableSectionHeader({ title, content, sectionId, copiedSection, onCopy, extra }: {
  title: string
  content: string
  sectionId: string
  copiedSection: string | null
  onCopy: (text: string, section: string) => void
  extra?: ReactNode
}) {
  return (
    <div className="flex items-center justify-between mb-2">
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary">{title}</h4>
        {extra}
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="text-text-tertiary hover:text-text-primary transition-colors"
            onClick={() => onCopy(content, sectionId)}
            aria-label={copiedSection === sectionId ? `Copied ${title}` : `Copy ${title} to clipboard`}
          >
            {copiedSection === sectionId ? <Check className="h-3 w-3 text-signal-green" /> : <Copy className="h-3 w-3" />}
          </button>
        </TooltipTrigger>
        <TooltipContent>{copiedSection === sectionId ? `Copied ${title}` : `Copy ${title} to clipboard`}</TooltipContent>
      </Tooltip>
    </div>
  )
}

export function CodeFixLiteralBlock({
  title,
  content,
  className,
}: {
  title: string
  content: string
  className: string
}) {
  const unescaped = unescapeCodeContent(content)
  return (
    <div className="mt-2">
      <p className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">{title}</p>
      <pre className={`overflow-x-auto max-h-96 overflow-y-auto rounded bg-surface-elevated p-2 text-xs font-mono whitespace-pre-wrap ${className}`}>
        {unescaped}
      </pre>
    </div>
  )
}

interface FailureCardProps {
  group: GroupedFailure
  jobId: string
  childJobName?: string
  childBuildNumber?: number
  index: number
  /** Hash fragment (without #) from the URL, used for auto-expand & scroll-to on load. */
  activeHash?: string
}

export function FailureCard({ group, jobId, childJobName, childBuildNumber, index, activeHash }: FailureCardProps) {
  const scopedChildJobName = childJobName ?? ''
  const scopedChildBuildNumber = childBuildNumber ?? 0
  const { githubIssuesEnabled, jiraIssuesEnabled, serverJiraProjectKey, comments, reviews, aiModels, result, classifications, trackedIn } = useReportState()
  const dispatch = useReportDispatch()
  const { role, isOperator } = useAuth()
  const isViewer = role === 'viewer'
  const expandKey = `rootcoz-expand-${jobId}-${scopedChildJobName}-${scopedChildBuildNumber}-${group.id}`
  const [expanded, setExpanded] = useSessionState<boolean>(expandKey, false)
  const cardRef = useRef<HTMLDivElement>(null)
  const expandedByHashRef = useRef(false)

  // Auto-expand and scroll when URL hash targets this failure group
  useEffect(() => {
    if (activeHash === group.id && !expandedByHashRef.current) {
      setExpanded(true)
      expandedByHashRef.current = true
      cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    if (activeHash !== group.id) {
      expandedByHashRef.current = false
    }
  }, [activeHash, group.id])
  const [bugTarget, setBugTarget] = useState<'github' | 'jira' | null>(null)
  const [trackInOpen, setTrackInOpen] = useState(false)
  const [reviewingAll, setReviewingAll] = useState(false)
  const [reviewAllError, setReviewAllError] = useState<string | null>(null)
  const [selectedProvider, setSelectedProvider] = useState(result?.ai_provider ?? '')
  const [selectedModel, setSelectedModel] = useState(result?.ai_model ?? '')
  const [includeLinks, setIncludeLinks] = useState(false)
  const [reAnalyzeOpen, setReAnalyzeOpen] = useState(false)
  const { copiedKey: copiedSection, copy: copyToClipboard } = useClipboard()

  const rep = group.tests[0]
  const analysis = rep.analysis

  const { showSuggestion: showBugReviewSuggestion, loading: bugReviewLoading, error: bugReviewError, maybeSuggest: maybeSuggestBugReview, dismissSuggestion: dismissBugReviewSuggestion, confirmSuggestion: confirmBugReviewSuggestion } = useReviewSuggestion({
    jobId,
    testName: rep.test_name,
    childJobName,
    childBuildNumber,
  })

  const repoUrls = useMemo<RepoUrl[]>(
    () => buildRepoUrls(result?.request_params),
    [result?.request_params],
  )

  const providers = Object.keys(aiModels)
  const models = (aiModels[selectedProvider] ?? []).map((m) => m.id)

  function handleProviderChange(provider: string) {
    setSelectedProvider(provider)
    const providerModelIds = (aiModels[provider] ?? []).map((m) => m.id)
    if (providerModelIds.length === 0) {
      setSelectedModel('')
    } else if (!providerModelIds.includes(selectedModel)) {
      setSelectedModel(providerModelIds[0])
    }
  }

  const scopedReviewKey = (testName: string) =>
    reviewKey(testName, scopedChildJobName, scopedChildBuildNumber)

  const repKey = scopedReviewKey(rep.test_name)
  const classification = classifications[repKey] ?? analysis.classification
  const pattern = analysis.pattern || ''
  const borderColor = classification === 'PRODUCT BUG' ? 'border-l-signal-orange' : 'border-l-signal-blue'
  const showAiSelector = providers.length > 0 || models.length > 0

  // Comment count for ALL tests in the group
  const groupTestNames = group.tests.map((t) => t.test_name)
  const commentsInScope = useMemo(
    () => comments.filter((c) => isCommentInScope(c, groupTestNames, scopedChildJobName, scopedChildBuildNumber)),
    [comments, groupTestNames, scopedChildJobName, scopedChildBuildNumber],
  )
  const commentCount = commentsInScope.length

  // Auto-expand card when navigating to a specific comment (e.g. from Mentions page)
  const location = useLocation()
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const targetCommentId = params.get('comment')
    if (!targetCommentId || expanded) return
    if (commentsInScope.some((c) => String(c.id) === targetCommentId)) {
      setExpanded(true)
    }
  }, [location.search])

  // Review-all: check how many tests in group are reviewed
  const reviewedCount = group.tests.filter((t) => {
    const key = scopedReviewKey(t.test_name)
    return reviews[key]?.reviewed
  }).length
  const allReviewed = reviewedCount === group.tests.length

  async function handleReviewAll() {
    setReviewingAll(true)
    setReviewAllError(null)
    const newState = !allReviewed
    const BATCH_SIZE = 5
    try {
      let failedCount = 0
      const failedTestNames: string[] = []
      for (let batchStart = 0; batchStart < group.tests.length; batchStart += BATCH_SIZE) {
        const batch = group.tests.slice(batchStart, batchStart + BATCH_SIZE)
        const results = await Promise.allSettled(
          batch.map((t) =>
            api.put<{ status: string; reviewed_by: string }>(`/results/${jobId}/reviewed`, {
              test_name: t.test_name,
              reviewed: newState,
              child_job_name: scopedChildJobName,
              child_build_number: scopedChildBuildNumber,
            }).then((res) => ({ test: t, reviewed_by: res.reviewed_by })),
          ),
        )
        for (let i = 0; i < results.length; i++) {
          const result = results[i]
          if (result.status === 'fulfilled') {
            const { test: t, reviewed_by } = result.value
            const key = scopedReviewKey(t.test_name)
            dispatch({
              type: 'SET_REVIEW',
              payload: { key, state: { reviewed: newState, updated_at: new Date().toISOString(), username: reviewed_by ?? getUsername() } },
            })
          } else {
            failedCount++
            failedTestNames.push(batch[i].test_name)
          }
        }
      }
      if (failedCount > 0) {
        setReviewAllError(`Failed to update ${failedCount} of ${group.tests.length} tests: ${failedTestNames.join(', ')}`)
      }

      // Notify AllReviewedPrompt to check if all failures are now reviewed
      setTimeout(() => window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId } })), 100)
    } finally {
      setReviewingAll(false)
    }
  }

  return (
    <>
      <Card
        ref={cardRef}
        id={group.id}
        className={`border-l-4 ${borderColor} animate-slide-up scroll-mt-24${activeHash === group.id ? ' ring-2 ring-accent-blue/50' : ''}`}
        style={{ animationDelay: `${index * 50}ms`, animationFillMode: 'backwards' }}
      >
        {/* Header */}
        <div className="flex w-full items-center gap-3 p-4">
          <button
            className="flex min-w-0 flex-1 items-center gap-3 text-left"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
          >
            {expanded ? <ChevronDown className="h-4 w-4 shrink-0 text-text-tertiary" /> : <ChevronRight className="h-4 w-4 shrink-0 text-text-tertiary" />}
            <div className="min-w-0 flex-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <p className="truncate font-display text-sm font-medium text-text-primary">{rep.test_name}</p>
                </TooltipTrigger>
                <TooltipContent className="max-w-md break-all">{rep.test_name}</TooltipContent>
              </Tooltip>
              {group.count > 1 && <span className="text-xs text-text-tertiary">+{group.count - 1} more with same error</span>}
            </div>
          </button>
          <div className="flex items-center gap-1.5 shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="text-text-tertiary hover:text-text-primary transition-colors"
                  onClick={(e) => { e.stopPropagation(); copyToClipboard(rep.test_name, 'test-name') }}
                  aria-label={copiedSection === 'test-name' ? 'Copied test name' : 'Copy test name to clipboard'}
                >
                  {copiedSection === 'test-name' ? <Check className="h-3 w-3 text-signal-green" /> : <Copy className="h-3 w-3" />}
                </button>
              </TooltipTrigger>
              <TooltipContent>{copiedSection === 'test-name' ? 'Copied test name' : 'Copy test name to clipboard'}</TooltipContent>
            </Tooltip>
            <UuidCopyButton uuid={rep.id} sectionKey="uuid" copiedSection={copiedSection} onCopy={copyToClipboard} />
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {rep.reanalysis_status === 'running' && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center gap-1 rounded-md bg-accent-blue/15 px-2 py-1 text-[10px] font-mono text-accent-blue animate-pulse">
                    <RotateCw className="h-3 w-3 animate-spin" />
                    Re-analyzing
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  Re-analyzing using {rep.reanalyzed_with?.ai_provider ?? 'unknown'}{rep.reanalyzed_with?.ai_model ? ` / ${rep.reanalyzed_with.ai_model}` : ''}...
                </TooltipContent>
              </Tooltip>
            )}
            {rep.reanalysis_status === 'failed' && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center gap-1 rounded-md bg-signal-red/15 px-2 py-1 text-[10px] font-mono text-signal-red">
                    Re-analysis failed
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-md break-all">{rep.reanalysis_error || 'Unknown error'}</TooltipContent>
              </Tooltip>
            )}
            <ClassificationBadge classification={classification} />
            {pattern && (
              <>
                <span className="text-text-tertiary text-[10px]">·</span>
                <ClassificationBadge classification={pattern} />
              </>
            )}
            {trackedIn[rep.test_name] && trackedIn[rep.test_name].tracked_in_url ? (
              <TrackedInBadge url={trackedIn[rep.test_name].tracked_in_url} type={trackedIn[rep.test_name].tracked_in_type} />
            ) : !isViewer ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    onClick={(e) => { e.stopPropagation(); setTrackInOpen(true) }}
                    className="flex items-center gap-1 rounded-md bg-surface-elevated px-2 py-1 text-[10px] font-mono text-text-tertiary hover:text-text-secondary hover:bg-surface-elevated/80 transition-colors"
                  >
                    <Link2 className="h-3 w-3" />
                    Track
                  </button>
                </TooltipTrigger>
                <TooltipContent>Link to an existing issue</TooltipContent>
              </Tooltip>
            ) : null}
            {rep.reanalyzed_with && rep.reanalysis_status !== 'running' && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="rounded-md bg-surface-elevated px-2 py-1 text-[10px] font-mono text-text-tertiary cursor-default">
                    {rep.reanalyzed_with.ai_provider}{rep.reanalyzed_with.ai_model ? ` / ${rep.reanalyzed_with.ai_model}` : ''}
                  </span>
                </TooltipTrigger>
                <TooltipContent>Re-analyzed with different AI</TooltipContent>
              </Tooltip>
            )}
            {(() => {
              const secondaryBadges = new Set<string>()
              for (const t of group.tests) {
                const key = scopedReviewKey(t.test_name)
                const cls = classifications[key]
                if (cls && cls !== classification) secondaryBadges.add(cls)
              }
              return [...secondaryBadges].map((cls) => (
                <ClassificationBadge key={cls} classification={cls} />
              ))
            })()}
            {group.count === 1 ? (
              <ReviewToggle jobId={jobId} testName={rep.test_name} childJobName={scopedChildJobName} childBuildNumber={scopedChildBuildNumber} />
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleReviewAll() }}
                    disabled={reviewingAll}
                    className={`flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-mono transition-colors ${
                      allReviewed
                        ? 'bg-signal-green/15 text-signal-green'
                        : 'bg-surface-elevated text-text-tertiary hover:text-text-secondary'
                    }`}
                  >
                    <CheckCircle2 className="h-3 w-3" />
                    {allReviewed ? 'Reviewed' : `Review ${reviewedCount}/${group.count}`}
                  </button>
                </TooltipTrigger>
                <TooltipContent>{allReviewed ? 'All reviewed' : `Review all ${group.count} tests`}</TooltipContent>
              </Tooltip>
            )}
            {commentCount > 0 && (
              <span className="flex items-center gap-1 rounded-md bg-surface-elevated px-2 py-1 text-[10px] font-mono text-text-tertiary">
                <MessageSquare className="h-3 w-3" />
                {commentCount}
              </span>
            )}
          </div>
        </div>

        {/* Expanded body */}
        {expanded && (
          <CardContent className="space-y-4 border-t border-border-muted pt-4">
            {/* Review-all toggle for groups */}
            {group.count > 1 && (
              <div className="flex items-center gap-2">
                <button
                  onClick={handleReviewAll}
                  disabled={reviewingAll}
                  className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-xs font-bold transition-colors ${
                    allReviewed
                      ? 'bg-signal-green/15 text-signal-green'
                      : 'bg-surface-elevated text-text-tertiary hover:text-text-secondary'
                  }`}
                >
                  <CheckCircle2 className="h-4 w-4" />
                  {allReviewed ? 'All Reviewed' : `Review All (${reviewedCount}/${group.count})`}
                </button>
                {reviewAllError && <span role="alert" className="text-signal-red text-xs">{reviewAllError}</span>}
              </div>
            )}

            {/* Affected tests list */}
            {group.count > 1 && (
              <div>
                <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-2">Affected Tests ({group.count})</h4>
                <div className="space-y-1">
                  {group.tests.map((t) => (
                    <div key={t.test_name} className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <p className="font-mono text-xs text-text-secondary truncate">{t.test_name}</p>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-md break-all">{t.test_name}</TooltipContent>
                        </Tooltip>
                        <UuidCopyButton uuid={t.id} sectionKey={`uuid-${t.id}`} copiedSection={copiedSection} onCopy={copyToClipboard} />
                      </div>
                      <ReviewToggle jobId={jobId} testName={t.test_name} childJobName={scopedChildJobName} childBuildNumber={scopedChildBuildNumber} disabled={reviewingAll} />
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Error */}
            <div>
              <CopyableSectionHeader title="Error" content={rep.error} sectionId="error" copiedSection={copiedSection} onCopy={copyToClipboard} />
              <pre className="overflow-x-auto rounded-md bg-signal-red/5 border border-signal-red/20 p-3 text-xs text-signal-red font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                {rep.error}
              </pre>
            </div>

            {/* Analysis */}
            {analysis.details && (
              <div>
                <CopyableSectionHeader
                  title="Analysis"
                  content={analysis.details}
                  sectionId="analysis"
                  copiedSection={copiedSection}
                  onCopy={copyToClipboard}

                />
                <div className="rounded-md bg-glow-blue p-3 text-sm text-text-secondary whitespace-pre-wrap"><LinkedText text={analysis.details} repoUrls={repoUrls} /></div>
              </div>
            )}

            {/* Artifacts evidence */}
            {analysis.artifacts_evidence && (
              <div>
                <CopyableSectionHeader title="Artifacts Evidence" content={analysis.artifacts_evidence} sectionId="artifacts_evidence" copiedSection={copiedSection} onCopy={copyToClipboard} />
                <div className="overflow-x-auto rounded-md bg-surface-elevated p-3 text-xs text-text-secondary font-mono whitespace-pre-wrap max-h-64 overflow-y-auto">
                  <LinkedText text={analysis.artifacts_evidence} repoUrls={repoUrls} />
                </div>
              </div>
            )}

            {/* Code fix */}
            {classification !== 'PRODUCT BUG' && analysis.code_fix && typeof analysis.code_fix === 'object' && (
              <div>
                <CopyableSectionHeader
                  title="Suggested Fix"
                  content={[
                    analysis.code_fix?.file ? `${analysis.code_fix.file}${analysis.code_fix.line ? `:${analysis.code_fix.line}` : ''}` : '',
                    analysis.code_fix?.change ?? '',
                    analysis.code_fix.original_code != null ? `Original Code:\n${unescapeCodeContent(analysis.code_fix.original_code)}` : '',
                    analysis.code_fix.suggested_code != null ? `Suggested Code:\n${unescapeCodeContent(analysis.code_fix.suggested_code)}` : '',
                  ].filter(Boolean).join('\n\n')}
                  sectionId="suggested_fix"
                  copiedSection={copiedSection}
                  onCopy={copyToClipboard}
                />
                <div className="rounded-md bg-glow-green border border-signal-green/20 p-3 text-sm">
                  {analysis.code_fix.file && (
                    <p className="font-mono text-xs text-signal-green">
                      {repoUrls.length > 0 ? (() => {
                        const { repo, prefixMatched } = matchRepo(analysis.code_fix.file, repoUrls)
                        const relPath = repo && prefixMatched ? analysis.code_fix.file.slice(repo.name.length + 1) : analysis.code_fix.file
                        const href = repo ? buildFileUrl(repo.url, relPath, analysis.code_fix.line, repo.ref) : ''
                        const canLink = !!repo && isSafeHref(href)
                        return canLink ? (
                          <a
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-signal-green hover:underline"
                          >
                            {analysis.code_fix.file}{analysis.code_fix.line && `:${analysis.code_fix.line}`}
                          </a>
                        ) : (
                          <>{analysis.code_fix.file}{analysis.code_fix.line && `:${analysis.code_fix.line}`}</>
                        )
                      })() : (
                        <>{analysis.code_fix.file}{analysis.code_fix.line && `:${analysis.code_fix.line}`}</>
                      )}
                    </p>
                  )}
                  {analysis.code_fix.change && <p className="mt-1 text-text-secondary whitespace-pre-wrap"><LinkedText text={analysis.code_fix.change} repoUrls={repoUrls} /></p>}
                  {analysis.code_fix.original_code != null && (
                    <CodeFixLiteralBlock title="Original Code" content={analysis.code_fix.original_code} className="text-text-secondary" />
                  )}
                  {analysis.code_fix.suggested_code != null && (
                    <CodeFixLiteralBlock title="Suggested Code" content={analysis.code_fix.suggested_code} className="text-signal-green" />
                  )}
                </div>
              </div>
            )}

            {/* Product bug report */}
            {classification === 'PRODUCT BUG' && analysis.product_bug_report && typeof analysis.product_bug_report === 'object' && (
              <div>
                <h4 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-2">Bug Report</h4>
                <div className="rounded-md bg-glow-orange border border-signal-orange/20 p-3 text-sm space-y-2">
                  {analysis.product_bug_report.title && <p className="font-medium text-text-primary">{analysis.product_bug_report.title}</p>}
                  {analysis.product_bug_report.severity && <Badge variant="warning" className="text-[10px]">{analysis.product_bug_report.severity}</Badge>}
                  {analysis.product_bug_report.description && <p className="text-text-secondary whitespace-pre-wrap"><LinkedText text={analysis.product_bug_report.description} repoUrls={repoUrls} /></p>}
                  {analysis.product_bug_report?.jira_matches?.length > 0 && (
                    <div className="mt-2">
                      <p className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">Matching Jira Issues</p>
                      <ul className="space-y-1">
                        {analysis.product_bug_report.jira_matches.map((m) => (
                          <li key={m.key} className="flex items-center gap-2 text-xs">
                            <a href={m.url} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
                              {m.key}: {m.summary}
                            </a>
                            {m.status && <Badge variant="outline" className="text-[10px]">{m.status}</Badge>}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Previous Analyses (from re-analysis) */}
            {(() => {
              const prevList: PreviousAnalysis[] = rep.previous_analyses && rep.previous_analyses.length > 0
                ? rep.previous_analyses
                : rep.previous_analysis
                  ? [rep.previous_analysis]
                  : []
              return prevList.length > 0 && (
                <div className="space-y-2">
                  {prevList.map((prev, idx) => (
                    <PreviousAnalysisEntry key={idx} prev={prev} index={idx} repoUrls={repoUrls} />
                  ))}
                </div>
              )
            })()}

            {/* Peer debate trail */}
            {rep.peer_debate && <PeerDebateSection debate={rep.peer_debate} repoUrls={repoUrls} />}

            {/* Actions: classification + AI selector + bug creation */}
            <div className="flex flex-wrap items-center gap-3 pt-2 border-t border-border-muted">
              <ClassificationSelect
                jobId={jobId}
                testName={rep.test_name}
                testNames={groupTestNames}
                currentClassification={classification}
                childJobName={scopedChildJobName}
                childBuildNumber={scopedChildBuildNumber}
              />
              <PatternSelect
                jobId={jobId}
                testName={rep.test_name}
                testNames={groupTestNames}
                currentPattern={pattern}
                childJobName={scopedChildJobName}
                childBuildNumber={scopedChildBuildNumber}
              />
              {showAiSelector && (
                <>
                  <span className="text-xs text-text-tertiary whitespace-nowrap">AI for issue generation:</span>
                  <div className="flex items-center gap-2">
                    <Input
                      list={`provider-options-${group.id}`}
                      value={selectedProvider}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      placeholder="provider"
                      aria-label="AI provider"
                      className="h-7 border bg-surface-card px-2 text-xs text-text-primary w-24"
                    />
                    <datalist id={`provider-options-${group.id}`}>
                      {providers.map((p) => (
                        <option key={p} value={p} />
                      ))}
                    </datalist>

                    <Input
                      list={`model-options-${group.id}`}
                      value={selectedModel}
                      onChange={(e) => setSelectedModel(e.target.value)}
                      placeholder="model"
                      aria-label="AI model"
                      className="h-7 border bg-surface-card px-2 text-xs text-text-primary w-44"
                    />
                    <datalist id={`model-options-${group.id}`}>
                      {models.map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>
                  </div>
                </>
              )}
              {(showAiSelector || githubIssuesEnabled || jiraIssuesEnabled) && (
                <label className="flex items-center gap-1.5 text-xs text-text-secondary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={includeLinks}
                    onChange={(e) => setIncludeLinks(e.target.checked)}
                    className="rounded border-border-default"
                  />
                  Include links
                </label>
              )}
              {isOperator && (
                <Button variant="outline" size="sm" onClick={() => setReAnalyzeOpen(true)} disabled={rep.reanalysis_status === 'running'}>
                  <RotateCw className={`h-3.5 w-3.5 mr-1${rep.reanalysis_status === 'running' ? ' animate-spin' : ''}`} />
                  Re-analyze
                </Button>
              )}
              <IssueButton
                disabled={!githubIssuesEnabled}
                tooltip={!githubIssuesEnabled ? 'GitHub issues are disabled on this server' : undefined}
                label="GitHub Issue"
                onClick={() => setBugTarget('github')}
              />
              <IssueButton
                disabled={!jiraIssuesEnabled}
                tooltip={!jiraIssuesEnabled ? 'Jira issues are disabled on this server' : undefined}
                label="Jira Ticket"
                onClick={() => setBugTarget('jira')}
              />
            </div>

            {/* Comments */}
            <CommentsSection jobId={jobId} testNames={groupTestNames} childJobName={scopedChildJobName} childBuildNumber={scopedChildBuildNumber} />
          </CardContent>
        )}
      </Card>

      {/* For grouped failures, the dialog creates an issue for the representative test (first in group) */}
      {bugTarget && (
        <BugCreationDialog
          open={bugTarget !== null}
          onOpenChange={(o) => { if (!o) setBugTarget(null) }}
          jobId={jobId}
          testName={rep.test_name}
          includeLinks={includeLinks}
          target={bugTarget}
          childJobName={scopedChildJobName}
          childBuildNumber={scopedChildBuildNumber}
          aiProvider={selectedProvider}
          aiModel={selectedModel}
          defaultProjectKey={serverJiraProjectKey}
          availableRepos={
            repoUrls.length > 0
              ? repoUrls.map(({ name, url }) => ({ name, url }))
              : undefined
          }
          onIssueCreated={(url) => {
            // Auto-set tracked-in in local state (backend already persisted it)
            const trackedType = url.includes('github.com') ? 'github' : url.includes('jira') || url.includes('atlassian') ? 'jira' : ''
            dispatch({ type: 'SET_TRACKED_IN_ENTRY', payload: { testName: rep.test_name, entry: { tracked_in_url: url, tracked_in_type: trackedType } } })
            void maybeSuggestBugReview(url)
          }}
        />
      )}

      <TrackInDialog
        open={trackInOpen}
        onOpenChange={setTrackInOpen}
        jobId={jobId}
        testName={rep.test_name}
      />

      <ConfirmDialog
        open={showBugReviewSuggestion}
        onOpenChange={(open) => { if (!open) dismissBugReviewSuggestion() }}
        title="Mark as reviewed?"
        description="A bug issue was linked to this failure. Would you like to mark it as reviewed?"
        confirmLabel="Yes"
        cancelLabel="No"
        onConfirm={confirmBugReviewSuggestion}
        loading={bugReviewLoading}
      />
      {bugReviewError && (
        <span className="text-sm text-destructive" role="alert">{bugReviewError}</span>
      )}
      {result && (
        <ReAnalyzeDialog
          open={reAnalyzeOpen}
          onOpenChange={setReAnalyzeOpen}
          result={result}
          jobId={jobId}
          failureUuid={rep.id}
        />
      )}
    </>
  )
}
