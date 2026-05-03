import { useState, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api'
import type { AnalysisResult, AiConfig } from '@/types'
import { Section } from '@/components/shared/Section'
import { Toggle } from '@/components/shared/Toggle'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { Plus, Trash2, RotateCw } from 'lucide-react'

interface ReAnalyzeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  result: AnalysisResult
  jobId: string
}

function initFormState(p: AnalysisResult['request_params']) {
  return {
    aiProvider: p?.ai_provider || 'claude',
    aiModel: p?.ai_model || '',
    aiCliTimeout: p?.ai_cli_timeout != null ? (p.ai_cli_timeout as number) : undefined,
    rawPrompt: (p?.raw_prompt as string) || '',
    enablePeers: !!(p?.peer_ai_configs?.length),
    peerConfigs: p?.peer_ai_configs || [],
    maxRounds: p?.peer_analysis_max_rounds || 3,
    testsRepoUrl: p?.tests_repo_url || '',
    testsRepoRef: p?.tests_repo_ref || '',
    additionalRepos: (p?.additional_repos || []).map((r) => ({
      name: r.name,
      url: r.url,
      ref: r.ref || '',
    })),
    enableJira: p?.enable_jira != null ? (p.enable_jira as boolean) : undefined,
    jiraUrl: (p?.jira_url as string) || '',
    jiraProjectKey: (p?.jira_project_key as string) || '',
    getArtifacts: p?.get_job_artifacts != null ? (p.get_job_artifacts as boolean) : undefined,
    maxArtifactsSize: p?.jenkins_artifacts_max_size_mb != null ? (p.jenkins_artifacts_max_size_mb as number) : undefined,
    force: p?.force ?? false,
  }
}

