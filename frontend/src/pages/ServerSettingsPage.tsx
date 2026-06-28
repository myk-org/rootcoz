import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import { useLatestRef } from '@/lib/useLatestRef'
import { useAuth } from '@/lib/auth'
import { Card, CardContent } from '@/components/ui/card'
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
} from 'lucide-react'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { usePeerModels } from '@/lib/usePeerModels'
import { ProviderSelect } from '@/components/shared/ProviderSelect'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderModels } from '@/hooks/useProviderModels'
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
}

interface CategoryGroup {
  category: string
  settings: ServerSetting[]
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
  | { type: 'REVEAL_VALUE'; key: string; value: string }
  | { type: 'HIDE_VALUE'; key: string }
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
      const { [action.key]: _, ...restValues } = state.revealedValues
      return { ...state, revealedKeys: nextKeys, revealedValues: restValues }
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
  return splitOutsideBrackets(raw).map(entry => {
    const trimmed = entry.trim()
    if (!trimmed.includes(':')) return null
    const [provider, ...modelParts] = trimmed.split(':')
    return {
      id: crypto.randomUUID(),
      ai_provider: provider.trim(),
      ai_model: modelParts.join(':').trim(),
    }
  }).filter((p): p is PeerConfigWithId => p !== null && !!p.ai_provider && !!p.ai_model)
}

function serializePeerConfigs(configs: PeerConfigWithId[]): string {
  return configs
    .filter(c => c.ai_provider && c.ai_model)
    .map(c => `${c.ai_provider}:${c.ai_model}`)
    .join(',')
}

