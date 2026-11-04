import { useEffect, useRef, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
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
import { AlertTriangle, CheckCircle2, ExternalLink } from 'lucide-react'

type Tracker = 'github' | 'jira'

interface ChatIssueDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The chat answer the issue is drafted from. */
  message: string
}

type Draft = { title: string; body: string }
type Capabilities = {
  github_issues_enabled?: boolean
  jira_issues_enabled?: boolean
  /** The deployment's default Jira project; empty means the user must name one. */
  server_jira_project_key?: string
}
type Tokens = { github_token?: string; jira_token?: string; jira_sa_token?: string }

const NO_TRACKERS: Tracker[] = []

/**
 * Turn one chat answer into a tracker issue.
 *
 * The draft is scrubbed server-side before it is shown, so a redaction the user
 * disagrees with is visible and editable here, and the create request scrubs
 * again. The available trackers come from the account, not from browser
 * storage: a token saved in Profile Settings is usable on a fresh browser.
 */
export function ChatIssueDialog({ open, onOpenChange, message }: ChatIssueDialogProps) {
  const [draft, setDraft] = useState<Draft>({ title: '', body: '' })
  const [trackers, setTrackers] = useState<Tracker[]>(NO_TRACKERS)
  const [tracker, setTracker] = useState<Tracker>('github')
  const [projectKey, setProjectKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [createdUrl, setCreatedUrl] = useState('')
  // Bumped on every opening: a create from an earlier opening must not write here.
  const openEpoch = useRef(0)

  useEffect(() => {
    if (!open) return
    openEpoch.current += 1
    let ignore = false
    setLoading(true)
    setSubmitting(false)
    setError('')
    setCreatedUrl('')
    // Opening for another answer must not show what the previous answer drafted.
    setDraft({ title: '', body: '' })
    setTrackers(NO_TRACKERS)
    setProjectKey('')
    Promise.all([
      api.post<Draft>('/api/chat-issue/prefill', { text: message }),
      api.get<Capabilities>('/api/capabilities'),
      api.get<Tokens>('/api/user/tokens'),
    ])
      .then(([prefill, caps, tokens]) => {
        if (ignore) return
        const available: Tracker[] = []
        if (caps.github_issues_enabled !== false && !!tokens.github_token) available.push('github')
        if (caps.jira_issues_enabled !== false && (!!tokens.jira_token || !!tokens.jira_sa_token)) available.push('jira')
        setTrackers(available)
        setTracker(available[0] ?? 'github')
        // The server's default project, still editable: a Jira create without one is refused.
        setProjectKey(caps.server_jira_project_key ?? '')
        setDraft(prefill)
        setLoading(false)
      })
      .catch((err) => {
        if (ignore) return
        setError(extractApiDetail(err) ?? (err instanceof Error ? err.message : 'Failed to draft the issue'))
        setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [open, message])

  async function handleCreate() {
    const run = openEpoch.current
    setSubmitting(true)
    setError('')
    try {
      const res = await api.post<{ url: string }>('/api/chat-issue', {
        target: tracker,
        title: draft.title,
        body: draft.body,
        ...(tracker === 'jira' && projectKey.trim() ? { jira_project_key: projectKey.trim() } : {}),
      })
      if (run !== openEpoch.current) return
      setCreatedUrl(res.url)
    } catch (err) {
      if (run !== openEpoch.current) return
      // The server's 400/403 detail names exactly what to fix.
      setError(extractApiDetail(err) ?? (err instanceof Error ? err.message : 'Failed to create the issue'))
    } finally {
      // A reopening already reset this flag; do not clear the newer run's one.
      if (run === openEpoch.current) setSubmitting(false)
    }
  }

  const label = tracker === 'github' ? 'GitHub issue' : 'Jira ticket'
  const projectKeyMissing = tracker === 'jira' && !projectKey.trim()

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{createdUrl ? `${label} created` : `Create ${label} from chat`}</DialogTitle>
          {!createdUrl && !loading && trackers.length > 0 && (
            <DialogDescription>Review and edit before creating. Nothing is sent until you click Create.</DialogDescription>
          )}
        </DialogHeader>

        {loading && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Drafting the issue...</p>
          </div>
        )}

        {!loading && createdUrl && (
          <div className="space-y-4 py-2">
            <div className="flex items-center gap-2 text-signal-green">
              <CheckCircle2 className="h-4 w-4" />
              <span className="text-sm">{label} created</span>
            </div>
            <a
              href={createdUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-text-link hover:underline inline-flex items-center gap-1"
            >
              {createdUrl}
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        )}

        {!loading && !createdUrl && (
          <div className="space-y-4">
            {trackers.length === 0 ? (
              <div className="rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4 flex items-start gap-3">
                <AlertTriangle className="h-5 w-5 text-signal-orange shrink-0 mt-0.5" />
                <div className="flex flex-col gap-1">
                  <p className="text-sm font-medium text-text-primary">No tracker credential in your account</p>
                  <p className="text-xs text-text-secondary">
                    Add a GitHub or Jira token in Profile Settings to create issues from chat. The
                    bootstrap admin account has no profile to store one — sign in with your own
                    account to file issues.
                  </p>
                  <a
                    href="/settings"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-text-link hover:underline mt-1 inline-flex items-center gap-1"
                  >
                    Open Profile Settings →
                  </a>
                </div>
              </div>
            ) : (
              <>
                {trackers.length > 1 && (
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-display uppercase tracking-widest text-text-tertiary">Tracker</span>
                    {trackers.map((option) => (
                      <Button
                        key={option}
                        size="sm"
                        variant={option === tracker ? 'default' : 'ghost'}
                        className="h-7 px-3 text-xs"
                        disabled={submitting}
                        onClick={() => setTracker(option)}
                      >
                        {option === 'github' ? 'GitHub' : 'Jira'}
                      </Button>
                    ))}
                  </div>
                )}
                {tracker === 'jira' && (
                  <Input
                    value={projectKey}
                    onChange={(e) => setProjectKey(e.target.value)}
                    aria-label="Jira project key"
                    placeholder="Jira project key (e.g. MTV)"
                  />
                )}
                <Input
                  value={draft.title}
                  onChange={(e) => setDraft((prev) => ({ ...prev, title: e.target.value }))}
                  aria-label="Issue title"
                  placeholder="Issue title"
                />
                <Textarea
                  value={draft.body}
                  onChange={(e) => setDraft((prev) => ({ ...prev, body: e.target.value }))}
                  aria-label="Issue body"
                  rows={14}
                  className="min-h-[240px] resize-y"
                />
              </>
            )}

            {error && (
              <p className="text-xs text-signal-red" role="alert">
                {error}
              </p>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {createdUrl ? 'Close' : 'Cancel'}
          </Button>
          {!loading && !createdUrl && trackers.length > 0 && (
            <Button
              onClick={() => void handleCreate()}
              disabled={submitting || !draft.title.trim() || !draft.body.trim() || projectKeyMissing}
            >
              {submitting ? <LoadingSpinner size="sm" /> : `Create ${label}`}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