export function ReAnalyzeDialog({ open, onOpenChange, result, jobId }: ReAnalyzeDialogProps) {
  const navigate = useNavigate()
  const params = result.request_params

  const init = initFormState(params)
  const [aiProvider, setAiProvider] = useState(init.aiProvider)
  const [aiModel, setAiModel] = useState(init.aiModel)
  const [aiCliTimeout, setAiCliTimeout] = useState<number | undefined>(init.aiCliTimeout)
  const [rawPrompt, setRawPrompt] = useState(init.rawPrompt)

  const [enablePeers, setEnablePeers] = useState(init.enablePeers)
  const [peerConfigs, setPeerConfigs] = useState<AiConfig[]>(init.peerConfigs)
  const [maxRounds, setMaxRounds] = useState(init.maxRounds)

  const [testsRepoUrl, setTestsRepoUrl] = useState(init.testsRepoUrl)
  const [testsRepoRef, setTestsRepoRef] = useState(init.testsRepoRef)
  const [additionalRepos, setAdditionalRepos] = useState<
    Array<{ name: string; url: string; ref: string }>
  >(init.additionalRepos)

  const [enableJira, setEnableJira] = useState<boolean | undefined>(init.enableJira)
  const [jiraUrl, setJiraUrl] = useState(init.jiraUrl)
  const [jiraProjectKey, setJiraProjectKey] = useState(init.jiraProjectKey)

  const [getArtifacts, setGetArtifacts] = useState<boolean | undefined>(init.getArtifacts)
  const [maxArtifactsSize, setMaxArtifactsSize] = useState<number | undefined>(init.maxArtifactsSize)

  const [force, setForce] = useState(init.force)

  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Fetch available models when provider changes
  useEffect(() => {
    if (!aiProvider) { setAvailableModels([]); return }
    let ignore = false
    setAvailableModels([])
    api.get<{ models: ModelOption[] }>(`/api/ai-models?provider=${aiProvider}`)
      .then(res => { if (!ignore) setAvailableModels(res.models ?? []) })
      .catch(() => { if (!ignore) setAvailableModels([]) })
    return () => { ignore = true }
  }, [aiProvider])

  // Reset form state when dialog opens
  useEffect(() => {
    if (!open) return
    const s = initFormState(result.request_params)
    setAiProvider(s.aiProvider)
    setAiModel(s.aiModel)
    setAiCliTimeout(s.aiCliTimeout)
    setRawPrompt(s.rawPrompt)
    setEnablePeers(s.enablePeers)
    setPeerConfigs(s.peerConfigs)
    setMaxRounds(s.maxRounds)
    setTestsRepoUrl(s.testsRepoUrl)
    setTestsRepoRef(s.testsRepoRef)
    setAdditionalRepos(s.additionalRepos)
    setEnableJira(s.enableJira)
    setJiraUrl(s.jiraUrl)
    setJiraProjectKey(s.jiraProjectKey)
    setGetArtifacts(s.getArtifacts)
    setMaxArtifactsSize(s.maxArtifactsSize)
    setForce(s.force)
    setSubmitting(false)
    setError('')
  }, [open, result.request_params])

  const handleSubmit = useCallback(async () => {
    setSubmitting(true)
    setError('')
    try {
      const body: Record<string, unknown> = {
        ai_provider: aiProvider,
        ai_model: aiModel,
        force,
        ...(aiCliTimeout !== undefined && { ai_cli_timeout: aiCliTimeout }),
        ...(enableJira !== undefined && { enable_jira: enableJira }),
        ...(jiraUrl && { jira_url: jiraUrl }),
        ...(jiraProjectKey && { jira_project_key: jiraProjectKey }),
        ...(getArtifacts !== undefined && { get_job_artifacts: getArtifacts }),
        ...(maxArtifactsSize !== undefined && { jenkins_artifacts_max_size_mb: maxArtifactsSize }),
        ...(rawPrompt && { raw_prompt: rawPrompt }),
        ...(testsRepoUrl && { tests_repo_url: testsRepoRef ? `${testsRepoUrl}:${testsRepoRef}` : testsRepoUrl }),
        peer_ai_configs: enablePeers ? peerConfigs : [],
        peer_analysis_max_rounds: maxRounds,
        additional_repos: additionalRepos
          .filter((r) => r.name && r.url)
          .map((r) => ({
            name: r.name,
            url: r.url,
            ...(r.ref && { ref: r.ref }),
          })),
      }
      const data = await api.post<{ job_id: string }>(`/re-analyze/${jobId}`, body)
      onOpenChange(false)
      navigate(`/results/${data.job_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit re-analysis')
    } finally {
      setSubmitting(false)
    }
  }, [
    aiProvider,
    aiModel,
    force,
    aiCliTimeout,
    rawPrompt,
    enablePeers,
    peerConfigs,
    maxRounds,
    testsRepoUrl,
    testsRepoRef,
    additionalRepos,
    enableJira,
    jiraUrl,
    jiraProjectKey,
    getArtifacts,
    maxArtifactsSize,
    jobId,
    onOpenChange,
    navigate,
  ])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[600px] max-h-[85vh] flex flex-col bg-surface-card border-border-default p-0">
        <DialogHeader className="px-6 pt-5 pb-4 border-b border-border-default flex-shrink-0">
          <DialogTitle>🔄 Re-Analyze Job</DialogTitle>
          <DialogDescription>
            Adjust settings and re-run analysis. A new analysis will be created.
          </DialogDescription>
        </DialogHeader>

        <div className="overflow-y-auto flex-1 px-6 py-5 space-y-1">
          {/* AI Configuration */}
          <Section title="AI Configuration" dotColor="bg-signal-blue" defaultOpen>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>AI Provider</FieldLabel>
                <Select value={aiProvider} onValueChange={setAiProvider}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="claude">Claude</SelectItem>
                    <SelectItem value="gemini">Gemini</SelectItem>
                    <SelectItem value="cursor">Cursor</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <FieldLabel>AI CLI Timeout</FieldLabel>
                <Input
                  type="number"
                  min={1}
                  value={aiCliTimeout ?? ''}
                  placeholder="10"
                  onChange={(e) => setAiCliTimeout(e.target.value ? Number(e.target.value) || 1 : undefined)}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <FieldLabel>AI Model</FieldLabel>
              <ModelCombobox
                value={aiModel}
                onChange={setAiModel}
                options={availableModels}
                placeholder="Default model"
              />
            </div>
            <div className="space-y-1.5">
              <FieldLabel>Raw Prompt</FieldLabel>
              <textarea
                className="flex w-full rounded-md border border-border-default bg-surface-elevated px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent min-h-[80px] resize-y"
                placeholder="Custom prompt to send to AI..."
                value={rawPrompt}
                onChange={(e) => setRawPrompt(e.target.value)}
              />
            </div>
          </Section>

          <hr className="border-border-muted" />

          {/* Peer Analysis */}
          <Section
            title="Peer Analysis"
            dotColor="bg-signal-purple"
            defaultOpen={enablePeers}
          >
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Enable peer review</span>
              <Toggle checked={enablePeers} onChange={setEnablePeers} label="Enable peer review" />
            </div>
            {enablePeers && (
              <>
                <div className="space-y-2">
                  {peerConfigs.map((peer, i) => (
                    <div
                      key={i}
                      className="bg-surface-elevated border border-border-default rounded-lg p-2.5 flex items-center gap-2"
                    >
                      <Select
                        value={peer.ai_provider}
                        onValueChange={(v) => {
                          const next = [...peerConfigs]
                          next[i] = { ...next[i], ai_provider: v }
                          setPeerConfigs(next)
                        }}
                      >
                        <SelectTrigger className="w-[120px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="claude">Claude</SelectItem>
                          <SelectItem value="gemini">Gemini</SelectItem>
                          <SelectItem value="cursor">Cursor</SelectItem>
                        </SelectContent>
                      </Select>
                      <Input
                        className="flex-1"
                        placeholder="Model"
                        value={peer.ai_model}
                        onChange={(e) => {
                          const next = [...peerConfigs]
                          next[i] = { ...next[i], ai_model: e.target.value }
                          setPeerConfigs(next)
                        }}
                      />
                      <button
                        type="button"
                        className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
                        onClick={() => setPeerConfigs(peerConfigs.filter((_, j) => j !== i))}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
                  onClick={() =>
                    setPeerConfigs([...peerConfigs, { ai_provider: 'claude', ai_model: '' }])
                  }
                >
                  <Plus className="h-3.5 w-3.5" />
                  Add Peer
                </button>
                <div className="space-y-1.5">
                  <FieldLabel>Max Rounds</FieldLabel>
                  <Input
                    type="number"
                    min={1}
                    max={10}
                    value={maxRounds}
                    onChange={(e) => setMaxRounds(Number(e.target.value) || 1)}
                    className="w-24"
                  />
                </div>
              </>
            )}
          </Section>

          <hr className="border-border-muted" />

          {/* Source Repositories */}
          <Section title="Source Repositories" dotColor="bg-signal-green">
            <div className="grid grid-cols-3 gap-3">
              <div className="col-span-2 space-y-1.5">
                <FieldLabel>Tests Repo URL</FieldLabel>
                <Input
                  placeholder="https://github.com/org/repo"
                  value={testsRepoUrl}
                  onChange={(e) => setTestsRepoUrl(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>Ref / Branch</FieldLabel>
                <Input
                  placeholder="main"
                  value={testsRepoRef}
                  onChange={(e) => setTestsRepoRef(e.target.value)}
                />
              </div>
            </div>
            <div className="space-y-2">
              <FieldLabel>Additional Repositories</FieldLabel>
              {additionalRepos.map((repo, i) => (
                <div
                  key={i}
                  className="bg-surface-elevated border border-border-default rounded-lg p-2.5 space-y-2"
                >
                  <div className="flex items-center gap-2">
                    <Input
                      className="w-32"
                      placeholder="Name"
                      value={repo.name}
                      onChange={(e) => {
                        const next = [...additionalRepos]
                        next[i] = { ...next[i], name: e.target.value }
                        setAdditionalRepos(next)
                      }}
                    />
                    <Input
                      className="flex-1"
                      placeholder="URL"
                      value={repo.url}
                      onChange={(e) => {
                        const next = [...additionalRepos]
                        next[i] = { ...next[i], url: e.target.value }
                        setAdditionalRepos(next)
                      }}
                    />
                    <Input
                      className="w-24"
                      placeholder="Ref"
                      value={repo.ref}
                      onChange={(e) => {
                        const next = [...additionalRepos]
                        next[i] = { ...next[i], ref: e.target.value }
                        setAdditionalRepos(next)
                      }}
                    />
                    <button
                      type="button"
                      className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
                      onClick={() =>
                        setAdditionalRepos(additionalRepos.filter((_, j) => j !== i))
                      }
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              ))}
              <button
                type="button"
                className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
                onClick={() =>
                  setAdditionalRepos([...additionalRepos, { name: '', url: '', ref: '' }])
                }
              >
                <Plus className="h-3.5 w-3.5" />
                Add Repository
              </button>
            </div>
          </Section>

          <hr className="border-border-muted" />

          {/* Jira Integration */}
          <Section title="Jira Integration" dotColor="bg-signal-orange">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Enable Jira search</span>
              <Toggle checked={enableJira ?? true} onChange={setEnableJira} label="Enable Jira search" />
            </div>
            {enableJira && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <FieldLabel>Jira URL</FieldLabel>
                    <Input
                      placeholder="https://jira.example.com"
                      value={jiraUrl}
                      onChange={(e) => setJiraUrl(e.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <FieldLabel>Project Key</FieldLabel>
                    <Input
                      placeholder="PROJ"
                      value={jiraProjectKey}
                      onChange={(e) => setJiraProjectKey(e.target.value)}
                    />
                  </div>
                </div>
                <p className="text-[11px] text-text-tertiary">
                  🔒 Credentials from original analysis will be reused securely.
                </p>
              </>
            )}
          </Section>

          <hr className="border-border-muted" />

          {/* Force Analysis */}
          <Section title="Advanced" dotColor="bg-text-tertiary">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Force analysis on successful builds</span>
              <Toggle checked={force} onChange={setForce} label="Force analysis on successful builds" />
            </div>
            <p className="text-[11px] text-text-tertiary">
              When enabled, analysis runs even if Jenkins reports the build as SUCCESS.
            </p>
          </Section>

          <hr className="border-border-muted" />

          {/* Jenkins Artifacts */}
          <Section title="Jenkins Artifacts" dotColor="bg-[#58a6ff]">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Fetch build artifacts</span>
              <Toggle checked={getArtifacts ?? true} onChange={setGetArtifacts} label="Fetch build artifacts" />
            </div>
            {getArtifacts && (
              <div className="space-y-1.5">
                <FieldLabel>Max Size (MB)</FieldLabel>
                <Input
                  type="number"
                  min={1}
                  value={maxArtifactsSize ?? ''}
                  placeholder="50"
                  onChange={(e) => setMaxArtifactsSize(e.target.value ? Number(e.target.value) || 1 : undefined)}
                />
              </div>
            )}
          </Section>
        </div>

        <DialogFooter className="px-6 py-4 border-t border-border-default flex-shrink-0">
          {error && <p className="text-signal-red text-xs mr-auto">{error}</p>}
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting} className="gap-1.5">
            <RotateCw className={`h-3.5 w-3.5 ${submitting ? 'animate-spin' : ''}`} />
            Re-Analyze
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
