import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api, ApiError, extractApiDetail, type TestIntegrationResult } from '@/lib/api'
import { useLatestRef } from '@/lib/useLatestRef'
import { useSSE } from '@/lib/useSSE'
import { useAuth } from '@/lib/auth'
import { Card, CardContent } from '@/components/ui/card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  Search,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  ChevronsDownUp,
  Eye,
  EyeOff,
  AlertTriangle,
  RotateCcw,
  Loader2,
  Clock,
  RefreshCw,
  Zap,
} from 'lucide-react'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { ProviderSelect } from '@/components/shared/ProviderSelect'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { CursorAuthBanner } from '@/components/shared/CursorAuthBanner'
import { useProviderModels } from '@/hooks/useProviderModels'
import { useCursorAuthStatus } from '@/lib/useProviderOptions'
import { ServerProvidersCard } from '@/components/shared/ServerProvidersCard'
import { AdditionalReposList } from '@/components/shared/AdditionalReposList'
import type { RepoWithId } from '@/components/shared/AdditionalReposList'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ServerSetting {
  key: string
  env_var: string
  value: string
  default: string
  description: string
  type: 'string' | 'boolean' | 'integer'
  category: string
  sensitive: boolean
  restart_required: boolean
  source: 'default' | 'env' | 'db'
  updated_by: string
  updated_at: string
  has_env_var?: boolean
  env_var_value?: string
  options?: string[]
  jira_auth_mode?: string
}

interface CategoryGroup {
  category: string
  settings: ServerSetting[]
}

// Category display order for the tab layout (mirrors backend _SETTINGS_CATEGORIES).
const CATEGORY_ORDER = [
  'Server', 'AI', 'Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Prow',
  'Auth & Security', 'Web Push', 'Notifications', 'Sidecar',
]

// Categories with a test-connection endpoint (category -> integration id).
const TEST_INTEGRATIONS: Record<string, string> = {
  Jenkins: 'jenkins',
  Jira: 'jira',
  GitHub: 'github',
  'Report Portal': 'reportportal',
  Prow: 'prow',
}

// Jira auth methods, in backend resolution precedence order (oauth wins).
type JiraMethod = 'oauth' | 'basic' | 'pat'
const JIRA_METHODS: { id: JiraMethod; label: string; fields: string[] }[] = [
  {
    id: 'oauth',
    label: 'Atlassian OAuth (token / cloud ID)',
    fields: ['jira_oauth_access_token', 'atlassian_oauth_cloud_id', 'atlassian_oauth_client_id'],
  },
  {
    id: 'basic',
    label: 'Email + API token',
    fields: ['jira_email', 'jira_api_token'],
  },
  {
    id: 'pat',
    label: 'PAT (Bearer)',
    fields: ['jira_pat'],
  },
]

const jiraMethodLabel = (id: JiraMethod) => JIRA_METHODS.find(m => m.id === id)!.label

function hiddenJiraFieldsFor(method: JiraMethod): Set<string> {
  return new Set(JIRA_METHODS.filter(m => m.id !== method).flatMap(m => m.fields))
}

// Server-only settings (env/CLI-owned, never persistable to the DB) — the
// known set, kept alongside the backend's server-only metadata marker.
const SERVER_ONLY_SETTING_KEYS = new Set(['admin_key', 'db_path', 'debug'])

// Active Jira auth method. The backend exposes the authoritative mode on the
// jira_url settings item (`jira_auth_mode`: sa→oauth, cloud→basic, server→pat);
// the local inference below is only a fallback for older servers without it.
// The badge, initial method selection and precedence warning all derive from
// this function.
function resolveActiveJiraMethod(settings: ServerSetting[]): JiraMethod | null {
  const backendMode = settings.find(s => s.key === 'jira_url')?.jira_auth_mode
  if (backendMode === 'oauth' || backendMode === 'basic' || backendMode === 'pat') return backendMode
  const value = (key: string) => settings.find(s => s.key === key)?.value?.trim() ?? ''
  if (value('jira_oauth_access_token')) return 'oauth'
  if (value('jira_email') && value('jira_api_token')) return 'basic'
  if (value('jira_pat')) return 'pat'
  return null
}

// ---------------------------------------------------------------------------
// State reducer
// ---------------------------------------------------------------------------

interface HistoryEntry {
  id: number
  key: string
  value: string
  previous_value: string | null
  action: string
  changed_by: string
  changed_at: string
}

interface PageState {
  settings: ServerSetting[]
  loading: boolean
  error: string | null
  search: string
  collapsedCategories: Set<string>
  editingKey: string | null
  editValue: string
  saving: boolean
  saveError: string | null
  resettingKey: string | null
  revealedKeys: Set<string>
  revealedValues: Record<string, string>
  // Bumped by CLEAR_REVEALED: a reveal response whose captured generation no
  // longer matches is dropped, so a late response can't resurrect an old secret.
  revealGenerations: Record<string, number>
  historyKey: string
  history: HistoryEntry[]
  historyLoading: boolean
}

type PageAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_SUCCESS'; settings: ServerSetting[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'SET_SEARCH'; search: string }
  | { type: 'TOGGLE_CATEGORY'; category: string }
  | { type: 'START_EDIT'; key: string; currentValue: string }
  | { type: 'CANCEL_EDIT' }
  | { type: 'SET_EDIT_VALUE'; value: string }
  | { type: 'SAVE_START' }
  | { type: 'SAVE_SUCCESS' }
  | { type: 'SAVE_ERROR'; error: string }
  | { type: 'RESET_START'; key: string }
  | { type: 'RESET_DONE' }
  | { type: 'REVEAL_VALUE'; key: string; value: string; generation: number }
  | { type: 'HIDE_VALUE'; key: string }
  | { type: 'CLEAR_REVEALED'; keys: string[] }
  | { type: 'EXPAND_ALL' }
  | { type: 'COLLAPSE_ALL'; categories: string[] }
  | { type: 'SHOW_HISTORY'; key: string }
  | { type: 'HIDE_HISTORY' }
  | { type: 'SET_HISTORY'; history: HistoryEntry[] }
  | { type: 'SET_HISTORY_LOADING'; loading: boolean }

function reducer(state: PageState, action: PageAction): PageState {
  switch (action.type) {
    case 'FETCH_START':
      return { ...state, loading: true, error: null }
    case 'FETCH_SUCCESS':
      return { ...state, loading: false, settings: action.settings, error: null }
    case 'FETCH_ERROR':
      return { ...state, loading: false, error: action.error }
    case 'SET_SEARCH':
      return { ...state, search: action.search }
    case 'TOGGLE_CATEGORY': {
      const next = new Set(state.collapsedCategories)
      if (next.has(action.category)) next.delete(action.category)
      else next.add(action.category)
      return { ...state, collapsedCategories: next }
    }
    case 'START_EDIT':
      return { ...state, editingKey: action.key, editValue: action.currentValue, saveError: null }
    case 'CANCEL_EDIT':
      return { ...state, editingKey: null, editValue: '', saveError: null }
    case 'SET_EDIT_VALUE':
      return { ...state, editValue: action.value }
    case 'SAVE_START':
      return { ...state, saving: true, saveError: null }
    case 'SAVE_SUCCESS':
      return { ...state, saving: false, editingKey: null, editValue: '' }
    case 'SAVE_ERROR':
      return { ...state, saving: false, saveError: action.error }
    case 'RESET_START':
      return { ...state, resettingKey: action.key }
    case 'RESET_DONE':
      return { ...state, resettingKey: null }
    case 'REVEAL_VALUE': {
      // A reveal whose generation was invalidated (save/reset cleared the key
      // while the request was in flight) must not re-cache the old secret (#343).
      if ((state.revealGenerations[action.key] ?? 0) !== action.generation) return state
      const nextKeys = new Set(state.revealedKeys)
      nextKeys.add(action.key)
      return {
        ...state,
        revealedKeys: nextKeys,
        revealedValues: { ...state.revealedValues, [action.key]: action.value },
      }
    }
    case 'HIDE_VALUE': {
      const nextKeys = new Set(state.revealedKeys)
      nextKeys.delete(action.key)
      const revealedValues = { ...state.revealedValues }
      delete revealedValues[action.key]
      return { ...state, revealedKeys: nextKeys, revealedValues }
    }
    case 'CLEAR_REVEALED': {
      // Saved/reset values invalidate cached reveals — the row must fall back to
      // the fresh (masked) refetched value until the user reveals again (#343).
      const keys = new Set(action.keys)
      const nextKeys = new Set([...state.revealedKeys].filter(k => !keys.has(k)))
      const revealedValues = { ...state.revealedValues }
      const revealGenerations = { ...state.revealGenerations }
      for (const k of keys) {
        delete revealedValues[k]
        revealGenerations[k] = (revealGenerations[k] ?? 0) + 1
      }
      return { ...state, revealedKeys: nextKeys, revealedValues, revealGenerations }
    }
    case 'EXPAND_ALL':
      return { ...state, collapsedCategories: new Set<string>() }
    case 'COLLAPSE_ALL':
      return { ...state, collapsedCategories: new Set<string>(action.categories) }
    case 'SHOW_HISTORY':
      return { ...state, historyKey: action.key, history: [], historyLoading: true }
    case 'HIDE_HISTORY':
      return { ...state, historyKey: '', history: [], historyLoading: false }
    case 'SET_HISTORY':
      return { ...state, history: action.history, historyLoading: false }
    case 'SET_HISTORY_LOADING':
      return { ...state, historyLoading: action.loading }
    default:
      return state
  }
}

