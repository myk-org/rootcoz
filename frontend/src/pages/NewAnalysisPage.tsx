import { useState, useCallback, useRef, useEffect } from 'react'
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
import type { AiConfig } from '@/types'
import { Section } from '@/components/shared/Section'
import { Toggle } from '@/components/shared/Toggle'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { Plus, Trash2, Send, Upload } from 'lucide-react'

function toIntInRange(value: string, min: number, max: number, fallback: number): number {
  const n = Number(value)
  if (!value || Number.isNaN(n)) return fallback
  return Math.max(min, Math.min(max, Math.floor(n)))
}

export function NewAnalysisPage() {
  const navigate = useNavigate()

  // Input mode
  const [inputMode, setInputMode] = useState<'jenkins' | 'paste' | 'upload'>('jenkins')

  // Raw XML (paste / upload)
  const [rawXml, setRawXml] = useState('')
  const [uploadFileName, setUploadFileName] = useState('')

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
  const [aiProvider, setAiProvider] = useState('claude')
  const [aiModel, setAiModel] = useState('')
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])
  const [aiCliTimeout, setAiCliTimeout] = useState<number | undefined>(undefined)
  const [rawPrompt, setRawPrompt] = useState('')

  // Peer analysis
  const [enablePeers, setEnablePeers] = useState(false)
  const [peerConfigs, setPeerConfigs] = useState<Array<AiConfig & { id: string }>>([])
  const [maxRounds, setMaxRounds] = useState(3)

  // Source repositories
  const [testsRepoUrl, setTestsRepoUrl] = useState('')
  const [testsRepoRef, setTestsRepoRef] = useState('')
  const [additionalRepos, setAdditionalRepos] = useState<
    Array<{ id: string; name: string; url: string; ref: string }>
  >([])

  // Jira integration
  const [enableJira, setEnableJira] = useState(true)
  const [jiraUrl, setJiraUrl] = useState('')
  const [jiraProjectKey, setJiraProjectKey] = useState('')

  // Advanced
  const [force, setForce] = useState(false)
  const [getArtifacts, setGetArtifacts] = useState(true)
  const [maxArtifactsSize, setMaxArtifactsSize] = useState<number | undefined>(undefined)

  const fileInputRef = useRef<HTMLInputElement>(null)

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

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const canSubmit =
    inputMode === 'jenkins'
      ? jobName.trim() !== '' && buildNumber !== '' && buildNumber > 0
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
        ai_provider: aiProvider,
        ...(aiModel && { ai_model: aiModel }),
        ...(aiCliTimeout !== undefined && { ai_cli_timeout: aiCliTimeout }),
        ...(rawPrompt && { raw_prompt: rawPrompt }),
        enable_jira: enableJira,
        ...(jiraUrl && { jira_url: jiraUrl }),
        ...(jiraProjectKey && { jira_project_key: jiraProjectKey }),
        ...(testsRepoUrl && { tests_repo_url: testsRepoRef ? `${testsRepoUrl}:${testsRepoRef}` : testsRepoUrl }),
        ...(enablePeers && peerConfigs.length > 0 && {
          peer_ai_configs: peerConfigs.map(({ ai_provider, ai_model }) => ({ ai_provider, ai_model })),
        }),
        peer_analysis_max_rounds: maxRounds,
        ...(() => {
          const validRepos = additionalRepos
            .filter((r) => r.name.trim() && r.url.trim())
            .map((r) => ({ name: r.name.trim(), url: r.url.trim(), ...(r.ref.trim() && { ref: r.ref.trim() }) }))
          return validRepos.length > 0 ? { additional_repos: validRepos } : {}
        })(),
      }

      if (inputMode === 'jenkins') {
        const body: Record<string, unknown> = {
          ...commonFields,
          job_name: jobName.trim(),
          build_number: buildNumber,
          force,
          wait_for_completion: waitForCompletion,
          poll_interval_minutes: pollInterval,
          max_wait_minutes: maxWait,
          ...(jenkinsUrl && { jenkins_url: jenkinsUrl }),
          ...(jenkinsUser && { jenkins_user: jenkinsUser }),
          ...(jenkinsPassword && { jenkins_password: jenkinsPassword }),
          get_job_artifacts: getArtifacts,
          ...(maxArtifactsSize !== undefined && { jenkins_artifacts_max_size_mb: maxArtifactsSize }),
        }
        const data = await api.post<{ job_id: string }>('/analyze', body)
        navigate(`/status/${data.job_id}`)
      } else {
        const body: Record<string, unknown> = {
          ...commonFields,
          raw_xml: rawXml,
        }
        const data = await api.post<{ job_id: string }>('/analyze-failures', body)
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
    force,
    waitForCompletion,
    pollInterval,
    maxWait,
    aiCliTimeout,
    rawPrompt,
    jenkinsUrl,
    jenkinsUser,
    jenkinsPassword,
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
    navigate,
  ])

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-display text-xl font-bold text-text-primary">New Analysis</h1>
        <p className="mt-0.5 text-sm text-text-tertiary">
          Submit a Jenkins job for AI-powered failure analysis.
        </p>
      </div>

      <div className="rounded-lg border border-border-default bg-surface-card">
        <div className="space-y-1 p-6">
          {/* Input Mode Selector */}
          <div className="flex gap-2 pb-2">
            {(['jenkins', 'paste', 'upload'] as const).map((mode) => (
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
                {mode === 'jenkins' ? 'Jenkins Job' : mode === 'paste' ? 'Paste XML' : 'Upload File'}
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
                  onChange={(e) => setAiCliTimeout(e.target.value ? toIntInRange(e.target.value, 1, 3600, 1) : undefined)}
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
              <>
                <div className="space-y-2">
                  {peerConfigs.map((peer, i) => (
                    <div
                      key={peer.id}
                      className="bg-surface-elevated border border-border-default rounded-lg p-2.5 flex items-center gap-2"
                    >
                      <Select
                        value={peer.ai_provider}
                        onValueChange={(v) =>
                          setPeerConfigs((prev) =>
                            prev.map((p) => (p.id === peer.id ? { ...p, ai_provider: v } : p))
                          )
                        }
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
                        onChange={(e) =>
                          setPeerConfigs((prev) =>
                            prev.map((p) =>
                              p.id === peer.id ? { ...p, ai_model: e.target.value } : p
                            )
                          )
                        }
                      />
                      <button
                        type="button"
                        aria-label={`Remove peer ${i + 1}`}
                        className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
                        onClick={() => setPeerConfigs((prev) => prev.filter((p) => p.id !== peer.id))}
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
                    setPeerConfigs((prev) => [
                      ...prev,
                      { id: crypto.randomUUID(), ai_provider: 'claude', ai_model: '' },
                    ])
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
                    onChange={(e) => setMaxRounds(toIntInRange(e.target.value, 1, 10, 1))}
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
              {additionalRepos.map((repo) => (
                <div
                  key={repo.id}
                  className="bg-surface-elevated border border-border-default rounded-lg p-2.5 space-y-2"
                >
                  <div className="flex items-center gap-2">
                    <Input
                      className="w-32"
                      placeholder="Name"
                      value={repo.name}
                      onChange={(e) =>
                        setAdditionalRepos((prev) =>
                          prev.map((r) =>
                            r.id === repo.id ? { ...r, name: e.target.value } : r
                          )
                        )
                      }
                    />
                    <Input
                      className="flex-1"
                      placeholder="URL"
                      value={repo.url}
                      onChange={(e) =>
                        setAdditionalRepos((prev) =>
                          prev.map((r) =>
                            r.id === repo.id ? { ...r, url: e.target.value } : r
                          )
                        )
                      }
                    />
                    <Input
                      className="w-24"
                      placeholder="Ref"
                      value={repo.ref}
                      onChange={(e) =>
                        setAdditionalRepos((prev) =>
                          prev.map((r) =>
                            r.id === repo.id ? { ...r, ref: e.target.value } : r
                          )
                        )
                      }
                    />
                    <button
                      type="button"
                      aria-label={`Remove repository ${repo.name || repo.id}`}
                      className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
                      onClick={() =>
                        setAdditionalRepos((prev) => prev.filter((r) => r.id !== repo.id))
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
                  setAdditionalRepos((prev) => [
                    ...prev,
                    { id: crypto.randomUUID(), name: '', url: '', ref: '' },
                  ])
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
              <Toggle checked={enableJira} onChange={setEnableJira} label="Enable Jira search" />
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

          {inputMode === 'jenkins' && (
          <>
          <hr className="border-border-muted" />

          {/* Advanced */}
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
      </div>
    </div>
  )
}
