import { useState, useCallback, useEffect } from 'react'
import { useProviderModels } from '@/lib/useProviderModels'
import { usePeerModels } from '@/lib/usePeerModels'
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
import { toIntInRange, resolveBuildDisplayId, ciSourceLabel } from '@/lib/utils'
import type { AnalysisResult } from '@/types'
import { Section } from '@/components/shared/Section'
import { Toggle } from '@/components/shared/Toggle'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderOptions } from '@/lib/useProviderOptions'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { AdditionalReposList } from '@/components/shared/AdditionalReposList'
import type { RepoWithId } from '@/components/shared/AdditionalReposList'
import { RotateCw } from 'lucide-react'
import { normalizeProvider } from '@/lib/aiProviders'

interface ReAnalyzeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  result: AnalysisResult
  jobId: string
  failureUuid?: string
  inPlaceAnalyze?: boolean
}

function initFormState(p: AnalysisResult['request_params']) {
  return {
    aiProvider: normalizeProvider(p?.ai_provider || 'claude'),
    aiModel: p?.ai_model || '',
    aiCallTimeout: p?.ai_call_timeout != null ? (p.ai_call_timeout as number) : undefined,
    rawPrompt: (p?.raw_prompt as string) || '',
    enablePeers: !!(p?.peer_ai_configs?.length),
    peerConfigs: (p?.peer_ai_configs || []).map(c => ({
      ...c,
      ai_provider: normalizeProvider(c.ai_provider || 'claude'),
      id: crypto.randomUUID(),
    })),
    maxRounds: p?.peer_analysis_max_rounds || 3,
    testsRepoUrl: p?.tests_repo_url || '',
    testsRepoRef: p?.tests_repo_ref || '',
    additionalRepos: (p?.additional_repos || []).map((r) => ({
      id: crypto.randomUUID(),
      name: r.name,
      url: r.url,
      ref: r.ref || '',
    })),
    enableJira: p?.enable_jira != null ? (p.enable_jira as boolean) : undefined,
    jiraUrl: (p?.jira_url as string) || '',
    jiraProjectKey: (p?.jira_project_key as string) || '',
    getArtifacts: p?.get_job_artifacts != null ? (p.get_job_artifacts as boolean) : undefined,
    maxArtifactsSize: p?.jenkins_artifacts_max_size_mb != null ? (p.jenkins_artifacts_max_size_mb as number) : undefined,
  }
}

