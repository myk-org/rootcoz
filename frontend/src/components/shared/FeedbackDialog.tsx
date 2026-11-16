import { useState, useRef, useEffect } from 'react'
import { api, getRecentFailedCalls } from '@/lib/api'
import { createIssueFailureMessage, createOutcomeUnknown } from '@/lib/issueCreateError'
import { getRecentErrors } from '@/lib/errorCapture'
import { IssueCreateError } from '@/components/shared/IssueCreateError'
import { AttachmentPicker, IssueAttachmentDeliverySummary } from '@/components/shared/AttachmentPicker'
import {
  attachmentPickerPropsFromCapability,
  filesToAttachmentPayload,
  useCapabilities,
} from '@/lib/attachments'
import { useSubmitGuard } from '@/lib/useSubmitGuard'
import { useUserTokenPresence } from '@/lib/useUserTokens'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { LoadingSpinner } from '@/components/shared/LoadingSpinner'
import { TokenRequiredBanner } from '@/components/shared/TokenRequiredBanner'
import { AnalysisProviderSelect, AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { CheckCircle2, ExternalLink } from 'lucide-react'
import { completeAiPairOverride } from '@/lib/analysisAi'
import { useAiSelection } from '@/lib/useAiSelection'
import type {
  Capabilities,
  FeedbackRequest,
  FeedbackPreviewResponse,
  FeedbackCreateRequest,
  FeedbackCreateResponse,
  IssueAttachmentSummary,
  IssueAttachmentUpload,
} from '@/types'

type Phase = 'form' | 'previewing' | 'preview' | 'creating' | 'success' | 'error' | 'ai-not-configured'

interface FeedbackDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  capabilities?: Capabilities | null
}

