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
import { api, extractApiDetail } from '@/lib/api'
import { toIntInRange, resolveBuildDisplayId, ciSourceLabel } from '@/lib/utils'
import type { AnalysisResult } from '@/types'
import { Section } from '@/components/shared/Section'
import { Toggle } from '@/components/shared/Toggle'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { AnalysisProviderSelect, AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { CredentialAccessNotice } from '@/components/shared/CredentialAccessNotice'
import { isAnalysisFormAiUnavailable, completeAiPairOverride } from '@/lib/analysisAi'
import { resolvedPeerConfigs } from '@/lib/peerDebate'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { AdditionalReposList } from '@/components/shared/AdditionalReposList'
import type { RepoWithId } from '@/components/shared/AdditionalReposList'
import { RotateCw } from 'lucide-react'
import { normalizeProvider } from '@/lib/aiProviders'
import { useAuth } from '@/lib/auth'

interface ReAnalyzeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  result: AnalysisResult
  jobId: string
  failureUuid?: string
  inPlaceAnalyze?: boolean
}

function initFormState(p: AnalysisResult['request_params'], result: AnalysisResult) {
  // Resolved peer pairs actually used by the completed analysis — replay them
  // when the request params omitted peer configs (same params → result → empty
  // chain as the primary provider/model).
  const resolvedPeers = resolvedPeerConfigs(result)
  return {
    // No hardcoded fallback: explicit request params win, then the values the
    // server actually resolved for the original analysis, else empty (deferred
    // to the server-side resolution chain on submit).
    aiProvider: normalizeProvider(p?.ai_provider || result.ai_provider || ''),
    aiModel: p?.ai_model || result.ai_model || '',
    forceServerCredentials: p?.force_server_credentials === true,
    aiCallTimeout: p?.ai_call_timeout != null ? (p.ai_call_timeout as number) : undefined,
    rawPrompt: (p?.raw_prompt as string) || '',
    enablePeers: !!(p?.peer_ai_configs?.length) || resolvedPeers.length > 0,
    peerConfigs: (
      p?.peer_ai_configs?.length
        ? p.peer_ai_configs
        : resolvedPeers
    ).map(c => ({
      ...c,
      ai_provider: c.ai_provider ? normalizeProvider(c.ai_provider) : '',
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
    getArtifacts: p?.get_job_artifacts != null
      ? (p.get_job_artifacts as boolean)
      : undefined,
    maxArtifactsSize: p?.jenkins_artifacts_max_size_mb != null ? (p.jenkins_artifacts_max_size_mb as number) : undefined,
  }
}

export function ReAnalyzeDialog({ open, onOpenChange, result, jobId, failureUuid, inPlaceAnalyze }: ReAnalyzeDialogProps) {
  const navigate = useNavigate()
  const { canUseServerProviders, canUseFreeProviders } = useAuth()
  const params = result.request_params
  const isProwJob = ciSourceLabel(result.request_params) === 'Prow'

  const init = initFormState(params, result)
  const [aiProvider, setAiProvider] = useState(init.aiProvider)
  const [aiModel, setAiModel] = useState(init.aiModel)
  const [forceServerCredentials, setForceServerCredentials] = useState(init.forceServerCredentials)
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

  const effectiveForceServer = forceServerCredentials && canUseServerProviders
  const { providers, providerStatus } = useProviderCatalog(effectiveForceServer)

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Reset form state when dialog opens
  useEffect(() => {
    if (!open) return
    const s = initFormState(result.request_params, result)
    setAiProvider(s.aiProvider)
    setAiModel(s.aiModel)
    setForceServerCredentials(s.forceServerCredentials)
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
  }, [open, result.request_params, inPlaceAnalyze])

  // Defer exactly like the submit form (deferAiToTestsRepo): an empty pair
  // defers to the server-side resolution chain ONLY when a tests repo can
  // supply .rootcoz/settings.json after clone — otherwise the backend would
  // reject the request (no resolution source), so the user must pick.
  const deferPrimary = !!testsRepoUrl.trim() && !aiProvider.trim() && !aiModel.trim()
  // Empty peer rows carry no selection; they are dropped from the submitted
  // configs, so they must not block validation either. Zero active peers while
  // peer review is enabled still blocks, as on the submit form.
  const activePeers = peerConfigs.filter((c) => c.ai_provider.trim())
  const aiUnavailable = isAnalysisFormAiUnavailable(providers, providerStatus, { ai_provider: aiProvider, ai_model: aiModel }, activePeers, enablePeers, effectiveForceServer, canUseServerProviders, canUseFreeProviders, deferPrimary)

  const handleSubmit = useCallback(async () => {
    if (aiUnavailable) {
      setError('Select an available AI provider and model.')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const body: Record<string, unknown> = {
        ...completeAiPairOverride(aiProvider, aiModel),
        force_server_credentials: effectiveForceServer,
        ...(aiCallTimeout !== undefined && { ai_call_timeout: aiCallTimeout }),
        ...(enableJira !== undefined && { enable_jira: enableJira }),
        ...(jiraUrl && { jira_url: jiraUrl }),
        ...(jiraProjectKey && { jira_project_key: jiraProjectKey }),
        ...(getArtifacts !== undefined && { get_job_artifacts: getArtifacts }),
        ...(maxArtifactsSize !== undefined && { jenkins_artifacts_max_size_mb: maxArtifactsSize }),
        ...(rawPrompt && { raw_prompt: rawPrompt }),
        ...(testsRepoUrl && { tests_repo_url: testsRepoRef ? `${testsRepoUrl}:${testsRepoRef}` : testsRepoUrl }),
        peer_ai_configs: enablePeers ? activePeers.map(({ ai_provider, ai_model }) => ({ ai_provider, ai_model })) : [],
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
      setError(extractApiDetail(err) ?? (err instanceof Error ? err.message : 'Failed to submit re-analysis'))
    } finally {
      setSubmitting(false)
    }
  }, [
    aiProvider,
    aiModel,
    effectiveForceServer,
    aiUnavailable,
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
                <AnalysisProviderSelect value={aiProvider} onChange={(v) => { setAiProvider(v); setAiModel('') }} forceServer={effectiveForceServer} />
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
              <AnalysisModelSelect provider={aiProvider} value={aiModel} onChange={setAiModel} forceServer={effectiveForceServer} />
            </div>
            {deferPrimary && (
              <p className="text-[11px] text-text-tertiary">
                Left empty, the provider and model are resolved by the server from the repository settings or the admin's server settings.
              </p>
            )}
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Use server credentials</span>
              <Toggle checked={effectiveForceServer} onChange={setForceServerCredentials} label="Use server credentials" disabled={!canUseServerProviders} />
            </div>
            {!canUseServerProviders && <CredentialAccessNotice className="mt-2" message={params?.force_server_credentials ? 'The original analysis used server credentials, but you no longer have access. Select a model available with your own AI key or ask an admin for access.' : undefined} />}
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
                  setPeerConfigs([{ id: crypto.randomUUID(), ai_provider: '', ai_model: '' }])
                }
              }} label="Enable peer review" />
            </div>
            {enablePeers && (
              <PeerConfigList
                peerConfigs={peerConfigs}
                setPeerConfigs={setPeerConfigs}
                maxRounds={maxRounds}
                setMaxRounds={setMaxRounds}
                forceServer={effectiveForceServer}
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
          {aiUnavailable && <p role="alert" className="text-signal-amber text-xs mr-auto">Select an available AI provider and model. Add your own key or ask an admin for server access.</p>}
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting || aiUnavailable} className="gap-1.5">
            <RotateCw className={`h-3.5 w-3.5 ${submitting ? 'animate-spin' : ''}`} />
            {inPlaceAnalyze ? 'Analyze' : 'Re-Analyze'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
