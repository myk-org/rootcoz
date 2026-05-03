import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
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
import { Badge } from '@/components/ui/badge'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { LoadingSpinner } from '@/components/shared/LoadingSpinner'
import { CheckCircle2, ExternalLink, AlertTriangle } from 'lucide-react'
import type { PreviewIssueResponse, CreateIssueResponse, SimilarIssue, CommentsAndReviews } from '@/types'
import { getGithubToken, getJiraToken, getJiraEmail } from '@/lib/cookies'
import { useReportDispatch, useRefreshEnrichments } from './ReportContext'

type BugTarget = 'github' | 'jira'
type Phase = 'idle' | 'loading-prompt' | 'prompt' | 'loading' | 'preview' | 'creating' | 'success' | 'error'

interface BugCreationDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  jobId: string
  testName: string
  target: BugTarget
  childJobName?: string
  childBuildNumber?: number
  aiProvider?: string
  aiModel?: string
  includeLinks?: boolean
  availableRepos?: Array<{ name: string; url: string }>
  defaultProjectKey?: string
  /** Called after a bug issue is successfully created (with the issue URL). */
  onIssueCreated?: (url: string) => void
}

export function BugCreationDialog({
  open,
  onOpenChange,
  jobId,
  testName,
  target,
  childJobName,
  childBuildNumber,
  includeLinks = false,
  aiProvider,
  aiModel,
  availableRepos,
  defaultProjectKey,
  onIssueCreated,
}: BugCreationDialogProps) {
  const dispatch = useReportDispatch()
  const refreshEnrichments = useRefreshEnrichments()
  const [phase, setPhase] = useState<Phase>('idle')
  const [issuePrompt, setIssuePrompt] = useState('')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [similar, setSimilar] = useState<SimilarIssue[]>([])
  const [createdUrl, setCreatedUrl] = useState('')
  const [errorMsg, setErrorMsg] = useState('')
  const [selectedRepo, setSelectedRepo] = useState(availableRepos?.[0]?.url ?? '')
  const [jiraProjectKey, setJiraProjectKey] = useState(defaultProjectKey || '')
  const [confirmedProjectKey, setConfirmedProjectKey] = useState(defaultProjectKey || '')
  const [jiraProjects, setJiraProjects] = useState<Array<{key: string; name: string}>>([])
  const [jiraSecurityLevel, setJiraSecurityLevel] = useState('')
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const [showSecurityDropdown, setShowSecurityDropdown] = useState(false)
  const [securityLevels, setSecurityLevels] = useState<Array<{id: string; name: string; description: string}>>([])
  const [jiraIssueType, setJiraIssueType] = useState('Bug')
  const [customIssueType, setCustomIssueType] = useState('')

  const JIRA_ISSUE_TYPES = ['Bug', 'Task', 'Story', 'Epic', 'Sub-task']

  const previewPath = target === 'github' ? 'preview-github-issue' : 'preview-jira-bug'
  const createPath = target === 'github' ? 'create-github-issue' : 'create-jira-bug'
  const label = target === 'github' ? 'GitHub Issue' : 'Jira Ticket'
  const hasToken = target === 'github' ? !!getGithubToken() : !!getJiraToken()

  function getTrackerCredentials() {
    return {
      github_token: target === 'github' ? getGithubToken() : '',
      jira_token: target === 'jira' ? getJiraToken() : '',
      jira_email: target === 'jira' ? getJiraEmail() : '',
      ...(target === 'github' && selectedRepo ? { github_repo_url: selectedRepo } : {}),
      ...(target === 'jira' && jiraProjectKey ? { jira_project_key: jiraProjectKey } : {}),
      ...(target === 'jira' && jiraSecurityLevel ? { jira_security_level: jiraSecurityLevel } : {}),
      ...(target === 'jira' ? { jira_issue_type: jiraIssueType === '__custom__' ? customIssueType : jiraIssueType } : {}),
    }
  }

  // Debounced Jira project search
  useEffect(() => {
    let ignore = false
    if (!open || target !== 'jira') return
    if (jiraProjectKey.length < 2) {
      setJiraProjects([])
      setShowProjectDropdown(false)
      return
    }
    const timer = setTimeout(() => {
      api.post<Array<{key: string; name: string}>>('/api/jira-projects', {
        jira_token: getJiraToken(),
        jira_email: getJiraEmail(),
        query: jiraProjectKey,
      })
        .then((data) => { if (!ignore) setJiraProjects(data) })
        .catch((err) => console.warn('Failed to fetch Jira projects:', err))
    }, 300)
    return () => { ignore = true; clearTimeout(timer) }
  }, [open, target, jiraProjectKey])

  // Fetch security levels only after user confirms a project selection
  useEffect(() => {
    let ignore = false
    if (!open || target !== 'jira' || !confirmedProjectKey) {
      setSecurityLevels([])
      return
    }
    api.post<Array<{id: string; name: string; description: string}>>('/api/jira-security-levels', {
      jira_token: getJiraToken(),
      jira_email: getJiraEmail(),
      project_key: confirmedProjectKey,
    })
      .then((data) => { if (!ignore) setSecurityLevels(data) })
      .catch((err) => console.warn('Failed to fetch security levels:', err))
    return () => { ignore = true }
  }, [open, target, confirmedProjectKey])

  // Fetch default issue prompt when dialog opens
  useEffect(() => {
    if (!open || phase !== 'idle') return
    let ignore = false
    setPhase('loading-prompt')
    api
      .get<{ prompt?: string; issue_prompt?: string }>(`/results/${jobId}/issue-prompt`)
      .then((res) => {
        if (ignore) return
        setIssuePrompt(res.issue_prompt ?? res.prompt ?? '')
        setPhase('prompt')
      })
      .catch(() => {
        if (ignore) return
        setIssuePrompt('')
        setPhase('prompt')
      })
    return () => {
      ignore = true
    }
  }, [open, jobId])

  function handleContinueFromPrompt() {
    setPhase('loading')
    api
      .post<PreviewIssueResponse>(`/results/${jobId}/${previewPath}`, {
        test_name: testName,
        include_links: includeLinks,
        ai_provider: aiProvider ?? '',
        ai_model: aiModel ?? '',
        child_job_name: childJobName ?? '',
        child_build_number: childBuildNumber ?? 0,
        issue_prompt: issuePrompt,
        ...getTrackerCredentials(),
      })
      .then((res) => {
        setTitle(res.title)
        setBody(res.body)
        setSimilar(res.similar_issues ?? [])
        setPhase('preview')
      })
      .catch((err) => {
        setErrorMsg(err instanceof Error ? err.message : 'Preview failed')
        setPhase('error')
      })
  }

  async function handleCreate() {
    setPhase('creating')
    try {
      const res = await api.post<CreateIssueResponse>(`/results/${jobId}/${createPath}`, {
        test_name: testName,
        title,
        body,
        child_job_name: childJobName ?? '',
        child_build_number: childBuildNumber ?? 0,
        ...getTrackerCredentials(),
      })
      setCreatedUrl(res.url)
      setPhase('success')
      onIssueCreated?.(res.url)

      // After successful creation, refresh comments to get the server-added comment
      api.get<CommentsAndReviews>(`/results/${jobId}/comments`)
        .then((commentsRes) => dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: commentsRes }))
        .catch(() => {})

      // Also refresh enrichments for the link badges
      refreshEnrichments(jobId)
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Creation failed')
      setPhase('error')
    }
  }

  const isBusy = phase === 'loading-prompt' || phase === 'loading' || phase === 'prompt' || phase === 'preview' || phase === 'creating'

  function resetState() {
    setTimeout(() => {
      setPhase('idle')
      setIssuePrompt('')
      setTitle('')
      setBody('')
      setSimilar([])
      setCreatedUrl('')
      setErrorMsg('')
      setSelectedRepo(availableRepos?.[0]?.url ?? '')
      setJiraProjectKey(defaultProjectKey || '')
      setConfirmedProjectKey(defaultProjectKey || '')
      setJiraProjects([])
      setJiraSecurityLevel('')
      setSecurityLevels([])
      setShowProjectDropdown(false)
      setShowSecurityDropdown(false)
      setJiraIssueType('Bug')
      setCustomIssueType('')
    }, 200)
  }

  function handleCancel() {
    onOpenChange(false)
    resetState()
  }

  function handleClose(nextOpen: boolean) {
    if (!nextOpen && isBusy) return
    if (nextOpen) return
    onOpenChange(false)
    resetState()
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent hideCloseButton={isBusy} className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{phase === 'success' ? `${label} Created` : `Create ${label}`}</DialogTitle>
          {phase === 'preview' && <DialogDescription>Review and edit before creating.</DialogDescription>}
        </DialogHeader>

        {/* Loading prompt */}
        {phase === 'loading-prompt' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Loading issue prompt...</p>
          </div>
        )}

        {/* Prompt */}
        {phase === 'prompt' && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="bug-issue-prompt" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Issue Prompt</label>
              <p className="text-xs text-text-tertiary">Customize the prompt used to generate the issue title and body. Edit or leave as-is, then continue.</p>
              <Textarea
                id="bug-issue-prompt"
                value={issuePrompt}
                onChange={(e) => setIssuePrompt(e.target.value)}
                rows={6}
                placeholder="Enter a prompt to guide issue generation..."
                className="font-mono text-xs"
              />
            </div>
          </div>
        )}

        {/* Loading */}
        {phase === 'loading' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Generating preview...</p>
          </div>
        )}

        {/* Preview */}
        {phase === 'preview' && (
          <div className="space-y-4">
            {target === 'github' && availableRepos && availableRepos.length > 1 && (
              <div className="space-y-2">
                <label htmlFor="bug-repo" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Repository</label>
                <select id="bug-repo" value={selectedRepo} onChange={(e) => setSelectedRepo(e.target.value)} className="w-full h-9 rounded-md border border-border-default bg-surface-elevated px-2 text-sm text-text-primary">
                  {availableRepos.map((r) => (
                    <option key={r.url} value={r.url}>{r.name}</option>
                  ))}
                </select>
              </div>
            )}
            {target === 'jira' && (
              <div className="space-y-2">
                <label htmlFor="bug-jira-project" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Jira Project</label>
                <div className="relative">
                  <input
                    id="bug-jira-project"
                    type="text"
                    value={jiraProjectKey}
                    onChange={(e) => {
                      const val = e.target.value
                      setJiraProjectKey(val)
                      setShowProjectDropdown(true)
                      if (val !== confirmedProjectKey) {
                        setConfirmedProjectKey('')
                        setJiraSecurityLevel('')
                        setSecurityLevels([])
                      }
                    }}
                    onFocus={() => setShowProjectDropdown(true)}
                    onBlur={() => setTimeout(() => setShowProjectDropdown(false), 200)}
                    placeholder="Type to search projects..."
                    autoComplete="off"
                    className="w-full h-9 rounded-md border border-border-default bg-surface-elevated px-2 text-sm text-text-primary placeholder:text-text-tertiary"
                  />
                  {showProjectDropdown && jiraProjects.length > 0 && (
                    <div className="absolute z-50 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-border-default bg-surface-card shadow-lg">
                      {jiraProjects.map((p) => (
                          <button
                            key={p.key}
                            type="button"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => {
                              setJiraProjectKey(p.key)
                              setConfirmedProjectKey(p.key)
                              setShowProjectDropdown(false)
                            }}
                            className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm hover:bg-surface-hover"
                          >
                            <span className="font-mono text-text-primary">{p.key}</span>
                            <span className="text-text-tertiary">{p.name}</span>
                          </button>
                        ))}
                    </div>
                  )}
                </div>
              </div>
            )}
            {target === 'jira' && (
              <div className="space-y-2">
                <label htmlFor="bug-jira-issue-type" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Issue Type</label>
                <Select value={jiraIssueType} onValueChange={setJiraIssueType}>
                  <SelectTrigger id="bug-jira-issue-type" className="w-full">
                    <SelectValue placeholder="Select issue type" />
                  </SelectTrigger>
                  <SelectContent>
                    {JIRA_ISSUE_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>{t}</SelectItem>
                    ))}
                    <SelectItem value="__custom__">Custom...</SelectItem>
                  </SelectContent>
                </Select>
                {jiraIssueType === '__custom__' && (
                  <input
                    type="text"
                    value={customIssueType}
                    onChange={(e) => setCustomIssueType(e.target.value)}
                    placeholder="Enter custom issue type..."
                    autoComplete="off"
                    className="w-full h-9 rounded-md border border-border-default bg-surface-elevated px-2 text-sm text-text-primary placeholder:text-text-tertiary mt-1"
                  />
                )}
              </div>
            )}
            {target === 'jira' && (
              <div className="space-y-2">
                <label htmlFor="bug-security-level" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Security Level</label>
                <div className="relative">
                  <input
                    id="bug-security-level"
                    type="text"
                    value={jiraSecurityLevel}
                    onChange={(e) => {
                      setJiraSecurityLevel(e.target.value)
                      setShowSecurityDropdown(true)
                    }}
                    onFocus={() => setShowSecurityDropdown(true)}
                    onBlur={() => setTimeout(() => setShowSecurityDropdown(false), 200)}
                    placeholder="None (public)"
                    autoComplete="off"
                    className="w-full h-9 rounded-md border border-border-default bg-surface-elevated px-2 text-sm text-text-primary placeholder:text-text-tertiary"
                  />
                  {showSecurityDropdown && securityLevels.length > 0 && (
                    <div className="absolute z-50 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-border-default bg-surface-card shadow-lg">
                      <button
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => { setJiraSecurityLevel(''); setShowSecurityDropdown(false) }}
                        className="flex w-full items-center px-2 py-1.5 text-left text-sm hover:bg-surface-hover text-text-tertiary"
                      >
                        None (public)
                      </button>
                      {securityLevels
                        .filter((lv) => {
                          const q = jiraSecurityLevel.toLowerCase()
                          return !q || lv.name.toLowerCase().includes(q) || lv.description.toLowerCase().includes(q)
                        })
                        .map((lv) => (
                          <button
                            key={lv.id}
                            type="button"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => { setJiraSecurityLevel(lv.name); setShowSecurityDropdown(false) }}
                            className="flex w-full flex-col px-2 py-1.5 text-left text-sm hover:bg-surface-hover"
                          >
                            <span className="text-text-primary">{lv.name}</span>
                            {lv.description && <span className="text-xs text-text-tertiary">{lv.description}</span>}
                          </button>
                        ))}
                    </div>
                  )}
                </div>
              </div>
            )}
            {similar.length > 0 && (
              <div className="rounded-md border border-signal-orange/30 bg-glow-orange p-3">
                <div className="flex items-center gap-2 text-sm font-medium text-signal-orange">
                  <AlertTriangle className="h-4 w-4" />
                  {similar.length} similar {similar.length === 1 ? 'issue' : 'issues'} found
                </div>
                <ul className="mt-2 space-y-1">
                  {similar.map((s) => (
                    <li key={s.url || s.key} className="text-xs">
                      <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
                        {s.key || `#${s.number}`}: {s.title}
                      </a>
                      {s.status && (
                        <Badge variant="outline" className="ml-2 text-[10px]">
                          {s.status}
                        </Badge>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="space-y-2">
              <label htmlFor="bug-title" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Title</label>
              <Input id="bug-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="space-y-2">
              <label htmlFor="bug-body" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Body</label>
              <Textarea id="bug-body" value={body} onChange={(e) => setBody(e.target.value)} rows={12} className="font-mono text-xs" />
            </div>
          </div>
        )}

        {/* Creating */}
        {phase === 'creating' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <LoadingSpinner size="lg" />
            <p className="text-sm text-text-secondary">Creating {label.toLowerCase()}...</p>
          </div>
        )}

        {/* Success */}
        {phase === 'success' && (
          <div className="flex flex-col items-center gap-4 py-8 animate-scale-in">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-signal-green/15">
              <CheckCircle2 className="h-8 w-8 text-signal-green" />
            </div>
            <p className="text-sm text-text-secondary">{label} created successfully.</p>
            <a
              href={createdUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-sm text-text-link hover:underline"
            >
              Open {label} <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        )}

        {/* Error */}
        {phase === 'error' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <p className="text-sm text-signal-red">{errorMsg}</p>
            {errorMsg.toLowerCase().includes('token') && errorMsg.toLowerCase().includes('invalid') && (
              <p className="text-xs text-text-tertiary">You can update your tokens in <a href="/settings" className="text-text-link hover:underline">settings</a>.</p>
            )}
          </div>
        )}

        <DialogFooter className="flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between">
          {phase === 'preview' && (
            <>
              {!hasToken && (
                <p className="text-xs text-text-tertiary">Add a {target === 'github' ? 'GitHub' : 'Jira'} token in <a href="/settings" className="text-text-link hover:underline">settings</a> to create directly.</p>
              )}
              <div className="flex gap-2 sm:ml-auto">
                <Button variant="outline" onClick={() => handleCancel()}>Cancel</Button>
                <Button onClick={handleCreate} disabled={!title.trim() || !hasToken || (target === 'jira' && jiraIssueType === '__custom__' && !customIssueType.trim())} title={!hasToken ? `Add a ${target === 'github' ? 'GitHub' : 'Jira'} token to create issues` : undefined}>Create {label}</Button>
              </div>
            </>
          )}
          {phase === 'prompt' && (
            <div className="flex gap-2 sm:ml-auto">
              <Button variant="outline" onClick={() => handleCancel()}>Cancel</Button>
              <Button onClick={handleContinueFromPrompt}>Continue</Button>
            </div>
          )}
          {(phase === 'success' || phase === 'error') && (
            <Button variant="outline" onClick={() => handleCancel()} className="sm:ml-auto">Close</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