export function FeedbackDialog({ open, onOpenChange, capabilities }: FeedbackDialogProps) {
  const [description, setDescription] = useState('')
  // Empty = server default (resolved server-side). Shared with the other AI surfaces.
  const { aiProvider, aiModel, setAiPair } = useAiSelection()
  const [phase, setPhase] = useState<Phase>('form')
  const [issueUrl, setIssueUrl] = useState('')
  const [attachments, setAttachments] = useState<File[]>([])
  const [attachmentSummary, setAttachmentSummary] = useState<IssueAttachmentSummary | null>(null)
  const [errorMsg, setErrorMsg] = useState('')
  // A failed create keeps the generated issue in the preview so it can be retried
  // as-is; only a failed preview sends the user back to the form.
  const [createError, setCreateError] = useState('')
  const [createMaybeFiled, setCreateMaybeFiled] = useState(false)
  const guardSubmit = useSubmitGuard()
  const generationRef = useRef(0)
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Preview state
  const [previewTitle, setPreviewTitle] = useState('')
  const [previewBody, setPreviewBody] = useState('')
  const [previewLabels, setPreviewLabels] = useState<string[]>([])
  // Server-RESOLVED pair for attribution — never the values this dialog sent.
  const [previewAi, setPreviewAi] = useState({ provider: '', model: '', generated: false })
  // The account row is the only credential source, so ask it when the dialog opens
  // — the same row the server reads when it files the issue.
  const tokenPresence = useUserTokenPresence(open)
  const hasGithubToken = tokenPresence.github
  // Only a read that ANSWERED "no token" is a missing credential: while it is in
  // flight the row is unknown, so no false warning and no blocked Create — the
  // create call's own refusal names the fix.
  const tokenMissing = tokenPresence.loaded && !hasGithubToken
  const capabilityState = useCapabilities(
    open,
    capabilities !== undefined ? { loaded: true, capabilities } : undefined,
  )
  const pickerProps = attachmentPickerPropsFromCapability(
    capabilityState.capabilities,
    capabilityState.loaded,
    'feedback',
  )
  const uncertainAttachmentLock = createMaybeFiled && attachments.length > 0

  // Track dialog open/close to guard async setState and clear session attachments
  useEffect(() => {
    if (open) {
      generationRef.current += 1
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current)
        resetTimerRef.current = null
      }
      return () => {
        setAttachments((prev) => (prev.length > 0 ? [] : prev))
        setAttachmentSummary(null)
      }
    } else {
      generationRef.current += 1
    }
  }, [open])

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current)
      }
    }
  }, [])

  function collectPageState(): FeedbackRequest['page_state'] {
    const state: FeedbackRequest['page_state'] = {}
    // Extract report_id from URL if on a report page
    const reportMatch = window.location.pathname.match(/\/results\/([^/]+)/)
    if (reportMatch) {
      state.report_id = reportMatch[1]
    }
    // Capture active filters from URL search params
    const params = new URLSearchParams(window.location.search)
    const filters = params.toString()
    if (filters) {
      state.active_filters = filters
    }
    return state
  }

  async function handlePreview() {
    if (!description.trim()) return

    const gen = generationRef.current
    // A create error belongs to the issue it failed on; a new preview must not
    // show it (or label its own button "Retry").
    setCreateError('')
    setCreateMaybeFiled(false)
    setPhase('previewing')
    try {
      const failedCalls = getRecentFailedCalls()
      const payload: FeedbackRequest = {
        description: description.trim(),
        console_errors: getRecentErrors(),
        failed_api_calls: failedCalls.map(({ status, endpoint, error }) => ({
          status,
          endpoint,
          error,
        })),
        page_state: collectPageState(),
        user_agent: navigator.userAgent,
        // Incomplete selection = no selection; the server resolves the pair.
        ...completeAiPairOverride(aiProvider, aiModel),
      }

      const res = await api.post<FeedbackPreviewResponse>('/api/feedback/preview', payload)
      if (gen !== generationRef.current) return
      setPreviewTitle(res.title)
      setPreviewBody(res.body)
      setPreviewLabels(res.labels)
      setPreviewAi({ provider: res.ai_provider ?? '', model: res.ai_model ?? '', generated: res.ai_generated === true })
      setPhase('preview')
    } catch (err) {
      if (gen !== generationRef.current) return
      const msg = err instanceof Error ? err.message : 'Failed to generate preview'
      if (/ai\s*(is)?\s*not\s*configured/i.test(msg)) {
        setPhase('ai-not-configured')
      } else {
        setErrorMsg(msg)
        setPhase('error')
      }
    }
  }

  async function handleCreate() {
    await guardSubmit(async () => {
      const gen = generationRef.current
      setCreateError('')
      setCreateMaybeFiled(false)
      setPhase('creating')

      let encodedAttachments: IssueAttachmentUpload[] = []
      if (attachments.length > 0) {
        try {
          encodedAttachments = await filesToAttachmentPayload(attachments, pickerProps)
        } catch (err) {
          if (gen !== generationRef.current) return
          setCreateError(err instanceof Error ? err.message : 'Failed to read selected attachment.')
          setCreateMaybeFiled(false)
          setPhase('preview')
          return
        }
        if (gen !== generationRef.current) return
      }

      try {
        const payload: FeedbackCreateRequest = {
          title: previewTitle,
          body: previewBody,
          labels: previewLabels,
          ...(encodedAttachments.length > 0 ? { attachments: encodedAttachments } : {}),
        }

        const res = await api.post<FeedbackCreateResponse>('/api/feedback/create', payload)
        if (gen !== generationRef.current) return
        setIssueUrl(res.issue_url)
        setAttachmentSummary(res.attachments ?? null)
        setAttachments([])
        setPhase('success')
      } catch (err) {
        if (gen !== generationRef.current) return
        setCreateError(createIssueFailureMessage(err))
        setCreateMaybeFiled(createOutcomeUnknown(err))
        setPhase('preview')
      }
    })
  }

  function handleBack() {
    setPhase('form')
  }

  function handleClose(nextOpen: boolean) {
    if (nextOpen) return
    onOpenChange(false)
    if (resetTimerRef.current) {
      clearTimeout(resetTimerRef.current)
    }
    resetTimerRef.current = setTimeout(() => {
      resetTimerRef.current = null
      setPhase('form')
      setDescription('')
      setIssueUrl('')
      setAttachments([])
      setAttachmentSummary(null)
      setErrorMsg('')
      setCreateError('')
      setCreateMaybeFiled(false)
      setPreviewTitle('')
      setPreviewBody('')
      setPreviewLabels([])
      setPreviewAi({ provider: '', model: '', generated: false })
    }, 200)
  }

  const dialogTitle =
    phase === 'success'
      ? 'Feedback Submitted'
      : phase === 'preview' || phase === 'creating'
        ? 'Preview Issue'
        : 'Send Feedback'

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => {
      if (!nextOpen && (phase === 'previewing' || phase === 'creating' || phase === 'preview')) return
      handleClose(nextOpen)
    }}>
      <DialogContent className="max-w-lg" hideCloseButton={phase === 'preview' || phase === 'previewing' || phase === 'creating'}>
        <DialogHeader>
          <DialogTitle>{dialogTitle}</DialogTitle>
          {phase === 'form' && (
            <DialogDescription>
              Describe your issue or idea. Browser context is attached automatically.
            </DialogDescription>
          )}
          {phase === 'preview' && (
            <DialogDescription>
              Review and edit the generated issue before creating it.
            </DialogDescription>
          )}
        </DialogHeader>

        {/* Phase 1: Form */}
        {phase === 'form' && (
          <div className="space-y-4">
            {/* AI for issue generation */}
            <div className="space-y-2">
              <span className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                AI for issue generation
              </span>
              <div className="grid grid-cols-2 gap-2">
                <AnalysisProviderSelect
                  value={aiProvider}
                  onChange={(v) => setAiPair(v, '')}
                  forceServer={false}
                  label="AI Provider for feedback"
                  allowDefault
                />
                <AnalysisModelSelect
                  provider={aiProvider}
                  value={aiModel}
                  onChange={(model) => setAiPair(aiProvider, model, { commit: false })}
                  onCommit={(model) => setAiPair(aiProvider, model)}
                  forceServer={false}
                  label="AI Model for feedback"
                />
              </div>
              <p className="text-xs text-text-tertiary">
                Leave unset to use this server&rsquo;s default AI.
              </p>
            </div>

            {/* Description */}
            <div className="space-y-2">
              <label htmlFor="feedback-description" className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                Description
              </label>
              <Textarea
                id="feedback-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe your issue or suggestion..."
                rows={6}
              />
            </div>

            <p className="text-xs text-text-tertiary">
              Console errors, recent failed API calls, and page context will be included automatically.
            </p>
          </div>
        )}

        {/* Previewing (loading) */}
        {phase === 'previewing' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Generating preview...</p>
          </div>
        )}

        {/* Phase 2: Preview */}
        {phase === 'preview' && (
          <div className="space-y-4">
            {createError && <IssueCreateError message={createError} />}
            {createError && createMaybeFiled && (
              <p role="status" className="text-xs text-signal-amber">
                The server could not confirm this attempt, so the issue may already
                have been filed. Check GitHub before you retry — never resubmit blindly,
                as submitting it again could create a duplicate.
                {attachments.length > 0 && ' Selected files remain listed below for inspection.'}
              </p>
            )}
            <div className="rounded-md border border-border-muted bg-surface-elevated px-3 py-2 text-xs text-text-secondary">
              {previewAi.generated
                ? `Generated using AI with ${previewAi.provider}/${previewAi.model}`
                : 'No AI model generated this issue — the fallback template was used.'}
            </div>
            <div className="space-y-2">
              <label htmlFor="preview-title" className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                Title
              </label>
              <Input
                id="preview-title"
                value={previewTitle}
                onChange={(e) => setPreviewTitle(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="preview-body" className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                Body
              </label>
              <Textarea
                id="preview-body"
                value={previewBody}
                onChange={(e) => setPreviewBody(e.target.value)}
                rows={10}
              />
            </div>
            <div className="space-y-2">
              <span className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                Attachments
              </span>
              <AttachmentPicker
                files={attachments}
                onChange={setAttachments}
                maxFiles={pickerProps.maxFiles}
                maxFileBytes={pickerProps.maxFileBytes}
                maxTotalBytes={pickerProps.maxTotalBytes}
                disabledReason={
                  uncertainAttachmentLock
                    ? 'Issue creation outcome is uncertain — check GitHub instead of resubmitting.'
                    : pickerProps.disabledReason
                }
                hint={uncertainAttachmentLock ? undefined : pickerProps.hint}
              />
            </div>
          </div>
        )}

        {/* Creating (loading) */}
        {phase === 'creating' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Creating issue...</p>
          </div>
        )}

        {/* Phase 3: Success */}
        {phase === 'success' && (
          <div className="flex flex-col items-center gap-4 py-8 animate-scale-in">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-signal-green/15">
              <CheckCircle2 className="h-8 w-8 text-signal-green" />
            </div>
            <p className="text-sm text-text-secondary">Thank you for your feedback!</p>
            {issueUrl && (
              <a
                href={issueUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-sm text-text-link hover:underline"
              >
                View issue <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
            {attachmentSummary && <IssueAttachmentDeliverySummary summary={attachmentSummary} />}
          </div>
        )}

        {/* AI not configured */}
        {phase === 'ai-not-configured' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <p className="text-sm text-text-secondary">AI is not configured on this server. You can open an issue manually:</p>
            <a
              href="https://github.com/myk-org/rootcoz/issues/new"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-sm text-text-link hover:underline"
            >
              Open a new issue <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        )}

        {/* Error */}
        {phase === 'error' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <p className="text-sm text-signal-red">{errorMsg}</p>
            {errorMsg.toLowerCase().includes('token') && (
              <p className="text-xs text-text-tertiary">You can update your tokens in <a href="/settings" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Profile Settings</a>.</p>
            )}
          </div>
        )}

        {phase === 'preview' && tokenMissing && (
          <TokenRequiredBanner provider="GitHub" action="submit feedback" />
        )}

        <DialogFooter className="flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-end">
          {phase === 'form' && (
            <div className="flex gap-2 sm:ml-auto">
              <Button variant="outline" onClick={() => handleClose(false)}>Cancel</Button>
              <Button onClick={handlePreview} disabled={!description.trim()}>Preview</Button>
            </div>
          )}
          {phase === 'preview' && (
            <div className="flex gap-2 sm:ml-auto">
              <Button variant="outline" onClick={handleBack}>Back</Button>
              <Button variant="outline" onClick={() => handleClose(false)}>Cancel</Button>
              <Button
                onClick={handleCreate}
                disabled={!previewTitle.trim() || !previewBody.trim() || tokenMissing || uncertainAttachmentLock}
              >{createError ? 'Retry' : 'Create Issue'}</Button>
            </div>
          )}
          {phase === 'error' && (
            <div className="flex gap-2 sm:ml-auto">
              <Button variant="outline" onClick={() => handleClose(false)}>Close</Button>
              <Button onClick={() => setPhase('form')}>Try Again</Button>
            </div>
          )}
          {phase === 'ai-not-configured' && (
            <Button variant="outline" onClick={() => handleClose(false)} className="sm:ml-auto">Close</Button>
          )}
          {phase === 'success' && (
            <Button variant="outline" onClick={() => handleClose(false)} className="sm:ml-auto">Close</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
