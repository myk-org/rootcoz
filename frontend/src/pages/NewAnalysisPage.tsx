import { useState, useCallback, useRef, useEffect } from 'react'
import { useProviderModels } from '@/lib/useProviderModels'
import { usePeerModels } from '@/lib/usePeerModels'
import { useNavigate } from 'react-router-dom'
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
import { toIntInRange, PROW_JOB_NAME_RE, PROW_BUILD_ID_RE } from '@/lib/utils'
import { Section } from '@/components/shared/Section'
import { Toggle } from '@/components/shared/Toggle'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { useProviderOptions } from '@/lib/useProviderOptions'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { AdditionalReposList } from '@/components/shared/AdditionalReposList'
import type { RepoWithId } from '@/components/shared/AdditionalReposList'
import { Send, Upload, Loader2 } from 'lucide-react'
import type { DefaultServerSettings } from '@/types'
import { normalizeProvider } from '@/lib/aiProviders'

export function NewAnalysisPage() {
  const navigate = useNavigate()

  // Input mode
  const [inputMode, setInputMode] = useState<'jenkins' | 'prow' | 'paste' | 'upload'>('jenkins')

  // Raw XML (paste / upload)
  const [rawXml, setRawXml] = useState('')
  const [uploadFileName, setUploadFileName] = useState('')

  // Prow fields
  const [prowJobName, setProwJobName] = useState('')
  const [prowBuildId, setProwBuildId] = useState('')
  const [prowUrl, setProwUrl] = useState('')
  const [gcsBucket, setGcsBucket] = useState('')
  const [gcsPrefix, setGcsPrefix] = useState('')

  // Jenkins fields
  const [jobName, setJobName] = useState('')
  const [buildNumber, setBuildNumber] = useState<number | ''>('')
  const [jenkinsUrl, setJenkinsUrl] = useState('')
  const [jenkinsUser, setJenkinsUser] = useState('')
  const [jenkinsPassword, setJenkinsPassword] = useState('')
  const [waitForCompletion, setWaitForCompletion] = useState(true)
  const [pollInterval, setPollInterval] = useState(2)
  const [maxWait, setMaxWait] = useState(0)

  // AI configuration
  const [aiProvider, setAiProvider] = useState('')
  const [aiModel, setAiModel] = useState('')
  const [aiCallTimeout, setAiCallTimeout] = useState<number | undefined>(undefined)
  const [rawPrompt, setRawPrompt] = useState('')

  // Peer analysis
  const [enablePeers, setEnablePeers] = useState(false)
  const [peerConfigs, setPeerConfigs] = useState<PeerConfigWithId[]>([])
  const [maxRounds, setMaxRounds] = useState(3)
  // Source repositories
  const [testsRepoUrl, setTestsRepoUrl] = useState('')
  const [testsRepoRef, setTestsRepoRef] = useState('')
  const [additionalRepos, setAdditionalRepos] = useState<RepoWithId[]>([])

  // Jira integration
  const [enableJira, setEnableJira] = useState<boolean | null>(null)
  const [jiraUserToggled, setJiraUserToggled] = useState(false)
  const [jiraUrl, setJiraUrl] = useState('')
  const [jiraProjectKey, setJiraProjectKey] = useState('')

  // Tags
  const [tags, setTags] = useState<string[]>([])
  const [tagInput, setTagInput] = useState('')

  // Advanced
  const [force, setForce] = useState(false)
  const [getArtifacts, setGetArtifacts] = useState(true)
  const [maxArtifactsSize, setMaxArtifactsSize] = useState<number | undefined>(undefined)
  const [defaultsLoading, setDefaultsLoading] = useState(true)
  const [defaultsError, setDefaultsError] = useState(false)

  // Fetch server defaults on mount and pre-populate form fields
  useEffect(() => {
    let cancelled = false
    let resolved = false
    // Abort after 5s to prevent infinite spinner on slow/hung server
    const timeoutId = setTimeout(() => {
      if (!resolved && !cancelled) {
        resolved = true
        console.warn('Defaults fetch timed out after 5s')
        setDefaultsError(true)
        setDefaultsLoading(false)
      }
    }, 5000)
    api.get<DefaultServerSettings>('/api/default-server-settings').then((defaults) => {
      if (cancelled || resolved) return
      if (defaults.ai_provider) setAiProvider(normalizeProvider(defaults.ai_provider))
      if (defaults.ai_model) setAiModel(defaults.ai_model)
      setAiCallTimeout(defaults.ai_call_timeout)
      setTestsRepoUrl(defaults.tests_repo_url)
      if (defaults.tests_repo_ref) setTestsRepoRef(defaults.tests_repo_ref)
      if (defaults.additional_repos.length > 0) {
        setAdditionalRepos(
          defaults.additional_repos.map((r) => ({
            id: crypto.randomUUID(),
            name: r.name,
            url: r.url,
            ref: r.ref ?? '',
          }))
        )
      }
      if (defaults.peer_ai_configs.length > 0) {
        setEnablePeers(true)
        setPeerConfigs(
          defaults.peer_ai_configs.map((p) => ({
            id: crypto.randomUUID(),
            ai_provider: normalizeProvider(p.ai_provider),
            ai_model: p.ai_model,
          }))
        )
      }
      setMaxRounds(defaults.peer_analysis_max_rounds)
      setEnableJira(defaults.jira_enabled)
      setJiraUrl(defaults.jira_url)
      setJiraProjectKey(defaults.jira_project_key)
      setForce(defaults.force_analysis)
      setGetArtifacts(defaults.get_job_artifacts)
      setMaxArtifactsSize(defaults.jenkins_artifacts_max_size_mb)
      setWaitForCompletion(defaults.wait_for_completion)
      setPollInterval(defaults.poll_interval_minutes)
      setMaxWait(defaults.max_wait_minutes)
      if (defaults.jenkins_url) setJenkinsUrl(defaults.jenkins_url)
      if (defaults.prow_url) setProwUrl(defaults.prow_url)
      if (defaults.gcs_bucket) setGcsBucket(defaults.gcs_bucket)
    }).catch((err) => {
      if (resolved) return
      console.warn('Failed to load analysis defaults:', err)
      if (!cancelled) setDefaultsError(true)
    }).finally(() => {
      resolved = true
      clearTimeout(timeoutId)
      if (!cancelled) setDefaultsLoading(false)
    })
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [])

  const fileInputRef = useRef<HTMLInputElement>(null)

  const availableModels = useProviderModels(aiProvider)
  const providerOptions = useProviderOptions(aiProvider)
  const peerModels = usePeerModels(peerConfigs, enablePeers)

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const canSubmit =
    inputMode === 'jenkins'
      ? jobName.trim() !== '' && buildNumber !== '' && buildNumber > 0
      : inputMode === 'prow'
      ? PROW_JOB_NAME_RE.test(prowJobName.trim()) && PROW_BUILD_ID_RE.test(prowBuildId.trim())
      : rawXml.trim() !== ''

  const handleFileUpload = useCallback((file: File) => {
    setError('')
    setRawXml('')
    setUploadFileName('')
    const reader = new FileReader()
    reader.onload = (e) => {
      const content = e.target?.result
      if (typeof content === 'string') {
        setRawXml(content)
        setUploadFileName(file.name)
      }
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
    reader.onerror = () => {
      setRawXml('')
      setUploadFileName('')
      setError(`Failed to read file: ${file.name}`)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
    reader.readAsText(file)
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError('')
    try {
      const commonFields: Record<string, unknown> = {
        // Always include user-entered fields
        ...(aiProvider && { ai_provider: aiProvider }),
        ...(aiModel && { ai_model: aiModel }),
        ...(aiCallTimeout !== undefined && { ai_call_timeout: aiCallTimeout }),
        ...(rawPrompt && { raw_prompt: rawPrompt }),
        ...(jiraUserToggled && { enable_jira: enableJira }),
        ...(jiraUrl && { jira_url: jiraUrl }),
        ...(jiraProjectKey && { jira_project_key: jiraProjectKey }),
        ...(testsRepoUrl && { tests_repo_url: testsRepoRef ? `${testsRepoUrl}:${testsRepoRef}` : testsRepoUrl }),
        // Peers: send configs when enabled, send [] to explicitly disable
        ...(enablePeers && peerConfigs.length > 0
          ? { peer_ai_configs: peerConfigs.map(({ ai_provider, ai_model }) => ({ ai_provider, ai_model })) }
          : !enablePeers ? { peer_ai_configs: [] } : {}
        ),
        // Additional repos: send [] when list is empty to explicitly clear
        ...(() => {
          const validRepos = additionalRepos
            .filter((r) => r.name.trim() && r.url.trim())
            .map((r) => ({ name: r.name.trim(), url: r.url.trim(), ...(r.ref.trim() && { ref: r.ref.trim() }) }))
          return { additional_repos: validRepos }
        })(),
        peer_analysis_max_rounds: maxRounds,
      }

      if (inputMode === 'jenkins') {
        const body: Record<string, unknown> = {
          ...commonFields,
          type: 'jenkins',
          job_name: jobName.trim(),
          build_number: buildNumber,
          ...(tags.length > 0 && { tags }),
          ...(jenkinsUrl && { jenkins_url: jenkinsUrl }),
          ...(jenkinsUser && { jenkins_user: jenkinsUser }),
          ...(jenkinsPassword && { jenkins_password: jenkinsPassword }),
          force,
          wait_for_completion: waitForCompletion,
          poll_interval_minutes: pollInterval,
          max_wait_minutes: maxWait,
          get_job_artifacts: getArtifacts,
          ...(maxArtifactsSize !== undefined && { jenkins_artifacts_max_size_mb: maxArtifactsSize }),
        }
        const data = await api.post<{ job_id: string }>('/analyze', body)
        navigate(`/status/${data.job_id}`)
      } else if (inputMode === 'prow') {
        const body: Record<string, unknown> = {
          ...commonFields,
          type: 'prow',
          prow_job_name: prowJobName.trim(),
          build_id: prowBuildId.trim(),
          ...(prowUrl && { prow_url: prowUrl }),
          ...(gcsBucket && { gcs_bucket: gcsBucket }),
          ...(gcsPrefix && { gcs_prefix: gcsPrefix }),
          force,
          get_job_artifacts: getArtifacts,
          ...(tags.length > 0 && { tags }),
        }
        const data = await api.post<{ job_id: string }>('/analyze', body)
        navigate(`/status/${data.job_id}`)
      } else {
        const body: Record<string, unknown> = {
          ...commonFields,
          type: 'file',
          raw_xml: rawXml,
          ...(tags.length > 0 && { tags }),
        }
        const data = await api.post<{ job_id: string }>('/analyze', body)
        navigate(`/results/${data.job_id}`)
      }
    } catch (err) {
      console.error('Failed to submit analysis', err)
      setError('Failed to submit analysis. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }, [
    canSubmit,
    inputMode,
    rawXml,
    jobName,
    buildNumber,
    aiProvider,
    aiModel,
    tags,
    force,
    waitForCompletion,
    pollInterval,
    maxWait,
    aiCallTimeout,
    rawPrompt,
    jenkinsUrl,
    jenkinsUser,
    jenkinsPassword,
    prowJobName,
    prowBuildId,
    prowUrl,
    gcsBucket,
    gcsPrefix,
    enablePeers,
    peerConfigs,
    maxRounds,
    testsRepoUrl,
    testsRepoRef,
    additionalRepos,
    enableJira,
    jiraUserToggled,
    jiraUrl,
    jiraProjectKey,
    getArtifacts,
    maxArtifactsSize,
    navigate,
  ])

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-display text-xl font-bold text-text-primary">New Analysis</h1>
        <p className="mt-0.5 text-sm text-text-tertiary">
          Submit a CI job for AI-powered failure analysis.
        </p>
      </div>

      <div className="rounded-lg border border-border-default bg-surface-card">
        {defaultsLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin text-text-tertiary" />
            <span className="ml-2 text-sm text-text-tertiary">Loading defaults...</span>
          </div>
        ) : (<>
        {defaultsError && (
          <div className="rounded-md border border-signal-amber/30 bg-signal-amber/10 p-3 m-6 mb-0">
            <p className="text-xs text-signal-amber">⚠️ Could not load server defaults. Form shows local fallback values.</p>
          </div>
        )}
        <div className="space-y-1 p-6">
          {/* Input Mode Selector */}
          <div className="flex gap-2 pb-2">
            {(['jenkins', 'prow', 'paste', 'upload'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setInputMode(mode)}
                className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                  inputMode === mode
                    ? 'bg-signal-blue text-white'
                    : 'bg-surface-elevated text-text-secondary hover:text-text-primary'
                }`}
              >
                {mode === 'jenkins' ? 'Jenkins Job' : mode === 'prow' ? 'Prow Job' : mode === 'paste' ? 'Paste XML' : 'Upload File'}
              </button>
            ))}
          </div>

          <hr className="border-border-muted" />

          {/* Jenkins Job */}
          {inputMode === 'jenkins' && (
          <Section title="Jenkins Job" dotColor="bg-signal-red" defaultOpen>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>Job Name *</FieldLabel>
                <Input
                  placeholder="folder/job-name"
                  value={jobName}
                  onChange={(e) => setJobName(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>Build Number *</FieldLabel>
                <Input
                  type="number"
                  min={1}
                  placeholder="123"
                  value={buildNumber}
                  onChange={(e) => setBuildNumber(e.target.value ? toIntInRange(e.target.value, 1, Number.MAX_SAFE_INTEGER, 1) : '')}
                  required
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <FieldLabel>Jenkins URL</FieldLabel>
              <Input
                placeholder="https://jenkins.example.com (overrides server default)"
                value={jenkinsUrl}
                onChange={(e) => setJenkinsUrl(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>Jenkins User</FieldLabel>
                <Input
                  placeholder="Username"
                  value={jenkinsUser}
                  onChange={(e) => setJenkinsUser(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>Jenkins Password / Token</FieldLabel>
                <Input
                  type="password"
                  placeholder="API token"
                  value={jenkinsPassword}
                  onChange={(e) => setJenkinsPassword(e.target.value)}
                />
              </div>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Wait for build completion</span>
              <Toggle checked={waitForCompletion} onChange={setWaitForCompletion} label="Wait for build completion" />
            </div>
            {waitForCompletion && (
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <FieldLabel>Poll Interval (min)</FieldLabel>
                  <Input
                    type="number"
                    min={1}
                    value={pollInterval}
                    onChange={(e) => setPollInterval(toIntInRange(e.target.value, 1, 1440, 1))}
                  />
                </div>
                <div className="space-y-1.5">
                  <FieldLabel>Max Wait (min, 0 = no limit)</FieldLabel>
                  <Input
                    type="number"
                    min={0}
                    value={maxWait}
                    onChange={(e) => setMaxWait(toIntInRange(e.target.value, 0, 1440, 0))}
                  />
                </div>
              </div>
            )}
          </Section>
          )}

          {/* Prow Job */}
          {inputMode === 'prow' && (
          <Section title="Prow Job" dotColor="bg-signal-red" defaultOpen>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>Job Name *</FieldLabel>
                <Input
                  placeholder="pull-kubevirt-e2e-k8s-1.36-sig-operator"
                  value={prowJobName}
                  onChange={(e) => setProwJobName(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>Build ID *</FieldLabel>
                <Input
                  inputMode="numeric"
                  pattern="[0-9]*"
                  placeholder="2072664659076321280"
                  value={prowBuildId}
                  onChange={(e) => setProwBuildId(e.target.value.replace(/[^0-9]/g, ''))}
                  required
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>Prow URL</FieldLabel>
                <Input
                  placeholder="https://prow.ci.kubevirt.io (overrides server default)"
                  value={prowUrl}
                  onChange={(e) => setProwUrl(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <FieldLabel>GCS Bucket</FieldLabel>
                <Input
                  placeholder="kubevirt-prow (overrides server default)"
                  value={gcsBucket}
                  onChange={(e) => setGcsBucket(e.target.value)}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <FieldLabel>GCS Prefix</FieldLabel>
              <Input
                placeholder="Auto-resolved (override: pr-logs/pull/org_repo/pr/job/build)"
                value={gcsPrefix}
                onChange={(e) => setGcsPrefix(e.target.value)}
              />
              <p className="text-[11px] text-text-tertiary">
                Leave empty for auto-detection. Periodic jobs use logs/job/build, PR jobs are resolved via directory pointer files.
              </p>
            </div>
          </Section>
          )}

          {/* Paste XML */}
          {inputMode === 'paste' && (
          <Section title="Paste XML" dotColor="bg-signal-red" defaultOpen>
            <div className="space-y-1.5">
              <FieldLabel>JUnit XML Content *</FieldLabel>
              <textarea
                className="flex w-full rounded-md border border-border-default bg-surface-elevated px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent min-h-[200px] resize-y font-mono"
                placeholder="Paste JUnit XML content..."
                value={rawXml}
                onChange={(e) => setRawXml(e.target.value)}
              />
            </div>
          </Section>
          )}

          {/* Upload XML File */}
          {inputMode === 'upload' && (
          <Section title="Upload XML File" dotColor="bg-signal-red" defaultOpen>
            <div
              className="relative flex flex-col items-center justify-center rounded-lg border-2 border-dashed border-border-default bg-surface-elevated p-8 transition-colors hover:border-signal-blue hover:bg-surface-hover cursor-pointer"
              onDragOver={(e) => { e.preventDefault(); e.stopPropagation() }}
              onDrop={(e) => {
                e.preventDefault()
                e.stopPropagation()
                const file = e.dataTransfer.files[0]
                if (file && file.name.endsWith('.xml')) handleFileUpload(file)
              }}
              onClick={() => fileInputRef.current?.click()}
              tabIndex={0}
              role="button"
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  fileInputRef.current?.click()
                }
              }}
            >
              <Upload className="h-8 w-8 text-text-tertiary mb-2" />
              <p className="text-sm text-text-secondary">
                {uploadFileName
                  ? <><span className="font-medium text-text-primary">{uploadFileName}</span> loaded</>
                  : <>Drag & drop an XML file here, or <span className="text-text-link font-medium">browse</span></>
                }
              </p>
              {rawXml && (
                <p className="text-xs text-text-tertiary mt-1">
                  {rawXml.length.toLocaleString()} characters
                </p>
              )}
              <input
                ref={fileInputRef}
                type="file"
                accept=".xml"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) handleFileUpload(file)
                }}
              />
            </div>
          </Section>
          )}

          <hr className="border-border-muted" />

          {/* AI Configuration */}
          <Section title="AI Configuration" dotColor="bg-signal-blue">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <FieldLabel>AI Provider</FieldLabel>
                <Select
                  value={aiProvider || undefined}
                  onValueChange={(v) => {
                    setAiProvider(v)
                    setAiModel('')
                  }}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select provider..." />
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
          <Section title="Peer Analysis" dotColor="bg-signal-purple">
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
              <Toggle checked={enableJira ?? false} onChange={(v) => { setEnableJira(v); setJiraUserToggled(true) }} label="Enable Jira search" />
            </div>
            {!enableJira ? null : (
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
            )}
          </Section>

          <hr className="border-border-muted" />

          {/* Tags */}
          <Section title="Tags" dotColor="bg-signal-blue">
            <div className="space-y-2">
              <label className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary">
                Tags <span className="text-text-tertiary font-normal normal-case tracking-normal">(optional)</span>
              </label>
              <div className="flex gap-2">
                <Input
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && tagInput.trim()) {
                      e.preventDefault()
                      const newTag = tagInput.trim().toLowerCase()
                      if (!tags.includes(newTag)) setTags([...tags, newTag])
                      setTagInput('')
                    }
                  }}
                  placeholder="Add tag and press Enter..."
                  className="h-10 font-mono flex-1"
                />
              </div>
              {tags.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {tags.map((tag) => (
                    <span key={tag} className="inline-flex items-center gap-1 rounded-full bg-signal-blue/10 px-2.5 py-0.5 text-xs font-medium text-signal-blue">
                      {tag}
                      <button
                        type="button"
                        onClick={() => setTags(tags.filter((t) => t !== tag))}
                        className="ml-0.5 hover:text-signal-red transition-colors"
                        aria-label={`Remove tag ${tag}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <p className="text-xs text-text-tertiary">Categorize this analysis (e.g. regression, flaky, nightly).</p>
            </div>
          </Section>

          {(inputMode === 'jenkins' || inputMode === 'prow') && (
          <>
          <hr className="border-border-muted" />

          {/* Advanced */}
          <Section title="Advanced" dotColor="bg-text-tertiary">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Force analysis on successful builds</span>
              <Toggle checked={force} onChange={setForce} label="Force analysis on successful builds" />
            </div>
            <p className="text-[11px] text-text-tertiary">
              When enabled, analysis runs even if the CI system reports the build as SUCCESS.
            </p>
          </Section>
          </>)}

          {inputMode === 'jenkins' && (
          <>
          <hr className="border-border-muted" />

          {/* Jenkins Artifacts */}
          <Section title="Jenkins Artifacts" dotColor="bg-[#58a6ff]">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Fetch build artifacts</span>
              <Toggle checked={getArtifacts} onChange={setGetArtifacts} label="Fetch build artifacts" />
            </div>
            {getArtifacts && (
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
          </>)}

          {inputMode === 'prow' && (
          <>
          <hr className="border-border-muted" />

          {/* Prow Artifacts */}
          <Section title="Build Artifacts" dotColor="bg-[#58a6ff]">
            <div className="flex items-center justify-between">
              <span className="text-sm text-text-secondary">Fetch GCS build artifacts</span>
              <Toggle checked={getArtifacts} onChange={setGetArtifacts} label="Fetch GCS build artifacts" />
            </div>
          </Section>
          </>)}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-border-default px-6 py-4">
          <div>
            {error && <p className="text-signal-red text-xs">{error}</p>}
          </div>
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={() => navigate('/')} disabled={submitting}>
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={submitting || !canSubmit} className="gap-1.5">
              <Send className={`h-3.5 w-3.5 ${submitting ? 'animate-pulse' : ''}`} />
              {submitting ? 'Submitting…' : 'Submit Analysis'}
            </Button>
          </div>
        </div>
        </>)}
      </div>
    </div>
  )
}