const initialState: PageState = {
  settings: [],
  loading: true,
  error: null,
  search: '',
  collapsedCategories: new Set(),
  editingKey: null,
  editValue: '',
  saving: false,
  saveError: null,
  resettingKey: null,
  revealedKeys: new Set(),
  revealedValues: {},
  revealGenerations: {},
  historyKey: '',
  history: [],
  historyLoading: false,
}

// ---------------------------------------------------------------------------
// Parse / serialize helpers
// ---------------------------------------------------------------------------

function splitOutsideBrackets(raw: string): string[] {
  const parts: string[] = []
  let current = ''
  let depth = 0
  for (const ch of raw) {
    if (ch === '[') { depth++; current += ch }
    else if (ch === ']') { depth--; current += ch }
    else if (ch === ',' && depth === 0) { parts.push(current); current = '' }
    else { current += ch }
  }
  if (current) parts.push(current)
  return parts
}

function parsePeerConfigString(raw: string): PeerConfigWithId[] {
  if (!raw || !raw.trim()) return []
  const result: PeerConfigWithId[] = []
  for (const entry of splitOutsideBrackets(raw)) {
    const trimmed = entry.trim()
    if (!trimmed.includes(':')) continue
    const [provider, ...modelParts] = trimmed.split(':')
    const ai_provider = provider.trim()
    const ai_model = modelParts.join(':').trim()
    if (!ai_provider || !ai_model) continue
    result.push({ id: crypto.randomUUID(), ai_provider, ai_model })
  }
  return result
}

function serializePeerConfigs(configs: PeerConfigWithId[]): string {
  return configs
    .filter(c => c.ai_provider && c.ai_model)
    .map(c => `${c.ai_provider}:${c.ai_model}`)
    .join(',')
}

function parseAdditionalReposString(raw: string): RepoWithId[] {
  if (!raw || !raw.trim()) return []
  const result: RepoWithId[] = []
  for (const entry of raw.split(',')) {
    const trimmed = entry.trim()
    if (!trimmed.includes(':')) continue
    const colonIdx = trimmed.indexOf(':')
    const name = trimmed.slice(0, colonIdx).trim()
    if (!name) continue
    const urlAndRef = trimmed.slice(colonIdx + 1).trim()

    // URL contains :// — find the path portion after the netloc
    // Format: https://host/org/repo or https://host/org/repo:ref
    // The ref is after the LAST colon in the path (not in the scheme)
    let url = urlAndRef
    let ref = ''

    // Find the end of scheme+netloc (after ://)
    const schemeEnd = urlAndRef.indexOf('://')
    if (schemeEnd >= 0) {
      const pathStart = urlAndRef.indexOf('/', schemeEnd + 3)
      if (pathStart >= 0) {
        const pathPortion = urlAndRef.slice(pathStart)
        const lastColon = pathPortion.lastIndexOf(':')
        if (lastColon > 0) {
          // There's a colon in the path — it's the ref separator
          ref = pathPortion.slice(lastColon + 1)
          url = urlAndRef.slice(0, pathStart + lastColon)
        }
      }
    }

    result.push({ id: crypto.randomUUID(), name, url, ref })
  }
  return result
}

function serializeAdditionalRepos(repos: RepoWithId[]): string {
  return repos
    .filter(r => r.name.trim() && r.url.trim())
    .map(r => {
      let entry = `${r.name.trim()}:${r.url.trim()}`
      if (r.ref.trim()) entry += `:${r.ref.trim()}`
      return entry
    })
    .join(',')
}

// Empty value means ALL server providers are active (backward compatible).
// The backend matches provider IDs case-insensitively, so canonicalize each
// saved ID to its catalog spelling; unknown IDs are preserved as-is.
function canonicalProvider(id: string, options: string[]): string {
  return options.find(o => o.toLowerCase() === id.toLowerCase()) ?? id
}

function parseActiveServerProviders(raw: string, options: string[]): string[] {
  if (!raw.trim()) return [...options]
  return [...new Set(raw.split(',').map(s => canonicalProvider(s.trim(), options)).filter(Boolean))]
}

// ---------------------------------------------------------------------------
// Source badge
// ---------------------------------------------------------------------------