function parseAdditionalReposString(raw: string): RepoWithId[] {
  if (!raw || !raw.trim()) return []
  return raw.split(',').map(entry => {
    const trimmed = entry.trim()
    if (!trimmed.includes(':')) return null
    const colonIdx = trimmed.indexOf(':')
    const name = trimmed.slice(0, colonIdx).trim()
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

    return { id: crypto.randomUUID(), name, url, ref }
  }).filter((r): r is RepoWithId => r !== null && !!r.name)
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
  const peerModels = usePeerModels(peerConfigs, peerEditing)

  // AI default provider/model editing
  const [aiProviderEditing, setAiProviderEditing] = useState(false)
  const [aiProviderValue, setAiProviderValue] = useState('')
  const [aiModelEditing, setAiModelEditing] = useState(false)
  const [aiModelValue, setAiModelValue] = useState('')
  // For the model combobox, use the editing provider if editing, otherwise the saved setting value
  const savedAiProvider = state.settings.find(s => s.key === 'ai_provider')?.value || ''
  const effectiveAiProvider = aiProviderEditing
    ? aiProviderValue
    : (savedAiProvider || aiProviderValue)
  const aiModels = useProviderModels(effectiveAiProvider)

  // Additional repos structured editor state
  const [additionalRepos, setAdditionalRepos] = useState<RepoWithId[]>([])
  const [reposEditing, setReposEditing] = useState(false)

  // Debounced search
  const [searchInput, setSearchInput] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => dispatch({ type: 'SET_SEARCH', search: searchInput }), 200)
    return () => clearTimeout(timer)
  }, [searchInput])

  // ---- Fetch settings ----
  const fetchSettings = useCallback(async () => {
    dispatch({ type: 'FETCH_START' })
    try {
      const data = await api.get<ServerSetting[]>('/api/admin/settings')
      dispatch({ type: 'FETCH_SUCCESS', settings: data })
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

  // ---- SSE ----
  useEffect(() => {
    const eventSource = new EventSource('/api/admin/settings/stream')
    eventSource.addEventListener('settings-changed', () => {
      fetchSettingsRef.current()
    })
    eventSource.onerror = () => {
      console.debug('Server settings SSE error')
    }
    return () => eventSource.close()
  }, [])

  // ---- Shared save helper ----
  async function saveSettingValue(key: string, value: string): Promise<boolean> {
    dispatch({ type: 'SAVE_START' })
    try {
      await api.put('/api/admin/settings', { settings: { [key]: value } })
      dispatch({ type: 'SAVE_SUCCESS' })
      fetchSettings()
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

  // ---- Save setting ----
  async function handleSave(key: string, value: string) {
    await saveSettingValue(key, value)
  }

  // ---- Reset setting ----
  async function handleReset(key: string) {
    dispatch({ type: 'RESET_START', key })
    try {
      await api.delete(`/api/admin/settings/${encodeURIComponent(key)}`)
      fetchSettings()
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
    dispatch({ type: 'SAVE_START' })
    try {
      await api.put('/api/admin/settings', { settings: updates })
      dispatch({ type: 'SAVE_SUCCESS' })
      fetchSettings()
      setPeerEditing(false)  // Only close on success
    } catch (err) {
      let msg = 'Failed to save'
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        msg = body?.detail ?? 'Save failed'
      }
      dispatch({ type: 'SAVE_ERROR', error: msg })
      // Don't close — user can fix and retry
    }
  }

  async function handleSaveAiProvider(value: string) {
    const ok = await saveSettingValue('ai_provider', value)
    if (ok) {
      setAiProviderEditing(false)
      // Keep aiProviderValue in sync so effectiveAiProvider (and thus the
      // model list) reflects the newly saved provider immediately, before
      // fetchSettings() re-renders with the server-side state.
      setAiProviderValue(value)
    }
  }

  async function handleSaveAiModel(value: string) {
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

  // ---- Toggle reveal (fetch real value from server) ----
  async function handleToggleReveal(key: string) {
    if (state.revealedKeys.has(key)) {
      dispatch({ type: 'HIDE_VALUE', key })
      return
    }
    try {
      const data = await api.get<ServerSetting[]>(
        `/api/admin/settings?reveal_key=${encodeURIComponent(key)}`
      )
      const item = data.find(s => s.key === key)
      if (item) {
        dispatch({ type: 'REVEAL_VALUE', key, value: item.value })
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
  const groups: CategoryGroup[] = useMemo(() => {
    const query = state.search.toLowerCase()
    const filtered = state.settings.filter((s) => {
      if (!query) return true
      return (
        s.key.toLowerCase().includes(query) ||
        s.env_var.toLowerCase().includes(query) ||
        s.description.toLowerCase().includes(query)
      )
    })

    const categoryMap = new Map<string, ServerSetting[]>()
    for (const s of filtered) {
      const cat = s.category || 'General'
      if (!categoryMap.has(cat)) categoryMap.set(cat, [])
      categoryMap.get(cat)!.push(s)
    }

    return Array.from(categoryMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([category, settings]) => ({ category, settings }))
  }, [state.settings, state.search])

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
        ) : (
          <div className="space-y-4">
            {groups.map(({ category, settings }) => {
              const isCollapsed = state.collapsedCategories.has(category)
              return (
                <Card key={category}>
                  <button
                    type="button"
                    onClick={() => dispatch({ type: 'TOGGLE_CATEGORY', category })}
                    className="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-surface-hover rounded-t-lg"
                    aria-expanded={!isCollapsed}
                  >
                    <span className="flex items-center gap-2">
                      {isCollapsed ? (
                        <ChevronRight className="h-4 w-4 text-text-tertiary" />
                      ) : (
                        <ChevronDown className="h-4 w-4 text-text-tertiary" />
                      )}
                      <span className="font-display text-sm font-semibold text-text-primary">
                        {category}
                      </span>
                      <span className="text-xs text-text-tertiary">
                        ({settings.length} {settings.length === 1 ? 'setting' : 'settings'})
                      </span>
                    </span>
                  </button>
                  {!isCollapsed && (
                    <CardContent className="px-4 pb-4 pt-0">
                      <div className="divide-y divide-border-default">
                        {settings.map((setting) => {
                          if (setting.key === 'ai_provider') {
                            return (
                              <AiSettingRow
                                key={setting.key}
                                setting={setting}
                                editing={aiProviderEditing}
                                onStartEdit={(v) => { setAiProviderValue(v); setAiProviderEditing(true) }}
                                onCancel={() => setAiProviderEditing(false)}
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
                                <ProviderSelect value={aiProviderValue} onChange={setAiProviderValue} />
                              </AiSettingRow>
                            )
                          }

                          if (setting.key === 'ai_model') {
                            return (
                              <AiSettingRow
                                key={setting.key}
                                setting={setting}
                                editing={aiModelEditing}
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
                                <ModelCombobox
                                  value={aiModelValue}
                                  onChange={setAiModelValue}
                                  options={aiModels}
                                  placeholder="Select model..."
                                  className="max-w-md"
                                />
                                {!effectiveAiProvider && (
                                  <p className="text-xs text-signal-amber">Select a provider first to see available models</p>
                                )}
                              </AiSettingRow>
                            )
                          }

                          // ---- PEER_AI_CONFIGS: structured editor ----
                          if (setting.key === 'peer_ai_configs') {
                            return (
                              <div key={setting.key} className="py-3 space-y-2">
                                <div className="flex items-center gap-2">
                                  <span className="font-mono text-xs font-semibold text-text-primary">{setting.env_var}</span>
                                  <SourceBadge source={setting.source} />
                                </div>
                                {peerEditing ? (
                                  <div className="space-y-3">
                                    <PeerConfigList
                                      peerConfigs={peerConfigs}
                                      setPeerConfigs={setPeerConfigs}
                                      peerModels={peerModels}
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
                    </CardContent>
                  )}
                </Card>
              )
            })}
          </div>
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
          {setting.restart_required && <RestartBadge />}
        </div>
        <Input
          ref={inputRef}
          type={setting.type === 'integer' ? 'number' : setting.sensitive ? 'password' : 'text'}
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
        <span className="inline-flex cursor-help items-center gap-0.5 rounded-full bg-signal-orange/10 px-1.5 py-0.5 text-xs text-signal-orange">
          <AlertTriangle className="h-3 w-3" />
          ⚠️
        </span>
      </TooltipTrigger>
      <TooltipContent>Requires restart to take effect</TooltipContent>
    </Tooltip>
  )
}