export function ReAnalyzeDialog({ open, onOpenChange, result, jobId, failureUuid, inPlaceAnalyze }: ReAnalyzeDialogProps) {
  const navigate = useNavigate()
  const params = result.request_params
  const isProwJob = ciSourceLabel(result.request_params) === 'Prow'

  const init = initFormState(params)
  const [aiProvider, setAiProvider] = useState(init.aiProvider)
  const [aiModel, setAiModel] = useState(init.aiModel)
  const [aiCallTimeout, setAiCallTimeout] = useState<number | undefined>(init.aiCallTimeout)
  const [rawPrompt, setRawPrompt] = useState(init.rawPrompt)

  const [enablePeers, setEnablePeers] = useState(init.enablePeers)
  const [peerConfigs, setPeerConfigs] = useState<PeerConfigWithId[]>(init.peerConfigs)
  const [maxRounds, setMaxRounds] = useState(init.maxRounds)

  const [testsRepoUrl, setTestsRepoUrl] = useState(init.testsRepoUrl)
  const [testsRepoRef, setTestsRepoRef] = useState(init.testsRepoRef)
  const [additionalRepos, setAdditionalRepos] = useState<RepoWithId[]>(init.additionalRepos)

  const [enableJira, setEnableJira] = useState<boolean | undefined>(init.enableJira)
  const [jiraUrl, setJiraUrl] = useState(init.jiraUrl)
  const [jiraProjectKey, setJiraProjectKey] = useState(init.jiraProjectKey)

  const [getArtifacts, setGetArtifacts] = useState<boolean | undefined>(init.getArtifacts)
  const [maxArtifactsSize, setMaxArtifactsSize] = useState<number | undefined>(init.maxArtifactsSize)

  const availableModels = useProviderModels(aiProvider)
  const providerOptions = useProviderOptions(aiProvider)
  const peerModels = usePeerModels(peerConfigs, enablePeers)

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Reset form state when dialog opens
  useEffect(() => {
    if (!open) return
    const s = initFormState(result.request_params)
    setAiProvider(s.aiProvider)
    setAiModel(s.aiModel)
    setAiCallTimeout(s.aiCallTimeout)
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
        ...(aiCallTimeout !== undefined && { ai_call_timeout: aiCallTimeout }),
        ...(enableJira !== undefined && { enable_jira: enableJira }),
        ...(jiraUrl && { jira_url: jiraUrl }),
        ...(jiraProjectKey && { jira_project_key: jiraProjectKey }),
        ...(getArtifacts !== undefined && { get_job_artifacts: getArtifacts }),
        ...(maxArtifactsSize !== undefined && { jenkins_artifacts_max_size_mb: maxArtifactsSize }),
        ...(rawPrompt && { raw_prompt: rawPrompt }),
        ...(testsRepoUrl && { tests_repo_url: testsRepoRef ? `${testsRepoUrl}:${testsRepoRef}` : testsRepoUrl }),
        peer_ai_configs: enablePeers ? peerConfigs.map(({ ai_provider, ai_model }) => ({ ai_provider, ai_model })) : [],
        peer_analysis_max_rounds: maxRounds,
        additional_repos: additionalRepos
          .filter((r) => r.name.trim() && r.url.trim())
          .map((r) => ({
            name: r.name.trim(),
            url: r.url.trim(),
            ...(r.ref.trim() && { ref: r.ref.trim() }),
          })),
      }
      if (failureUuid) {
        await api.post(`/api/failures/${failureUuid}/re-analyze`, body)
        onOpenChange(false)
      } else if (inPlaceAnalyze) {
        await api.post(`/results/${jobId}/analyze`, body)
        onOpenChange(false)
        navigate(`/status/${jobId}`)
      } else {
        const data = await api.post<{ job_id: string }>(`/re-analyze/${jobId}`, body)
        onOpenChange(false)
        navigate(`/results/${data.job_id}`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit re-analysis')
    } finally {
      setSubmitting(false)
    }
  }, [
    aiProvider,
    aiModel,
    aiCallTimeout,
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
    failureUuid,
    inPlaceAnalyze,
  ])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[600px] max-h-[85vh] flex flex-col bg-surface-card border-border-default p-0">
        <DialogHeader className="px-6 pt-5 pb-4 border-b border-border-default flex-shrink-0">
          <DialogTitle>
            🔄{' '}
            {failureUuid
              ? 'Re-Analyze Test'
              : inPlaceAnalyze
                ? isProwJob
                  ? 'Analyze Prow Job'
                  : 'Analyze Job'
                : isProwJob
                  ? 'Re-Analyze Prow Job'
                  : 'Re-Analyze Job'}
          </DialogTitle>
          <DialogDescription>
            {failureUuid
              ? 'Adjust settings and re-run analysis for this test failure. The result will update in-place.'
              : inPlaceAnalyze
                ? 'Adjust settings (prompts, repos, models) stored from submit, then run AI on this job.'
                : 'Adjust settings and re-run analysis. A new analysis will be created.'}
          </DialogDescription>
        </DialogHeader>

        <div className="overflow-y-auto overflow-x-hidden flex-1 px-6 py-5 space-y-1">
          {/* Prow Job Info */}
          {isProwJob && (
          <>
          <Section title="Prow Job" dotColor="bg-signal-red" defaultOpen>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>Job Name</FieldLabel>
                <Input value={result.job_name || ''} disabled className="opacity-70" />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>Build ID</FieldLabel>
                <Input
                  value={resolveBuildDisplayId(result) ?? ''}
                  disabled
                  className="opacity-70"
                />
              </div>
            </div>
            <p className="text-[11px] text-text-tertiary">
              Source details are carried over from the original analysis.
            </p>
          </Section>

          <hr className="border-border-muted" />
          </>
          )}

          {/* AI Configuration */}
          <Section title="AI Configuration" dotColor="bg-signal-blue" defaultOpen>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>AI Provider</FieldLabel>
                <Select
                  value={aiProvider}
                  onValueChange={(v) => {
                    setAiProvider(v)
                    setAiModel('')
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {providerOptions.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <FieldLabel>AI Call Timeout</FieldLabel>
                <Input
                  type="number"
                  min={1}
                  value={aiCallTimeout ?? ''}
                  placeholder="10"
                  onChange={(e) => setAiCallTimeout(e.target.value ? toIntInRange(e.target.value, 1, 3600, 1) : undefined)}
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
              <Toggle checked={enablePeers} onChange={(v) => {
                setEnablePeers(v)
                if (v && peerConfigs.length === 0) {
                  setPeerConfigs([{ id: crypto.randomUUID(), ai_provider: 'claude', ai_model: '' }])
                }
              }} label="Enable peer review" />
            </div>
            {enablePeers && (
              <PeerConfigList
                peerConfigs={peerConfigs}
                setPeerConfigs={setPeerConfigs}
                peerModels={peerModels}
                maxRounds={maxRounds}
                setMaxRounds={setMaxRounds}
              />
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
              <AdditionalReposList repos={additionalRepos} setRepos={setAdditionalRepos} />
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

          {/* Build Artifacts */}
          <Section title={isProwJob ? 'Build Artifacts' : 'Jenkins Artifacts'} dotColor="bg-[#58a6ff]">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">
                {isProwJob ? 'Fetch GCS build artifacts' : 'Fetch build artifacts'}
              </span>
              <Toggle checked={getArtifacts ?? true} onChange={setGetArtifacts} label="Fetch build artifacts" />
            </div>
            {!isProwJob && getArtifacts && (
              <div className="space-y-1.5">
                <FieldLabel>Max Size (MB)</FieldLabel>
                <Input
                  type="number"
                  min={1}
                  value={maxArtifactsSize ?? ''}
                  placeholder="50"
                  onChange={(e) => setMaxArtifactsSize(e.target.value ? toIntInRange(e.target.value, 1, 10000, 1) : undefined)}
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
            {inPlaceAnalyze ? 'Analyze' : 'Re-Analyze'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