function SourceBadge({ source }: { source: 'default' | 'env' | 'db' }) {
  const styles: Record<string, string> = {
    default: 'bg-surface-elevated text-text-tertiary',
    env: 'bg-signal-blue/10 text-signal-blue',
    db: 'bg-signal-green/10 text-signal-green',
  }
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[source]}`}>
      {source}
    </span>
  )
}

function SaveToDbButton({ saving, onClick }: { saving: boolean; onClick: () => void }) {
  return (
    <Button
      size="sm"
      variant="ghost"
      onClick={onClick}
      disabled={saving}
      className="h-6 px-2 text-xs text-text-tertiary hover:text-text-secondary"
    >
      {saving && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
      Save to DB
    </Button>
  )
}

function EnvVarWarning({ setting }: { setting: ServerSetting }) {
  if (setting.source !== 'db' || !setting.has_env_var) return null
  const valuesMatch = setting.env_var_value !== undefined && setting.value === setting.env_var_value
  return (
    <Tooltip>
      <TooltipTrigger>
        <AlertTriangle className="h-3.5 w-3.5 text-signal-amber" />
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs">
        {valuesMatch ? (
          <p className="text-xs">Environment variable <code className="font-mono">{setting.env_var}={setting.env_var_value}</code> is set to the same value. This DB override is redundant — consider resetting it to use the environment variable directly.</p>
        ) : setting.env_var_value === undefined ? (
          <p className="text-xs">Environment variable <code className="font-mono">{setting.env_var}</code> is also set (value hidden). The server setting (DB) takes priority, but consider removing the environment variable to avoid confusion.</p>
        ) : (
          <p className="text-xs">Environment variable <code className="font-mono">{setting.env_var}={setting.env_var_value}</code> is also set. The server setting (DB) takes priority, but consider removing the environment variable to avoid confusion.</p>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ServerSettingsPage() {
  const { isAdmin } = useAuth()
  const [state, dispatch] = useReducer(reducer, initialState)

  // Peer config structured editor state
  const [peerConfigs, setPeerConfigs] = useState<PeerConfigWithId[]>([])
  const [peerMaxRounds, setPeerMaxRounds] = useState(3)
  const [peerEditing, setPeerEditing] = useState(false)

  // AI default provider/model editing
  const [aiProviderEditing, setAiProviderEditing] = useState(false)
  const [aiProviderValue, setAiProviderValue] = useState('')
  const [aiModelEditing, setAiModelEditing] = useState(false)
  const [aiModelValue, setAiModelValue] = useState('')
  // For the model combobox, use the editing provider if editing, otherwise the saved setting value
  const savedAiProvider = state.settings.find(s => s.key === 'ai_provider')?.value || ''
  const savedAiModel = state.settings.find(s => s.key === 'ai_model')?.value || ''
  const effectiveAiProvider = aiProviderEditing
    ? aiProviderValue
    : (savedAiProvider || aiProviderValue)
  const { models: aiModels, refresh: refreshModels, refreshing: refreshingModels } = useProviderModels(effectiveAiProvider)
  const cursorAuthStatus = useCursorAuthStatus()

  // Additional repos structured editor state
  const [additionalRepos, setAdditionalRepos] = useState<RepoWithId[]>([])
  const [reposEditing, setReposEditing] = useState(false)

  // Active server providers editor state
  const [serverProvidersEditing, setServerProvidersEditing] = useState(false)
  const [serverProvidersChecked, setServerProvidersChecked] = useState<string[]>([])

  // Active settings tab (persisted across reloads)
  const [activeTab, setActiveTab] = useState<string>(() => {
    try {
      return localStorage.getItem('rootcoz_settings_tab') || ''
    } catch {
      return ''
    }
  })

  // Jira auth method selection (presentational grouping only)
  const [jiraMethod, setJiraMethod] = useState<JiraMethod | null>(null)

  // Test connection state
  const [testingCategory, setTestingCategory] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ category: string; result: TestIntegrationResult } | null>(null)

  // Save-response warnings (e.g. the non-loopback sidecar_host trust warning).
  const [saveWarning, setSaveWarning] = useState<string | null>(null)

  // "Save to DB" in-flight key (persisting an env-sourced setting into the DB).
  const [persistingKey, setPersistingKey] = useState<string | null>(null)

  // Debounced search
  const [searchInput, setSearchInput] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => dispatch({ type: 'SET_SEARCH', search: searchInput }), 200)
    return () => clearTimeout(timer)
  }, [searchInput])

  // ---- Fetch settings ----
  // silent=true skips the loading skeleton (used after save/reset: keep the
  // already-rendered content mounted so the scroll position is preserved).
  const fetchSettings = useCallback(async (silent = false) => {
    if (!silent) dispatch({ type: 'FETCH_START' })
    try {
      const data = await api.get<ServerSetting[]>('/api/admin/settings')
      dispatch({ type: 'FETCH_SUCCESS', settings: data })
      return data
    } catch (err) {
      dispatch({
        type: 'FETCH_ERROR',
        error: err instanceof Error ? err.message : 'Failed to load settings',
      })
    }
  }, [])

  const fetchSettingsRef = useLatestRef(fetchSettings)

  useEffect(() => {
    fetchSettings()
  }, [fetchSettings])

  // ---- Multiplexed SSE ----
  const settingsEvents = useMemo(() => ({
    'settings-changed': () => {
      // Settings are already rendered for any listener of this stream —
      // refetch silently to avoid the loading skeleton collapsing the page.
      fetchSettingsRef.current(true)
    },
  }), [fetchSettingsRef])

  useSSE('settings', settingsEvents)

  // ---- Shared save helper ----
  async function saveSettings(updates: Record<string, string>): Promise<boolean> {
    dispatch({ type: 'SAVE_START' })
    setSaveWarning(null)
    try {
      const result = await api.put<{ warnings?: string[] }>('/api/admin/settings', { settings: updates })
      // Keep controls disabled until the refetched values are rendered —
      // otherwise a re-opened editor can save the stale value over the fresh one.
      await fetchSettings(true)
      // A saved key's cached reveal is stale — clear it so the fresh masked value shows (#343).
      dispatch({ type: 'CLEAR_REVEALED', keys: Object.keys(updates) })
      dispatch({ type: 'SAVE_SUCCESS' })
      if (result?.warnings?.length) setSaveWarning(result.warnings.join(' '))
      return true
    } catch (err) {
      let msg = 'Failed to save'
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        msg = body?.detail ?? `Save failed (${err.status})`
      }
      dispatch({ type: 'SAVE_ERROR', error: msg })
      return false
    }
  }

  async function saveSettingValue(key: string, value: string): Promise<boolean> {
    return saveSettings({ [key]: value })
  }

  // ---- Save setting ----
  async function handleSave(key: string, value: string) {
    await saveSettingValue(key, value)
  }

  // ---- Reset setting ----
  async function handleReset(key: string) {
    dispatch({ type: 'RESET_START', key })
    try {
      await api.delete(`/api/admin/settings/${encodeURIComponent(key)}`)
      const fresh = await fetchSettings(true)
      dispatch({ type: 'CLEAR_REVEALED', keys: [key] })
      // A reset while the provider editor is open must not leave the pre-reset
      // checkbox selection saveable over the fresh value (#339/#340).
      if (key === 'active_server_providers') {
        const setting = fresh?.find(s => s.key === key)
        setServerProvidersChecked(parseActiveServerProviders(setting?.value ?? '', setting?.options ?? []))
        setServerProvidersEditing(false)
      }
    } catch {
      // silently fail – data will refetch anyway
    } finally {
      dispatch({ type: 'RESET_DONE' })
    }
  }

  // ---- Boolean toggle (save immediately) ----
  async function handleBooleanToggle(key: string, currentValue: string) {
    const newValue = currentValue === 'true' ? 'false' : 'true'
    await saveSettingValue(key, newValue)
  }

  // ---- Persist an env-sourced setting into the DB ----
  async function handlePersistEnv(key: string) {
    setPersistingKey(key)
    try {
      await api.persistSetting(key)
      await fetchSettings(true)
    } catch (err) {
      const msg = err instanceof ApiError
        ? extractApiDetail(err) ?? `Save to DB failed (${err.status})`
        : 'Failed to save to DB'
      dispatch({ type: 'SAVE_ERROR', error: msg })
    } finally {
      setPersistingKey(null)
    }
  }

  // ---- Peer config structured editor ----
  function handleStartPeerEdit(setting: ServerSetting) {
    setPeerConfigs(parsePeerConfigString(setting.value))
    const maxRoundsSetting = state.settings.find(s => s.key === 'peer_analysis_max_rounds')
    setPeerMaxRounds(maxRoundsSetting ? parseInt(maxRoundsSetting.value) || 3 : 3)
    setPeerEditing(true)
  }

  async function handleSavePeerConfig() {
    const configStr = serializePeerConfigs(peerConfigs)
    const updates: Record<string, string> = { peer_ai_configs: configStr }
    updates.peer_analysis_max_rounds = String(peerMaxRounds)
    const ok = await saveSettings(updates)
    if (ok) setPeerEditing(false)  // Only close on success
  }

  async function handleSaveAiProvider(value: string) {
    const previousProvider = savedAiProvider
    if (value !== previousProvider) {
      // Provider changed: require a model and persist both atomically so we never
      // delete ai_model (empty → env fallback) or leave a mismatched pair.
      const model = aiModelValue.trim()
      if (!model) {
        setAiModelEditing(true)
        dispatch({
          type: 'SAVE_ERROR',
          error: 'Select a model for the new provider before saving.',
        })
        return
      }
      const ok = await saveSettings({ ai_provider: value, ai_model: model })
      if (ok) {
        setAiProviderEditing(false)
        setAiModelEditing(false)
        setAiProviderValue(value)
      }
      return
    }
    const ok = await saveSettingValue('ai_provider', value)
    if (ok) {
      setAiProviderEditing(false)
      setAiProviderValue(value)
    }
  }

  async function handleSaveAiModel(value: string) {
    const providerDraft = (aiProviderValue || savedAiProvider).trim()
    if (providerDraft && providerDraft !== savedAiProvider) {
      const model = value.trim()
      if (!model) {
        dispatch({
          type: 'SAVE_ERROR',
          error: 'Select a model for the new provider before saving.',
        })
        return
      }
      const ok = await saveSettings({ ai_provider: providerDraft, ai_model: model })
      if (ok) {
        setAiProviderEditing(false)
        setAiModelEditing(false)
        setAiProviderValue(providerDraft)
      }
      return
    }
    const ok = await saveSettingValue('ai_model', value)
    if (ok) setAiModelEditing(false)
  }

  // ---- Additional repos structured editor ----
  function handleStartReposEdit(setting: ServerSetting) {
    setAdditionalRepos(parseAdditionalReposString(setting.value))
    setReposEditing(true)
  }

  async function handleSaveRepos() {
    const reposStr = serializeAdditionalRepos(additionalRepos)
    const ok = await saveSettingValue('additional_repos', reposStr)
    if (ok) setReposEditing(false)
  }

  // ---- Active server providers editor ----
  const serverProviderOptions = state.settings.find(s => s.key === 'active_server_providers')?.options ?? []

  async function handleSaveServerProviders() {
    // '' means all providers active: save it when nothing is checked (footgun warned
    // inline) or when every available option is checked (restore default semantics).
    const isAll = serverProviderOptions.length > 0 &&
      serverProvidersChecked.length === serverProviderOptions.length &&
      serverProviderOptions.every(o => serverProvidersChecked.includes(o))
    const value = serverProvidersChecked.length === 0 || isAll ? '' : serverProvidersChecked.join(',')
    const ok = await saveSettingValue('active_server_providers', value)
    if (ok) setServerProvidersEditing(false)
  }

  // ---- Toggle reveal (fetch real value from server) ----
  async function handleToggleReveal(key: string) {
    if (state.revealedKeys.has(key)) {
      dispatch({ type: 'HIDE_VALUE', key })
      return
    }
    try {
      // Capture the generation before awaiting: if a save/reset clears this key
      // while the GET is in flight, the reducer drops this stale response.
      const generation = state.revealGenerations[key] ?? 0
      const data = await api.get<ServerSetting[]>(
        `/api/admin/settings?reveal_key=${encodeURIComponent(key)}`
      )
      const item = data.find(s => s.key === key)
      if (item) {
        dispatch({ type: 'REVEAL_VALUE', key, value: item.value, generation })
      }
    } catch {
      // Silently fail — can't reveal
    }
  }

  // ---- Show history ----
  const historyKeyRef = useRef('')

  async function handleShowHistory(key: string) {
    if (state.historyKey === key) {
      dispatch({ type: 'HIDE_HISTORY' })
      historyKeyRef.current = ''
      return
    }
    dispatch({ type: 'SHOW_HISTORY', key })
    historyKeyRef.current = key
    try {
      const data = await api.get<HistoryEntry[]>(
        `/api/admin/settings/history?key=${encodeURIComponent(key)}&limit=20`
      )
      // Only update if this is still the active history request
      if (historyKeyRef.current === key) {
        dispatch({ type: 'SET_HISTORY', history: data })
      }
    } catch {
      if (historyKeyRef.current === key) {
        dispatch({ type: 'SET_HISTORY', history: [] })
      }
    }
  }

  // ---- Group & filter ----
  const allGroups: CategoryGroup[] = useMemo(() => {
    const categoryMap = new Map<string, ServerSetting[]>()
    for (const s of state.settings) {
      const cat = s.category || 'General'
      if (!categoryMap.has(cat)) categoryMap.set(cat, [])
      categoryMap.get(cat)!.push(s)
    }

    const order = (cat: string) => {
      const idx = CATEGORY_ORDER.indexOf(cat)
      return idx === -1 ? CATEGORY_ORDER.length : idx
    }
    return Array.from(categoryMap.entries())
      .sort(([a], [b]) => order(a) - order(b) || a.localeCompare(b))
      .map(([category, settings]) => ({ category, settings }))
  }, [state.settings])

  const groups: CategoryGroup[] = useMemo(() => {
    const query = state.search.toLowerCase()
    if (!query) return allGroups
    const matches = (s: ServerSetting) =>
      s.key.toLowerCase().includes(query) ||
      s.env_var.toLowerCase().includes(query) ||
      s.description.toLowerCase().includes(query)
    return allGroups
      .map(({ category, settings }) => ({ category, settings: settings.filter(matches) }))
      .filter(g => g.settings.length > 0)
  }, [allGroups, state.search])

  // ---- Jira auth method (presentational grouping, same precedence as backend) ----
  const activeJiraMethod = resolveActiveJiraMethod(state.settings)
  const effectiveJiraMethod = jiraMethod ?? activeJiraMethod ?? 'basic'
  const jiraHiddenFields = hiddenJiraFieldsFor(effectiveJiraMethod)
  // Editing a lower-precedence method while higher-precedence credentials are stored
  const jiraPrecedenceWarning =
    activeJiraMethod &&
    effectiveJiraMethod !== activeJiraMethod &&
    JIRA_METHODS.findIndex(m => m.id === effectiveJiraMethod) >
      JIRA_METHODS.findIndex(m => m.id === activeJiraMethod)
      ? activeJiraMethod
      : null

  // ---- Test connection helpers ----
  const editedCategory = state.editingKey
    ? state.settings.find(s => s.key === state.editingKey)?.category ?? null
    : null
  // Generation guard: a test resolving after a tab change must not re-show
  // its stale banner when the admin returns to that tab.
  const testGenerationRef = useRef(0)

  async function handleTestConnection(category: string) {
    const integration = TEST_INTEGRATIONS[category]
    if (!integration) return
    const generation = ++testGenerationRef.current
    setTestResult(null)
    setTestingCategory(category)
    try {
      const result = await api.testIntegration(integration)
      if (generation === testGenerationRef.current) setTestResult({ category, result })
    } catch (err) {
      if (generation === testGenerationRef.current) {
        setTestResult({
          category,
          result: { ok: false, message: err instanceof Error ? err.message : 'Test failed' },
        })
      }
    } finally {
      setTestingCategory(null)
    }
  }

  function handleTabChange(tab: string) {
    testGenerationRef.current++ // invalidate in-flight test results
    setActiveTab(tab)
    setTestResult(null) // dismiss test banners on tab change
    try {
      localStorage.setItem('rootcoz_settings_tab', tab)
    } catch {
      // ignore persistence failures
    }
  }

  const effectiveTab = allGroups.some(g => g.category === activeTab)
    ? activeTab
    : allGroups[0]?.category ?? ''

  // ---- Per-category card (shared by the tab view and search results) ----
  function renderCategory(category: string, settings: ServerSetting[]) {
    const isCollapsed = state.collapsedCategories.has(category)
    // "Save to DB" affordance for env-sourced rows (null for db/default rows
    // and for server-only settings the backend owns exclusively).
    const saveToDbNode = (s: ServerSetting) =>
      s.source === 'env' && !SERVER_ONLY_SETTING_KEYS.has(s.key) ? (
        <SaveToDbButton saving={persistingKey === s.key} onClick={() => handlePersistEnv(s.key)} />
      ) : null
    // Search must be able to find every Jira field, so auth-method hiding
    // only applies in the tab view.
    const rowSettings = category === 'Jira' && !state.search
      ? settings.filter(s => !jiraHiddenFields.has(s.key))
      : settings
    return (
      <Card key={category}>
        <div className="flex w-full items-center justify-between gap-2 px-4 py-3">
          <button
            type="button"
            onClick={() => dispatch({ type: 'TOGGLE_CATEGORY', category })}
            className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 text-left transition-colors hover:bg-surface-hover"
            aria-expanded={!isCollapsed}
          >
            {isCollapsed ? (
              <ChevronRight className="h-4 w-4 shrink-0 text-text-tertiary" />
            ) : (
              <ChevronDown className="h-4 w-4 shrink-0 text-text-tertiary" />
            )}
            <span className="font-display text-sm font-semibold text-text-primary">
              {category}
            </span>
            <span className="text-xs text-text-tertiary">
              ({settings.length} {settings.length === 1 ? 'setting' : 'settings'})
            </span>
          </button>
          {TEST_INTEGRATIONS[category] && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex shrink-0">
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-xs"
                    onClick={() => handleTestConnection(category)}
                    disabled={testingCategory === category || editedCategory === category}
                  >
                    {testingCategory === category ? (
                      <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Testing…</>
                    ) : (
                      <><Zap className="mr-1 h-3 w-3" />Test connection</>
                    )}
                  </Button>
                </span>
              </TooltipTrigger>
              {editedCategory === category && <TooltipContent>Save changes before testing</TooltipContent>}
            </Tooltip>
          )}
        </div>
        {testResult?.category === category && <TestResultBanner result={testResult.result} />}
        {!isCollapsed && (
          <CardContent className="px-4 pb-4 pt-0">
            {category === 'Jira' && (
              <JiraAuthMethodControl
                selected={effectiveJiraMethod}
                onSelect={(method) => {
                  // Cancel any open edit on a field the new method hides —
                  // otherwise the Jira test button stays disabled with no
                  // visible editor to save or cancel.
                  if (state.editingKey && hiddenJiraFieldsFor(method).has(state.editingKey)) {
                    dispatch({ type: 'CANCEL_EDIT' })
                  }
                  setJiraMethod(method)
                }}
                activeMethod={activeJiraMethod}
                warningMethod={jiraPrecedenceWarning}
              />
            )}
            {category === 'AI' && cursorAuthStatus && (
              <div className="mb-3">
                <CursorAuthBanner status={cursorAuthStatus} />
              </div>
            )}
            <div className="divide-y divide-border-default">
              {rowSettings.map((setting) => {
                if (setting.key === 'ai_provider') {
                  return (
                    <AiSettingRow
                      key={setting.key}
                      setting={setting}
                      editing={aiProviderEditing}
                      saveToDbNode={saveToDbNode(setting)}
                      onStartEdit={(v) => { setAiProviderValue(v); setAiProviderEditing(true) }}
                      onCancel={() => {
                        setAiProviderEditing(false)
                        setAiProviderValue(savedAiProvider)
                        setAiModelValue(savedAiModel)
                      }}
                      onSave={() => handleSaveAiProvider(aiProviderValue)}
                      onReset={() => handleReset(setting.key)}
                      saving={state.saving}
                      historyKey={state.historyKey}
                      history={state.history}
                      historyLoading={state.historyLoading}
                      onShowHistory={() => handleShowHistory(setting.key)}
                      configureLabel="Configure provider"
                      notConfiguredLabel="Not configured (uses AI_PROVIDER env var)"
                    >
                      <ProviderSelect
                        showCredentialSources
                        value={aiProviderValue}
                        onChange={(v) => {
                          setAiProviderValue(v)
                          setAiModelValue('')
                        }}
                      />
                    </AiSettingRow>
                  )
                }

                if (setting.key === 'ai_model') {
                  return (
                    <AiSettingRow
                      key={setting.key}
                      setting={setting}
                      editing={aiModelEditing}
                      saveToDbNode={saveToDbNode(setting)}
                      onStartEdit={(v) => { setAiModelValue(v); setAiModelEditing(true) }}
                      onCancel={() => setAiModelEditing(false)}
                      onSave={() => handleSaveAiModel(aiModelValue)}
                      onReset={() => handleReset(setting.key)}
                      saving={state.saving}
                      historyKey={state.historyKey}
                      history={state.history}
                      historyLoading={state.historyLoading}
                      onShowHistory={() => handleShowHistory(setting.key)}
                      configureLabel="Configure model"
                      notConfiguredLabel="Not configured (uses AI_MODEL env var)"
                    >
                      <div className="flex items-center gap-2">
                        <ModelCombobox
                          value={aiModelValue}
                          onChange={setAiModelValue}
                          options={aiModels}
                          placeholder="Select model..."
                          className="max-w-md"
                        />
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={refreshModels}
                              disabled={refreshingModels || !effectiveAiProvider}
                              className="h-8 w-8 shrink-0"
                            >
                              <RefreshCw className={`h-4 w-4 ${refreshingModels ? 'animate-spin' : ''}`} />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Refresh models from sidecar</TooltipContent>
                        </Tooltip>
                      </div>
                      {!effectiveAiProvider && (
                        <p className="text-xs text-signal-amber">Select a provider first to see available models</p>
                      )}
                    </AiSettingRow>
                  )
                }

                // ---- ACTIVE_SERVER_PROVIDERS: provider allowlist editor ----
                if (setting.key === 'active_server_providers') {
                  const options = setting.options ?? []
                  const savedActive = parseActiveServerProviders(setting.value, options)
                  return (
                    <div key={setting.key} className="py-3 space-y-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
                        <SourceBadge source={setting.source} />
                        {saveToDbNode(setting)}
                        <EnvVarWarning setting={setting} />
                      </div>
                      {serverProvidersEditing ? (
                        <div className="space-y-3">
                          {options.length === 0 ? (
                            <p className="text-xs text-text-tertiary italic">No server providers available</p>
                          ) : (
                            <div className="space-y-1">
                              {options.map((provider) => (
                                <label key={provider} className="flex items-center gap-2 text-xs">
                                  <input
                                    type="checkbox"
                                    checked={serverProvidersChecked.includes(provider)}
                                    onChange={() =>
                                      setServerProvidersChecked(prev =>
                                        prev.includes(provider)
                                          ? prev.filter(p => p !== provider)
                                          : [...prev, provider],
                                      )
                                    }
                                    className="h-3.5 w-3.5 accent-signal-blue"
                                    aria-label={provider}
                                  />
                                  <span className={`font-mono ${serverProvidersChecked.includes(provider) ? 'text-text-primary' : 'text-text-tertiary line-through'}`}>
                                    {provider}
                                  </span>
                                </label>
                              ))}
                            </div>
                          )}
                          {options.length > 0 && serverProvidersChecked.length === 0 && (
                            <p className="flex items-center gap-1.5 text-xs text-signal-amber">
                              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                              No providers selected — saving this activates ALL server providers (an empty value means all).
                            </p>
                          )}
                          <div className="flex items-center gap-2 pt-1">
                            <Button size="sm" onClick={handleSaveServerProviders} disabled={state.saving} className="h-7 text-xs">
                              {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => setServerProvidersEditing(false)} className="h-7 text-xs">Cancel</Button>
                            {options.length > 0 && (
                              <Button size="sm" variant="ghost" onClick={() => setServerProvidersChecked([...options])} className="h-7 text-xs text-text-tertiary">
                                Reset to all
                              </Button>
                            )}
                            {setting.source === 'db' && (
                              <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                                <RotateCcw className="mr-1 h-3 w-3" /> Reset
                              </Button>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div>
                          {options.length === 0 ? (
                            setting.value.trim() ? (
                              <div className="space-y-1.5">
                                <span className="text-xs text-text-secondary">
                                  Restricted to: <span className="font-mono">{setting.value}</span>
                                </span>
                                <p className="flex items-center gap-1.5 text-xs text-signal-amber">
                                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                                  Provider discovery is currently unavailable — the stored restriction above stays in effect.
                                </p>
                                {setting.source === 'db' && (
                                  <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} disabled={state.resettingKey === setting.key} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                                    <RotateCcw className="mr-1 h-3 w-3" /> Reset
                                  </Button>
                                )}
                              </div>
                            ) : (
                              <span className="text-xs text-text-tertiary italic">No server providers available</span>
                            )
                          ) : !setting.value.trim() ? (
                            <span className="text-xs text-text-secondary">
                              All server providers are active <span className="text-text-quaternary">(empty value = all)</span>
                            </span>
                          ) : (
                            <div className="flex flex-wrap gap-1.5">
                              {options.map((provider) => {
                                const active = savedActive.includes(provider)
                                return (
                                  <span
                                    key={provider}
                                    className={`inline-flex items-center rounded-md bg-surface-elevated px-2 py-0.5 text-xs font-mono border border-border-default ${active ? 'text-text-primary' : 'text-text-tertiary line-through'}`}
                                  >
                                    {provider}
                                  </span>
                                )
                              })}
                            </div>
                          )}
                          {options.length > 0 && (
                            <button type="button" onClick={() => { setServerProvidersChecked(savedActive); setServerProvidersEditing(true) }} className="mt-1.5 text-xs text-text-link hover:text-signal-blue font-medium">
                              Configure providers
                            </button>
                          )}
                        </div>
                      )}
                      <p className="text-xs text-text-tertiary">{setting.description}</p>
                      <p className="text-xs text-text-quaternary">Unchecked providers are hidden from users (no server-credential models). Users&rsquo; own API keys are unaffected.</p>
                      {setting.source === 'db' && setting.updated_by && (
                        <p className="text-xs text-text-quaternary">Modified by {setting.updated_by}</p>
                      )}
                      {setting.source === 'db' && (
                        <HistoryToggle
                          settingKey={setting.key}
                          sensitive={setting.sensitive}
                          historyKey={state.historyKey}
                          history={state.history}
                          historyLoading={state.historyLoading}
                          onShowHistory={() => handleShowHistory(setting.key)}
                        />
                      )}
                    </div>
                  )
                }

                // ---- PEER_AI_CONFIGS: structured editor ----
                if (setting.key === 'peer_ai_configs') {
                  return (
                    <div key={setting.key} className="py-3 space-y-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
                        <SourceBadge source={setting.source} />
                        {saveToDbNode(setting)}
                        <EnvVarWarning setting={setting} />
                      </div>
                      {peerEditing ? (
                        <div className="space-y-3">
                          <PeerConfigList
                            peerConfigs={peerConfigs}
                            setPeerConfigs={setPeerConfigs}
                            maxRounds={peerMaxRounds}
                            setMaxRounds={setPeerMaxRounds}
                          />
                          <div className="flex items-center gap-2 pt-1">
                            <Button size="sm" onClick={handleSavePeerConfig} disabled={state.saving} className="h-7 text-xs">
                              {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => setPeerEditing(false)} className="h-7 text-xs">Cancel</Button>
                            {setting.source === 'db' && (
                              <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                                <RotateCcw className="mr-1 h-3 w-3" /> Reset
                              </Button>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div>
                          {setting.value ? (
                            <div className="flex flex-wrap gap-1.5">
                              {splitOutsideBrackets(setting.value).map((entry, i) => {
                                const trimmed = entry.trim()
                                const colonIdx = trimmed.indexOf(':')
                                const provider = colonIdx > 0 ? trimmed.slice(0, colonIdx) : trimmed
                                const model = colonIdx > 0 ? trimmed.slice(colonIdx + 1) : ''
                                return (
                                  <span key={i} className="inline-flex items-center gap-1 rounded-md bg-surface-elevated px-2 py-0.5 text-xs font-mono border border-border-default">
                                    <span className="text-text-tertiary">{provider}</span>
                                    <span className="text-text-tertiary">/</span>
                                    <span className="text-text-primary">{model}</span>
                                  </span>
                                )
                              })}
                            </div>
                          ) : (
                            <span className="text-xs text-text-tertiary italic">Not configured</span>
                          )}
                          <button type="button" onClick={() => handleStartPeerEdit(setting)} className="mt-1.5 text-xs text-text-link hover:text-signal-blue font-medium">
                            Configure peers
                          </button>
                        </div>
                      )}
                      <p className="text-xs text-text-tertiary">{setting.description}</p>
                      {setting.source === 'db' && setting.updated_by && (
                        <p className="text-xs text-text-quaternary">Modified by {setting.updated_by}</p>
                      )}
                      {setting.source === 'db' && (
                        <HistoryToggle
                          settingKey={setting.key}
                          sensitive={setting.sensitive}
                          historyKey={state.historyKey}
                          history={state.history}
                          historyLoading={state.historyLoading}
                          onShowHistory={() => handleShowHistory(setting.key)}
                        />
                      )}
                    </div>
                  )
                }

                // ---- PEER_ANALYSIS_MAX_ROUNDS: managed by PeerConfigList ----
                if (setting.key === 'peer_analysis_max_rounds') {
                  return (
                    <div key={setting.key} className="py-3 space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
                        <SourceBadge source={setting.source} />
                        {saveToDbNode(setting)}
                        <EnvVarWarning setting={setting} />
                      </div>
                      <p className="text-xs text-text-tertiary">
                        Managed via Peer AI Configs above. Current: <span className="font-mono font-medium text-text-primary">{setting.value || '3'}</span>
                      </p>
                    </div>
                  )
                }

                // ---- ADDITIONAL_REPOS: structured editor ----
                if (setting.key === 'additional_repos') {
                  return (
                    <div key={setting.key} className="py-3 space-y-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
                        <SourceBadge source={setting.source} />
                        {saveToDbNode(setting)}
                        <EnvVarWarning setting={setting} />
                      </div>
                      {reposEditing ? (
                        <div className="space-y-3">
                          <AdditionalReposList repos={additionalRepos} setRepos={setAdditionalRepos} />
                          <div className="flex items-center gap-2 pt-1">
                            <Button size="sm" onClick={handleSaveRepos} disabled={state.saving} className="h-7 text-xs">
                              {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => setReposEditing(false)} className="h-7 text-xs">Cancel</Button>
                            {setting.source === 'db' && (
                              <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                                <RotateCcw className="mr-1 h-3 w-3" /> Reset
                              </Button>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div>
                          {setting.value ? (
                            <div className="space-y-1">
                              {parseAdditionalReposString(setting.value).map((repo) => (
                                <div key={repo.id} className="inline-flex items-center gap-2 rounded-md bg-surface-elevated px-2 py-1 text-xs font-mono border border-border-default mr-1.5">
                                  <span className="font-semibold text-text-primary">{repo.name}</span>
                                  <span className="text-text-tertiary truncate max-w-[300px]">{repo.url}</span>
                                  {repo.ref && <span className="text-signal-blue">{repo.ref}</span>}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <span className="text-xs text-text-tertiary italic">Not configured</span>
                          )}
                          <button type="button" onClick={() => handleStartReposEdit(setting)} className="mt-1.5 text-xs text-text-link hover:text-signal-blue font-medium">
                            Configure repositories
                          </button>
                        </div>
                      )}
                      <p className="text-xs text-text-tertiary">{setting.description}</p>
                      {setting.source === 'db' && setting.updated_by && (
                        <p className="text-xs text-text-quaternary">Modified by {setting.updated_by}</p>
                      )}
                      {setting.source === 'db' && (
                        <HistoryToggle
                          settingKey={setting.key}
                          sensitive={setting.sensitive}
                          historyKey={state.historyKey}
                          history={state.history}
                          historyLoading={state.historyLoading}
                          onShowHistory={() => handleShowHistory(setting.key)}
                        />
                      )}
                    </div>
                  )
                }

                // ---- Default: generic SettingRow ----
                return (
                  <SettingRow
                    key={setting.key}
                    setting={setting}
                    saveToDbNode={saveToDbNode(setting)}
                    isEditing={state.editingKey === setting.key}
                    editValue={state.editValue}
                    saving={state.saving}
                    resetting={state.resettingKey === setting.key}
                    revealed={state.revealedKeys.has(setting.key)}
                    historyKey={state.historyKey}
                    history={state.history}
                    historyLoading={state.historyLoading}
                    onShowHistory={() => handleShowHistory(setting.key)}
                    revealedValue={state.revealedValues[setting.key]}
                    onStartEdit={() =>
                      dispatch({
                        type: 'START_EDIT',
                        key: setting.key,
                        currentValue: state.revealedValues[setting.key] ?? setting.value,
                      })
                    }
                    onCancelEdit={() => dispatch({ type: 'CANCEL_EDIT' })}
                    onSetEditValue={(v) => dispatch({ type: 'SET_EDIT_VALUE', value: v })}
                    onSave={() => handleSave(setting.key, state.editValue)}
                    onReset={() => handleReset(setting.key)}
                    onToggleReveal={() => handleToggleReveal(setting.key)}
                    onBooleanToggle={() => handleBooleanToggle(setting.key, setting.value)}
                  />
                )
              })}
            </div>
            {category === 'AI' && <ServerProvidersCard />}

          </CardContent>
        )}
      </Card>
    )
  }

  // ---- Render ----
  if (!isAdmin) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="text-text-secondary">Admin access required.</p>
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="font-display text-xl font-bold text-text-primary">Server Settings</h1>
            <p className="mt-0.5 text-sm text-text-tertiary">
              {state.settings.length} settings across {groups.length} {groups.length === 1 ? 'category' : 'categories'}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative w-full sm:w-72">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
              <Input
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search settings..."
                className="h-9 pl-9 text-sm"
                aria-label="Search settings"
              />
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => dispatch({ type: 'EXPAND_ALL' })}
                  className="p-1.5 rounded-md border border-border-default bg-surface-elevated hover:bg-surface-hover text-text-secondary transition-colors"
                  aria-label="Expand all categories"
                >
                  <ChevronsUpDown className="h-4 w-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>Expand all categories</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => dispatch({ type: 'COLLAPSE_ALL', categories: [...new Set(state.settings.map(s => s.category))] })}
                  className="p-1.5 rounded-md border border-border-default bg-surface-elevated hover:bg-surface-hover text-text-secondary transition-colors"
                  aria-label="Collapse all categories"
                >
                  <ChevronsDownUp className="h-4 w-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>Collapse all categories</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {/* Error */}
        {state.error && (
          <p role="alert" className="text-center text-signal-red py-4">{state.error}</p>
        )}

        {/* Save error toast */}
        {state.saveError && (
          <p role="alert" className="text-center text-signal-red text-sm py-2">{state.saveError}</p>
        )}

        {/* Save-response warnings (e.g. sidecar_host trust warning) */}
        {saveWarning && (
          <p role="status" className="text-center text-signal-amber text-sm py-2">{saveWarning}</p>
        )}

        {/* Loading skeleton */}
        {state.loading ? (
          <div className="space-y-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-48 w-full" />
            ))}
          </div>
        ) : groups.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 text-center">
            <p className="text-text-secondary">
              {state.search ? 'No settings match your search.' : 'No settings found.'}
            </p>
          </div>
        ) : state.search ? (
          <div>
            <h2 className="font-display text-sm font-semibold text-text-primary">Search results</h2>
            <p className="mb-3 mt-0.5 text-xs text-text-tertiary">
              {groups.reduce((n, g) => n + g.settings.length, 0)} settings match &ldquo;{state.search}&rdquo;
            </p>
            <div className="space-y-4">
              {groups.map(({ category, settings }) => renderCategory(category, settings))}
            </div>
          </div>
        ) : (
          <Tabs value={effectiveTab} onValueChange={handleTabChange}>
            <TabsList>
              {allGroups.map(({ category }) => (
                <TabsTrigger key={category} value={category}>
                  {category}
                </TabsTrigger>
              ))}
            </TabsList>
            {allGroups.map(({ category, settings }) => (
              <TabsContent key={category} value={category}>
                {renderCategory(category, settings)}
              </TabsContent>
            ))}
          </Tabs>
        )}
      </div>
    </TooltipProvider>
  )
}

// ---------------------------------------------------------------------------
// AI Setting Row (shared by ai_provider and ai_model)
// ---------------------------------------------------------------------------

interface AiSettingRowProps {
  setting: ServerSetting
  editing: boolean
  saveToDbNode?: React.ReactNode
  onStartEdit: (currentValue: string) => void
  onCancel: () => void
  onSave: () => void
  onReset: () => void
  saving: boolean
  historyKey: string
  history: HistoryEntry[]
  historyLoading: boolean
  onShowHistory: () => void
  children: React.ReactNode  // The actual editor (ProviderSelect or ModelCombobox)
  configureLabel: string  // e.g. "Configure provider" or "Configure model"
  notConfiguredLabel: string  // e.g. "Not configured (uses AI_PROVIDER env var)"
}

function AiSettingRow({
  setting,
  editing,
  saveToDbNode,
  onStartEdit,
  onCancel,
  onSave,
  onReset,
  saving,
  historyKey,
  history,
  historyLoading,
  onShowHistory,
  children,
  configureLabel,
  notConfiguredLabel,
}: AiSettingRowProps) {
  return (
    <div key={setting.key} className="py-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
        <SourceBadge source={setting.source} />
        {saveToDbNode}
        <EnvVarWarning setting={setting} />
      </div>
      {editing ? (
        <div className="space-y-3">
          {children}
          <div className="flex items-center gap-2 pt-1">
            <Button size="sm" onClick={onSave} disabled={saving} className="h-7 text-xs">
              {saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
            </Button>
            <Button size="sm" variant="outline" onClick={onCancel} className="h-7 text-xs">Cancel</Button>
            {setting.source === 'db' && (
              <Button size="sm" variant="ghost" onClick={onReset} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                <RotateCcw className="mr-1 h-3 w-3" /> Reset
              </Button>
            )}
          </div>
        </div>
      ) : (
        <div>
          {setting.value ? (
            <span className="inline-flex items-center rounded-md bg-surface-elevated px-2 py-0.5 text-xs font-mono border border-border-default text-text-primary truncate max-w-md">
              {setting.value}
            </span>
          ) : (
            <span className="text-xs text-text-tertiary italic">{notConfiguredLabel}</span>
          )}
          <button type="button" onClick={() => onStartEdit(setting.value || '')} className="mt-1.5 block text-xs text-text-link hover:text-signal-blue font-medium">
            {configureLabel}
          </button>
        </div>
      )}
      <p className="text-xs text-text-tertiary">{setting.description}</p>
      {setting.source === 'db' && setting.updated_by && (
        <p className="text-xs text-text-quaternary">Modified by {setting.updated_by}</p>
      )}
      {setting.source === 'db' && (
        <HistoryToggle
          settingKey={setting.key}
          sensitive={setting.sensitive}
          historyKey={historyKey}
          history={history}
          historyLoading={historyLoading}
          onShowHistory={onShowHistory}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Setting Row
// ---------------------------------------------------------------------------

interface SettingRowProps {
  setting: ServerSetting
  saveToDbNode?: React.ReactNode
  isEditing: boolean
  editValue: string
  saving: boolean
  resetting: boolean
  revealed: boolean
  revealedValue?: string
  historyKey: string
  history: HistoryEntry[]
  historyLoading: boolean
  onShowHistory: () => void
  onStartEdit: () => void
  onCancelEdit: () => void
  onSetEditValue: (value: string) => void
  onSave: () => void
  onReset: () => void
  onToggleReveal: () => void
  onBooleanToggle: () => void
}

function SettingRow({
  setting,
  saveToDbNode,
  isEditing,
  editValue,
  saving,
  resetting,
  revealed,
  revealedValue,
  historyKey,
  history,
  historyLoading,
  onShowHistory,
  onStartEdit,
  onCancelEdit,
  onSetEditValue,
  onSave,
  onReset,
  onToggleReveal,
  onBooleanToggle,
}: SettingRowProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  // Focus input when editing starts
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus()
    }
  }, [isEditing])

  const displayValue = setting.sensitive
    ? (revealed && revealedValue !== undefined ? revealedValue : '••••••••')
    : setting.value

  // ---- Editing mode ----
  if (isEditing) {
    return (
      <div className="py-3 space-y-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
          <SourceBadge source={setting.source} />
          {saveToDbNode}
          <EnvVarWarning setting={setting} />
          {setting.restart_required && <RestartBadge />}
        </div>
        <Input
          ref={inputRef}
          type={setting.type === 'integer' ? 'number' : setting.sensitive ? 'password' : 'text'}
          autoComplete={setting.sensitive ? 'new-password' : 'off'}
          value={editValue}
          onChange={(e) => onSetEditValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSave()
            if (e.key === 'Escape') onCancelEdit()
          }}
          className="h-9 font-mono text-sm"
          aria-label={`Edit ${setting.env_var}`}
        />
        <p className="text-xs text-text-tertiary">{setting.description}</p>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onSave} disabled={saving} className="h-7 text-xs">
            {saving ? (
              <>
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                Saving…
              </>
            ) : (
              'Save'
            )}
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancelEdit} disabled={saving} className="h-7 text-xs">
            Cancel
          </Button>
          {setting.source === 'db' && (
            <Button
              size="sm"
              variant="ghost"
              onClick={onReset}
              disabled={resetting}
              className="h-7 text-xs text-text-tertiary hover:text-signal-red"
            >
              <RotateCcw className="mr-1 h-3 w-3" />
              Reset to default
            </Button>
          )}
        </div>
      </div>
    )
  }

  // ---- Display mode ----
  return (
    <div className="group py-3">
      <div className="flex items-start justify-between gap-4">
        {/* Left side: info */}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
            <SourceBadge source={setting.source} />
            {saveToDbNode}
            <EnvVarWarning setting={setting} />
            {setting.restart_required && <RestartBadge />}
            {setting.sensitive && (
              <button
                type="button"
                onClick={onToggleReveal}
                className="rounded p-0.5 text-text-tertiary transition-colors hover:text-text-secondary"
                aria-label={revealed ? 'Hide value' : 'Reveal value'}
              >
                {revealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            )}
          </div>

          {/* Value display */}
          {setting.type === 'boolean' ? (
            <div className="flex items-center gap-2">
              <Switch
                checked={setting.value === 'true'}
                onCheckedChange={() => onBooleanToggle()}
                aria-label={`Toggle ${setting.env_var}`}
              />
              <span className="font-mono text-xs text-text-secondary">
                {setting.value === 'true' ? 'Enabled' : 'Disabled'}
              </span>
            </div>
          ) : (
            <button
              type="button"
              onClick={onStartEdit}
              className="block max-w-full cursor-pointer truncate rounded px-1 py-0.5 text-left font-mono text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary"
              aria-label={`Edit ${setting.env_var}`}
            >
              {displayValue || <span className="italic text-text-tertiary">(empty)</span>}
            </button>
          )}

          {/* Description */}
          <p className="text-xs text-text-tertiary">{setting.description}</p>

          {/* Modified by info */}
          {setting.source === 'db' && setting.updated_by && (
            <p className="text-xs text-text-tertiary">
              Modified by <span className="font-medium text-text-secondary">{setting.updated_by}</span>
              {setting.updated_at && (
                <> at <span className="font-medium text-text-secondary">{new Date(setting.updated_at).toLocaleString()}</span></>
              )}
            </p>
          )}

          {/* History toggle & entries */}
          {setting.source === 'db' && (
            <HistoryToggle
              settingKey={setting.key}
              sensitive={setting.sensitive}
              historyKey={historyKey}
              history={history}
              historyLoading={historyLoading}
              onShowHistory={onShowHistory}
            />
          )}
        </div>

        {/* Right side: actions */}
        <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
          {setting.type !== 'boolean' && (
            <Button
              size="sm"
              variant="ghost"
              onClick={onStartEdit}
              className="h-7 text-xs"
            >
              Edit
            </Button>
          )}
          {setting.source === 'db' && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={onReset}
                  disabled={resetting}
                  className="h-7 text-xs text-text-tertiary hover:text-signal-red"
                  aria-label={`Reset ${setting.env_var}`}
                >
                  {resetting ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reset to default</TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// History Toggle
// ---------------------------------------------------------------------------

interface HistoryToggleProps {
  settingKey: string
  sensitive: boolean
  historyKey: string
  history: HistoryEntry[]
  historyLoading: boolean
  onShowHistory: () => void
}

function HistoryToggle({
  settingKey,
  sensitive,
  historyKey,
  history,
  historyLoading,
  onShowHistory,
}: HistoryToggleProps) {
  const isOpen = historyKey === settingKey
  return (
    <>
      <div className="flex items-center gap-2 mt-1">
        <button
          type="button"
          onClick={onShowHistory}
          className="inline-flex items-center gap-1 text-xs text-text-link hover:text-signal-blue font-medium"
        >
          <Clock className="h-3 w-3" />
          {isOpen ? 'Hide history' : 'Show history'}
        </button>
      </div>

      {isOpen && (
        <div className="mt-2 space-y-1 border-l-2 border-border-default pl-3">
          {historyLoading ? (
            <p className="text-xs text-text-tertiary">Loading...</p>
          ) : history.length === 0 ? (
            <p className="text-xs text-text-tertiary italic">No history</p>
          ) : (
            history.map((entry) => (
              <div key={entry.id} className="text-xs text-text-secondary">
                <span className="text-text-tertiary">
                  {new Date(entry.changed_at.replace(' ', 'T') + 'Z').toLocaleString()}
                </span>
                {' · '}
                <span className={entry.action === 'reset' ? 'text-signal-amber' : 'text-signal-blue'}>
                  {entry.action}
                </span>
                {entry.changed_by && <> by <span className="font-medium">{entry.changed_by}</span></>}
                {entry.action === 'set' && entry.previous_value !== null && (
                  <span className="text-text-quaternary">
                    {' · '}
                    <span className="line-through">{sensitive ? '••••' : (entry.previous_value || '(empty)')}</span>
                    {' → '}
                    {sensitive ? '••••' : (entry.value || '(empty)')}
                  </span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Restart Badge
// ---------------------------------------------------------------------------

function RestartBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span aria-label="Restart required" className="inline-flex cursor-help items-center gap-0.5 rounded-full bg-signal-orange/10 px-1.5 py-0.5 text-xs text-signal-orange">
          <AlertTriangle className="h-3 w-3" />
          ⚠️
        </span>
      </TooltipTrigger>
      <TooltipContent>Requires restart to take effect</TooltipContent>
    </Tooltip>
  )
}

// ---------------------------------------------------------------------------
// Jira auth method control (presentational grouping only)
// ---------------------------------------------------------------------------

interface JiraAuthMethodControlProps {
  selected: JiraMethod
  onSelect: (method: JiraMethod) => void
  activeMethod: JiraMethod | null
  warningMethod: JiraMethod | null
}

function JiraAuthMethodControl({ selected, onSelect, activeMethod, warningMethod }: JiraAuthMethodControlProps) {
  return (
    <div className="mb-3 space-y-2 rounded-md border border-border-default bg-surface-elevated px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-text-secondary">Auth method</span>
        <div role="group" aria-label="Jira auth method" className="inline-flex overflow-hidden rounded-md border border-border-default">
          {JIRA_METHODS.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => onSelect(m.id)}
              aria-pressed={selected === m.id}
              className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                selected === m.id
                  ? 'bg-surface-hover text-text-primary'
                  : 'text-text-tertiary hover:bg-surface-hover hover:text-text-secondary'
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
        {activeMethod && (
          <span className="inline-flex items-center rounded-full bg-signal-green/10 px-2 py-0.5 text-xs font-medium text-signal-green">
            Active: {jiraMethodLabel(activeMethod)}
          </span>
        )}
      </div>
      {warningMethod && (
        <p className="flex items-center gap-1.5 text-xs text-signal-amber">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {jiraMethodLabel(warningMethod)} credentials are stored and take precedence — clear them to use this method.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Test connection result banner
// ---------------------------------------------------------------------------

function TestResultBanner({ result }: { result: TestIntegrationResult }) {
  return (
    <div
      role="status"
      className={`mx-4 mb-3 rounded-md border px-3 py-2 text-sm ${
        result.ok
          ? 'border-signal-green/30 bg-signal-green/10 text-signal-green'
          : 'border-signal-red/30 bg-signal-red/10 text-signal-red'
      }`}
    >
      <p className="font-medium">{result.ok ? 'Connection successful' : 'Connection failed'}</p>
      {result.message && <p className="mt-0.5">{result.message}</p>}
      {result.details && Object.keys(result.details).length > 0 && (
        <pre className="mt-1 overflow-x-auto rounded bg-surface-card p-2 text-xs text-text-secondary">
          {JSON.stringify(result.details, null, 2)}
        </pre>
      )}
    </div>
  )
}
