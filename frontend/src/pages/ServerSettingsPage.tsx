import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
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
import { Checkbox } from '@/components/ui/checkbox'
import { SearchableMultiSelect } from '@/components/ui/searchable-multi-select'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
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
  Eye,
  EyeOff,
  AlertTriangle,
  RotateCcw,
  Loader2,
  Clock,
  RefreshCw,
  Zap,
  Download,
  Upload,
  X,
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
import type { AiModelsResponse } from '@/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ServerSetting {
  key: string
  /** Human name for the row title (E2E-F27) — the machine key is secondary. */
  label: string
  env_var: string
  value: string
  default: string
  description: string
  type: 'string' | 'boolean' | 'integer'
  category: string
  sensitive: boolean
  /** Whether a value is stored at all (#379 E2E-F31). Older servers omit it. */
  is_set?: boolean
  restart_required: boolean
  source: 'default' | 'env' | 'db'
  updated_by: string
  updated_at: string
  has_env_var?: boolean
  env_var_value?: string
  // Enumerated-setting contract (#379): a fixed set of choices, or the name
  // of the set only another endpoint knows.
  options?: string[] | null
  options_source?: OptionSource | null
  multiple?: boolean
  allow_custom?: boolean
  jira_verified_method?: string
  persistable?: boolean
}

interface CategoryGroup {
  category: string
  settings: ServerSetting[]
}

// POST /api/admin/settings/import response — sensitive values arrive masked.
interface SettingsImportResult {
  applied: Record<string, { from: string | null; to: string }>
  unchanged: string[]
  restart_required: string[]
  warnings?: string[]
  dry_run?: boolean
}

// Category display order for the tab layout (mirrors backend _SETTINGS_CATEGORIES).
const CATEGORY_ORDER = [
  'Server', 'AI', 'Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Datadog', 'Prow',
  'Auth & Security', 'Web Push', 'Notifications', 'Sidecar',
]

// Categories with a test-connection endpoint (category -> integration id).
const TEST_INTEGRATIONS: Record<string, string> = {
  Jenkins: 'jenkins',
  Jira: 'jira',
  GitHub: 'github',
  'Report Portal': 'reportportal',
  Datadog: 'datadog',
  Prow: 'prow',
}

// Sets of choices the server only knows at runtime, fetched from the endpoint
// that owns them (exporters, the AI provider catalog, the user table).
type OptionSource = 'exporters' | 'providers' | 'users'

// Jira auth methods (exactly one is selected in Server Settings; none = Jira disabled)
type JiraMethod = 'oauth' | 'basic' | 'pat' | 'atst'
const JIRA_METHODS: { id: JiraMethod; label: string; fields: string[] }[] = [
  {
    id: 'oauth',
    label: 'Atlassian OAuth (client secret / cloud ID)',
    fields: ['jira_oauth_client_secret', 'atlassian_oauth_client_id', 'atlassian_oauth_cloud_id', 'jira_oauth_ssl_verify'],
  },
  {
    id: 'basic',
    label: 'Email + API token',
    fields: ['jira_email', 'jira_api_token', 'jira_cloud_url', 'jira_cloud_ssl_verify'],
  },
  {
    id: 'pat',
    label: 'PAT (Bearer)',
    fields: ['jira_pat', 'jira_server_url', 'jira_server_ssl_verify'],
  },
  {
    id: 'atst',
    label: 'Atlassian service account token (ATST, Bearer)',
    fields: ['jira_atst_token', 'atlassian_oauth_cloud_id', 'jira_oauth_ssl_verify'],
  },
]

const jiraMethodLabel = (id: JiraMethod) => JIRA_METHODS.find(m => m.id === id)!.label

// Locator/TLS fields shared by oauth and atst (same Atlassian gateway tenant).
const JIRA_SHARED_FIELDS = new Set(['atlassian_oauth_cloud_id', 'jira_oauth_ssl_verify'])

function jiraSharedFieldHint(method: JiraMethod | null, key: string): string | undefined {
  if (!method || !JIRA_SHARED_FIELDS.has(key)) return undefined
  const other = method === 'oauth' ? 'atst' : method === 'atst' ? 'oauth' : null
  return other ? `Shared with the ${jiraMethodLabel(other)} method — both target the same Atlassian gateway tenant.` : undefined
}

// Per-setting hints for fields whose meaning is not obvious from the description.
function settingHint(category: string, method: JiraMethod | null, key: string): string | undefined {
  if (key === 'login_lockout_threshold') {
    return 'Failed logins before the account locks. 0 disables lockout.'
  }
  return category === 'Jira' ? jiraSharedFieldHint(method, key) : undefined
}

// Why the threshold cannot be edited while the switch is off (E2E-F34).
const LOCKOUT_OFF_HINT =
  'Lockout is disabled — this threshold is ignored until login lockout is turned on.'

function hiddenJiraFieldsFor(method: JiraMethod): Set<string> {
  // oauth and atst share locator/TLS fields (atlassian_oauth_cloud_id,
  // jira_oauth_ssl_verify) — a shared field stays visible for either of
  // its methods, so subtract the SELECTED method's fields instead of
  // unioning every other method's.
  const selected = new Set(JIRA_METHODS.find(m => m.id === method)!.fields)
  return new Set(
    JIRA_METHODS.filter(m => m.id !== method)
      .flatMap(m => m.fields)
      .filter(f => !selected.has(f)),
  )
}

// Server-only settings (env/CLI-owned, never persistable to the DB) — the
// known set, kept alongside the backend's server-only metadata marker.
const SERVER_ONLY_SETTING_KEYS = new Set(['admin_key', 'db_path', 'debug'])

// How long a ?highlight= row stays marked after the audit link lands on it.
const HIGHLIGHT_MS = 2500

// Valid Jira auth method ids ('' / anything else = unset: Jira disabled).
function parseJiraMethod(value?: string): JiraMethod | null {
  return value === 'oauth' || value === 'basic' || value === 'pat' || value === 'atst' ? value : null
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
  | { type: 'CANCEL_EDIT'; keys?: string[] }
  | { type: 'SET_EDIT_VALUE'; value: string }
  | { type: 'SAVE_START' }
  | { type: 'SAVE_SUCCESS'; keepEditor?: boolean }
  | { type: 'SAVE_ERROR'; error: string }
  | { type: 'RESET_START'; key: string }
  | { type: 'RESET_DONE' }
  | { type: 'REVEAL_VALUE'; key: string; value: string; generation: number }
  | { type: 'HIDE_VALUE'; key: string }
  | { type: 'CLEAR_REVEALED'; keys: string[] }
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
      // Optional keys scope the cancel (import): an editor on a setting the
      // document does not touch keeps its draft (#361 Qodo r2).
      if (action.keys && state.editingKey && !action.keys.includes(state.editingKey))
        return state
      return { ...state, editingKey: null, editValue: '', saveError: null }
    case 'SET_EDIT_VALUE':
      return { ...state, editValue: action.value }
    case 'SAVE_START':
      return { ...state, saving: true, saveError: null }
    case 'SAVE_SUCCESS':
      // keepEditor: a Jira method switch saves auth mode without touching the
      // field being edited — an editor on a field still visible under the new
      // method keeps its draft across the switch (#346).
      if (action.keepEditor) return { ...state, saving: false }
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

function sameProviderSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every(p => b.includes(p))
}

// Serializes the picker selection back to a setting value.
// The "all active" decision must be made on the visible selection BEFORE the
// undiscovered stored IDs are appended: otherwise unchecking every visible
// provider while a stored ID is undiscovered yields a one-provider allowlist
// instead of the empty "all" value, silently disabling the rest (#380).
function serializeActiveServerProviders(
  checked: string[],
  options: string[],
  undiscovered: string[],
): string {
  const visibleChecked = options.filter(o => checked.includes(o))
  // None selected and all selected both mean "all active" (there is no "none").
  if (visibleChecked.length === 0 || sameProviderSet(visibleChecked, options)) return ''
  // A real subset: carry undiscovered stored IDs through unchanged — they have
  // no checkbox, so dropping them would silently narrow the restriction.
  return [...new Set([...visibleChecked, ...undiscovered])].join(',')
}

// ---------------------------------------------------------------------------
// Enumerated setting choices (#379)
// ---------------------------------------------------------------------------

interface DynamicOptions {
  status: 'loading' | 'ready' | 'failed'
  options: string[]
  /** option -> why it cannot be picked (an exporter that is not configured) */
  disabled: Record<string, string>
}

const LOADING_OPTIONS: DynamicOptions = { status: 'loading', options: [], disabled: {} }

/** A stored list value: comma-separated, order preserved, no empties. */
function parseSettingList(raw: string): string[] {
  return raw.split(',').map(v => v.trim()).filter(Boolean)
}

async function fetchSettingOptions(
  source: OptionSource,
): Promise<Pick<DynamicOptions, 'options' | 'disabled'>> {
  if (source === 'exporters') {
    const exporters = await api.get<{ name: string; enabled: boolean }[]>('/api/exporters')
    const disabled: Record<string, string> = {}
    // An exporter that is not configured would never run, so it must not be
    // selectable — but the admin still sees it exists and why it is out.
    for (const exporter of exporters) if (!exporter.enabled) disabled[exporter.name] = 'not configured'
    return { options: exporters.map(e => e.name), disabled }
  }
  if (source === 'providers') {
    // The same catalog the AI pickers use. Fetched here rather than through
    // useProviderCatalog because that hook reports an empty catalog for both
    // "no providers" and "fetch failed", and a failed fetch must fall back to
    // a plain input instead of a picker with nothing to pick.
    const catalog = await api.get<AiModelsResponse>('/api/ai-models')
    return { options: Object.keys(catalog.providers ?? {}), disabled: {} }
  }
  const data = await api.get<{ users: { username: string }[] }>('/api/admin/users')
  return { options: data.users.map(u => u.username), disabled: {} }
}

/** Fetch each needed dynamic list once per mount; a failed list stays failed. */
function useDynamicSettingOptions(
  sources: OptionSource[],
): Partial<Record<OptionSource, DynamicOptions>> {
  const [state, setState] = useState<Partial<Record<OptionSource, DynamicOptions>>>({})
  const requested = useRef(new Set<OptionSource>())
  const alive = useRef(true)
  // StrictMode mounts, unmounts and remounts in development: the flag must be
  // set again on mount, or every resolved fetch after the remount is discarded
  // and the control sits on "Loading choices…" forever (live-only bug).
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  // Keyed by the set, so a settings refetch with the same sources is free.
  const key = sources.join(',')
  useEffect(() => {
    const pending = (key ? (key.split(',') as OptionSource[]) : []).filter(s => !requested.current.has(s))
    for (const source of pending) {
      requested.current.add(source)
      fetchSettingOptions(source)
        .then(v => {
          if (alive.current) setState(prev => ({ ...prev, [source]: { status: 'ready', ...v } }))
        })
        .catch(() => {
          // Allow a later refetch to retry this source instead of pinning the
          // field to its fallback input for the rest of the session.
          requested.current.delete(source)
          if (alive.current) {
            setState(prev => ({ ...prev, [source]: { status: 'failed', options: [], disabled: {} } }))
          }
        })
    }
  }, [key])
  return state
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

// Every row is titled with its human label. The env var is the operator's link
// to `.env` and the docs, so it lives in the label's tooltip — discoverable,
// copyable, never a permanent second line (E2E-F27).
function SettingRowLabel({ setting }: { setting: ServerSetting }) {
  const label = (tabIndex?: number) => (
    <span data-setting-title="" tabIndex={tabIndex} className="text-sm font-semibold text-text-primary">
      {setting.label}
    </span>
  )
  // A setting without an env var has nothing to reveal (jira_auth_mode).
  if (!setting.env_var) return label()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* Focusable: the tooltip must open without a pointer, not hover-only. */}
        {label(0)}
      </TooltipTrigger>
      <TooltipContent side="top">
        <span data-setting-env-var="" className="select-text font-mono text-xs">
          {setting.env_var}
        </span>
      </TooltipContent>
    </Tooltip>
  )
}

function SettingRowHeader({
  setting,
  saveToDbNode,
  children,
}: {
  setting: ServerSetting
  saveToDbNode?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <SettingRowLabel setting={setting} />
      <SourceBadge source={setting.source} />
      {saveToDbNode}
      <EnvVarWarning setting={setting} />
      {setting.restart_required && <RestartBadge />}
      {children}
    </div>
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

  // Peer config structured editor (always inline, E2E-F28..F30). A null draft
  // renders the stored value, so a refetch (SSE, save, reset) never overwrites
  // a change the admin has not saved yet.
  const [peerDraft, setPeerDraft] = useState<PeerConfigWithId[] | null>(null)

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

  // Additional repos structured editor — same draft contract as the peers.
  const [reposDraft, setReposDraft] = useState<RepoWithId[] | null>(null)

  const settingValue = (key: string) => state.settings.find(s => s.key === key)?.value ?? ''
  const savedPeerRows = parsePeerConfigString(settingValue('peer_ai_configs'))
  const savedRepoRows = parseAdditionalReposString(settingValue('additional_repos'))
  const peers = peerDraft ?? savedPeerRows
  const repos = reposDraft ?? savedRepoRows
  // Save/Cancel exist exactly while a draft can be saved: any change to the
  // rows. Comparing the serialised rows keeps a revert-to-stored draft clean;
  // comparing lengths keeps an added-but-still-empty row dirty, because it can
  // be saved.
  const peersDirty = peerDraft !== null && (
    peerDraft.length !== savedPeerRows.length ||
    serializePeerConfigs(peerDraft) !== serializePeerConfigs(savedPeerRows)
  )
  const reposDirty = reposDraft !== null && (
    reposDraft.length !== savedRepoRows.length ||
    serializeAdditionalRepos(reposDraft) !== serializeAdditionalRepos(savedRepoRows)
  )
  const setRepos = (action: React.SetStateAction<RepoWithId[]>) =>
    setReposDraft(prev => (typeof action === 'function' ? action(prev ?? savedRepoRows) : action))
  const setPeers = (action: React.SetStateAction<PeerConfigWithId[]>) =>
    setPeerDraft(prev => (typeof action === 'function' ? action(prev ?? savedPeerRows) : action))
  // Save, Cancel, reset and import all mean "show the stored value again".
  function clearPeerDraft() {
    setPeerDraft(null)
  }

  // Active server providers draft (null = untouched, render the stored value).
  // Keeping the draft null until a toggle means a refetch (SSE, save, reset)
  // never overwrites a selection the admin has not saved yet.
  const [serverProvidersDraft, setServerProvidersDraft] = useState<string[] | null>(null)

  // Active settings tab (persisted across reloads)
  const [activeTab, setActiveTab] = useState<string>(() => {
    try {
      return localStorage.getItem('rootcoz_settings_tab') || ''
    } catch {
      return ''
    }
  })

  // Jira auth method switch confirmation (target method, or 'none' = disable) + section-local save error
  const [pendingJiraSwitch, setPendingJiraSwitch] = useState<JiraMethod | 'none' | null>(null)
  const [jiraModeError, setJiraModeError] = useState<string | null>(null)

  // Test connection state
  const [testingCategory, setTestingCategory] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ category: string; result: TestIntegrationResult } | null>(null)

  // Save-response warnings (e.g. the non-loopback sidecar_host trust warning).
  const [saveWarning, setSaveWarning] = useState<string | null>(null)

  // "Save to DB" in-flight key (persisting an env-sourced setting into the DB).
  const [persistingKey, setPersistingKey] = useState<string | null>(null)

  // Settings export/import state (#361)
  const [exporting, setExporting] = useState(false)
  const [includeSecrets, setIncludeSecrets] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [importDoc, setImportDoc] = useState<{ name: string; doc: unknown } | null>(null)
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<SettingsImportResult | null>(null)
  const [settingsIoError, setSettingsIoError] = useState<string | null>(null)
  const importFileRef = useRef<HTMLInputElement>(null)

  // Debounced search
  const [searchInput, setSearchInput] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => dispatch({ type: 'SET_SEARCH', search: searchInput }), 200)
    return () => clearTimeout(timer)
  }, [searchInput])

  // ?highlight=<key> comes from an audit row's target link: mark that setting's
  // row for a moment so the admin sees which one the event changed.
  const [searchParams] = useSearchParams()
  const highlightKey = searchParams.get('highlight') ?? ''
  const [highlighted, setHighlighted] = useState('')
  // The highlight whose search this effect already cleared: the admin's own
  // typing in the 2.5s the row stays marked must survive.
  const clearedHighlightRef = useRef('')
  useEffect(() => {
    if (!highlightKey) return
    setHighlighted(highlightKey)
    const timer = setTimeout(() => setHighlighted(''), HIGHLIGHT_MS)
    return () => clearTimeout(timer)
  }, [highlightKey])

  // ---- Fetch settings ----
  // silent=true skips the loading skeleton (used after save/reset: keep the
  // already-rendered content mounted so the scroll position is preserved).
  // Generation guard (same pattern as testGenerationRef): a stale response —
  // e.g. the Jira-test refetch resolving after a method-switch refetch —
  // must not overwrite newer settings (#346). A superseded response also
  // returns null so no caller state is fed from it — only the dispatch is
  // dropped, the stale payload never reaches a caller.
  const settingsGenerationRef = useRef(0)
  const fetchSettings = useCallback(async (silent = false): Promise<ServerSetting[] | null> => {
    const generation = ++settingsGenerationRef.current
    if (!silent) dispatch({ type: 'FETCH_START' })
    try {
      const data = await api.get<ServerSetting[]>('/api/admin/settings')
      if (generation !== settingsGenerationRef.current) return null // superseded
      dispatch({ type: 'FETCH_SUCCESS', settings: data })
      return data
    } catch (err) {
      if (generation === settingsGenerationRef.current) {
        dispatch({
          type: 'FETCH_ERROR',
          error: err instanceof Error ? err.message : 'Failed to load settings',
        })
      }
      return null
    }
  }, [])

  const fetchSettingsRef = useLatestRef(fetchSettings)
  // Latest editing key for async flows (Jira method switch): the closure state
  // captured at call time can be stale once the awaited save resolves (#346).
  const editingKeyRef = useLatestRef(state.editingKey)

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

  // Choices for the enumerated settings whose list lives in another endpoint
  // (exporters, the provider catalog, the user table). Loaded once per source,
  // off the render path so a slow sidecar never blocks the page.
  const optionSources = useMemo(
    () => [...new Set(
      state.settings
        .map(s => s.options_source)
        .filter((source): source is OptionSource => Boolean(source)),
    )].sort(),
    [state.settings],
  )
  const dynamicOptions = useDynamicSettingOptions(optionSources)

  // ---- Shared save helper (returns null on success, the error message on failure) ----
  // opts.keepEditor: the Jira method switch uses this so the SAVE_SUCCESS it
  // triggers does not close an editor on a field still visible under the new
  // method (#346).
  async function saveSettings(
    updates: Record<string, string>,
    opts?: { keepEditor?: boolean },
  ): Promise<string | null> {
    dispatch({ type: 'SAVE_START' })
    setSaveWarning(null)
    try {
      const result = await api.put<{ warnings?: string[] }>('/api/admin/settings', { settings: updates })
      // Keep controls disabled until the refetched values are rendered —
      // otherwise a re-opened editor can save the stale value over the fresh one.
      await fetchSettings(true)
      // A saved key's cached reveal is stale — clear it so the fresh masked value shows (#343).
      dispatch({ type: 'CLEAR_REVEALED', keys: Object.keys(updates) })
      dispatch({ type: 'SAVE_SUCCESS', keepEditor: opts?.keepEditor })
      if (result?.warnings?.length) setSaveWarning(result.warnings.join(' '))
      return null
    } catch (err) {
      let msg = 'Failed to save'
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        msg = body?.detail ?? `Save failed (${err.status})`
      }
      dispatch({ type: 'SAVE_ERROR', error: msg })
      return msg
    }
  }

  async function saveSettingValue(key: string, value: string, keepEditor = false): Promise<string | null> {
    return saveSettings({ [key]: value }, { keepEditor })
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
      await fetchSettings(true)
      dispatch({ type: 'CLEAR_REVEALED', keys: [key] })
      // A reset while a provider draft is unsaved must not leave it saveable
      // over the fresh value (#339/#340). Dropping the draft renders whatever
      // the refetch returned — never the pre-reset selection (#346).
      if (key === 'active_server_providers') setServerProvidersDraft(null)
      if (key === 'peer_ai_configs') clearPeerDraft()
      if (key === 'additional_repos') setReposDraft(null)
    } catch {
      // silently fail – data will refetch anyway
    } finally {
      dispatch({ type: 'RESET_DONE' })
    }
  }

  // ---- Boolean toggle (save immediately) ----
  // keepEditor for the lockout switch: the threshold editor it is about to
  // disable must keep the number the admin already typed (E2E-F34).
  async function handleBooleanToggle(key: string, currentValue: string) {
    const newValue = currentValue === 'true' ? 'false' : 'true'
    await saveSettingValue(key, newValue, key === 'login_lockout_enabled')
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

  // ---- Export / import server settings (#361) ----
  async function runExport() {
    setExporting(true)
    setSettingsIoError(null)
    try {
      const doc = await api.get<Record<string, unknown>>(
        `/api/admin/settings/export${includeSecrets ? '?include_sensitive=true' : ''}`,
      )
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `rootcoz-settings-${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setSettingsIoError(
        err instanceof ApiError
          ? extractApiDetail(err) ?? `Export failed (${err.status})`
          : 'Failed to export settings',
      )
    } finally {
      setExporting(false)
    }
  }

  function handleExport() {
    // The export dialog owns the secrets choice (#361 UI review): the
    // checkbox lives with the action it affects, so the toolbar stays two
    // plain buttons and nothing about "secrets" floats unattached.
    setExportOpen(true)
  }

  function handleImportFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = '' // allow re-selecting the same file
    if (!file) return
    setSettingsIoError(null)
    void file.text().then(text => {
      try {
        setImportDoc({ name: file.name, doc: JSON.parse(text) })
      } catch {
        setSettingsIoError(`${file.name} is not a valid JSON file`)
      }
    })
  }

  async function handleConfirmImport() {
    if (!importDoc || importing) return
    setImporting(true)
    // A retry must clear the previous attempt's error — the page-level
    // alert would otherwise sit beside the success summary (#361 Qodo r2).
    setSettingsIoError(null)
    try {
      const result = await api.post<SettingsImportResult>(
        '/api/admin/settings/import',
        importDoc.doc,
      )
      setImportResult(result)
      const doc = importDoc.doc as { settings?: Record<string, unknown> }
      const importedKeys = Object.keys(doc.settings ?? {})
      setImportDoc(null)
      // An open editor on an imported key holds a pre-import draft that
      // would overwrite the imported value on Save — cancel it before the
      // refetch lands. Editors on untouched keys keep their drafts.
      dispatch({ type: 'CANCEL_EDIT', keys: importedKeys })
      if (importedKeys.includes('peer_ai_configs')) clearPeerDraft()
      if (importedKeys.includes('additional_repos')) setReposDraft(null)
      // Revealed values are pre-import plaintext — clear them so the fresh
      // (masked) values show and editors never pre-fill a stale secret.
      dispatch({ type: 'CLEAR_REVEALED', keys: importedKeys })
      await fetchSettings(true)
    } catch (err) {
      setSettingsIoError(
        err instanceof ApiError
          ? extractApiDetail(err) ?? `Import failed (${err.status})`
          : 'Failed to import settings',
      )
      // importDoc is kept — the dialog stays open so the admin can retry
      // the same file without reselecting it.
    } finally {
      setImporting(false)
    }
  }

  // ---- Peer config structured editor ----
  // Rounds are a setting of their own row (E2E-F33): the peer payload is peers only.
  async function handleSavePeerConfig() {
    const error = await saveSettingValue('peer_ai_configs', serializePeerConfigs(peers))
    // Only a saved change returns the editor to its clean state.
    if (!error) clearPeerDraft()
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
      const error = await saveSettings({ ai_provider: value, ai_model: model })
      if (!error) {
        setAiProviderEditing(false)
        setAiModelEditing(false)
        setAiProviderValue(value)
      }
      return
    }
    const error = await saveSettingValue('ai_provider', value)
    if (error) return
    setAiProviderEditing(false)
    setAiProviderValue(value)
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
      const error = await saveSettings({ ai_provider: providerDraft, ai_model: model })
      if (!error) {
        setAiProviderEditing(false)
        setAiModelEditing(false)
        setAiProviderValue(providerDraft)
      }
      return
    }
    const error = await saveSettingValue('ai_model', value)
    if (error) return
    setAiModelEditing(false)
  }

  // ---- Additional repos structured editor ----
  async function handleSaveRepos() {
    const reposStr = serializeAdditionalRepos(repos)
    const error = await saveSettingValue('additional_repos', reposStr)
    if (!error) setReposDraft(null)
  }

  // ---- Active server providers editor ----
  async function handleSaveServerProviders(checked: string[], options: string[], undiscovered: string[]) {
    const value = serializeActiveServerProviders(checked, options, undiscovered)
    const error = await saveSettingValue('active_server_providers', value)
    if (error) return
    setServerProvidersDraft(null)
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
    // Every whitespace-separated word must appear somewhere: "audit retention"
    // matches AUDIT_RETENTION_DAYS even though the words are not adjacent.
    const tokens = state.search.trim().toLowerCase().split(/\s+/).filter(Boolean)
    if (tokens.length === 0) return allGroups
    // The category is searchable too: "audit security" finds AUDIT_RETENTION_DAYS
    // under "Auth & Security".
    const matches = (s: ServerSetting, group: string) => {
      const text = `${s.label} ${s.key} ${s.env_var} ${s.description} ${group}`.toLowerCase()
      return tokens.every(token => text.includes(token))
    }
    return allGroups
      .map(({ category, settings }) => ({
        category,
        settings: settings.filter(s => matches(s, category)),
      }))
      .filter(g => g.settings.length > 0)
  }, [allGroups, state.search])

  // ---- Jira auth method (explicit selection, persisted as jira_auth_mode) ----
  // The checkbox state comes ONLY from the persisted jira_auth_mode setting.
  const jiraAuthSetting = state.settings.find(s => s.key === 'jira_auth_mode')
  const persistedJiraMethod = parseJiraMethod(jiraAuthSetting?.value)
  // A method counts as configured only after its Test connection succeeded (#346).
  // The server attaches the verified-method marker to one settings item —
  // look it up by the marker, not by a specific key.
  const verifiedJiraMethod = parseJiraMethod(
    state.settings.find(s => s.jira_verified_method !== undefined)?.jira_verified_method,
  )
  // Unset = no method chosen yet: hide every method credential field until
  // the admin picks one; only method-independent rows stay visible (#346).
  const jiraHiddenFields = persistedJiraMethod
    ? hiddenJiraFieldsFor(persistedJiraMethod)
    : new Set(JIRA_METHODS.flatMap(m => m.fields))

  function handleJiraMethodChange(method: JiraMethod | null) {
    setJiraModeError(null)
    if (method === null) {
      // Dropping the active method disables Jira — a verified method gets the
      // same confirmation as a switch; an unverified one applies immediately.
      if (persistedJiraMethod === verifiedJiraMethod) {
        setPendingJiraSwitch('none')
      } else {
        void applyJiraMode(null)
      }
    } else if (persistedJiraMethod && persistedJiraMethod === verifiedJiraMethod && persistedJiraMethod !== method) {
      // Switching away from a VERIFIED active method is never silent; a
      // selected-but-unverified method switches without a prompt (#346).
      setPendingJiraSwitch(method)
    } else {
      void applyJiraMode(method)
    }
  }

  async function applyJiraMode(method: JiraMethod | null) {
    // Cancel any open edit on a field the new method hides — otherwise the
    // Jira test button stays disabled with no visible editor to save or cancel.
    // Re-checked after the save: keepEditor preserves the draft of a
    // still-visible field across the switch, but an editor opened mid-save on
    // a field the new method hides must still be cancelled (#346).
    const cancelHiddenJiraEdit = () => {
      const key = editingKeyRef.current
      if (key && (method === null || hiddenJiraFieldsFor(method).has(key))) {
        dispatch({ type: 'CANCEL_EDIT' })
      }
    }
    cancelHiddenJiraEdit()
    // keepEditor: a switch must not discard the draft of a field that stays
    // visible under the new method (e.g. jira_project_key) — only fields the new
    // method hides are cancelled above (#346).
    const error = await saveSettings({ jira_auth_mode: method ?? '' }, { keepEditor: true })
    setJiraModeError(error)
    if (!error) cancelHiddenJiraEdit()
    // The admin must land on a WORKING method: after a successful switch to a
    // real method, auto-run the existing Jira test for it. Disabling Jira never
    // tests; the result is surfaced, the switch itself never reverts (#346).
    if (method && !error) void handleTestConnection('Jira')
  }

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
      // A successful Jira test updates jira_verified_method server-side —
      // refetch so the switch-confirmation gate has current metadata (#346).
      if (result.ok && category === 'Jira') void fetchSettings(true)
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

  // A highlighted row has to be visible: drop a search that filters it out,
  // open its category, expand it, then scroll to the row. One change per pass,
  // and the saved tab is left alone — following an audit link must not rewrite
  // the admin's own preference.
  useEffect(() => {
    if (!highlighted) {
      clearedHighlightRef.current = ''
      return
    }
    // A filtered-out row is never rendered, so there would be nothing to mark.
    if (clearedHighlightRef.current !== highlighted) {
      clearedHighlightRef.current = highlighted
      if (searchInput || state.search) {
        setSearchInput('')
        dispatch({ type: 'SET_SEARCH', search: '' })
        return
      }
    }
    const category = state.settings.find(s => s.key === highlighted)?.category
    if (category) {
      if (state.collapsedCategories.has(category)) {
        dispatch({ type: 'TOGGLE_CATEGORY', category })
        return
      }
      if (category !== effectiveTab) {
        setActiveTab(category)
        return
      }
    }
    document
      .querySelector(`[data-setting-key="${CSS.escape(highlighted)}"]`)
      ?.scrollIntoView({ block: 'center' })
  }, [highlighted, searchInput, state.search, state.settings, state.collapsedCategories, effectiveTab])

  // ---- Per-category card (shared by the tab view and search results) ----
  function renderCategory(category: string, settings: ServerSetting[]) {
    const isCollapsed = state.collapsedCategories.has(category)
    // "Save to DB" affordance for env-sourced rows (null for db/default rows
    // and for server-only settings the backend owns exclusively). The
    // backend's persistable flag is authoritative — derived settings like
    // sidecar_url must not offer persistence even when env-sourced (#352).
    const saveToDbNode = (s: ServerSetting) =>
      s.source === 'env' && !SERVER_ONLY_SETTING_KEYS.has(s.key) && s.persistable !== false ? (
        <SaveToDbButton saving={persistingKey === s.key} onClick={() => handlePersistEnv(s.key)} />
      ) : null
    // Search must be able to find every Jira field, so auth-method hiding
    // only applies in the tab view. jira_auth_mode is edited by the card's
    // selector, so it never gets a row of its own (E2E-F33).
    const rowSettings = settings.filter(s => s.key !== 'jira_auth_mode' && !(
      category === 'Jira' && !state.search && jiraHiddenFields.has(s.key)
    ))
    // One setting row; the map below supplies the shared key, data attribute and
    // match mark, so no branch can render an unaddressable row.
    const renderSettingRow = (setting: ServerSetting) => {
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
              {/* Grows with the row so ~40-char model ids are readable;
                  the refresh button beside it keeps its own size. */}
              <ModelCombobox
                value={aiModelValue}
                onChange={setAiModelValue}
                options={aiModels}
                placeholder="Select model..."
                className="min-w-0 flex-1 max-w-3xl"
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

      // ---- ACTIVE_SERVER_PROVIDERS: always-visible provider picker ----
      if (setting.key === 'active_server_providers') {
        const options = setting.options ?? []
        const stored = parseActiveServerProviders(setting.value, options)
        // Stored IDs with no discovered option have no checkbox, so a
        // save must carry them through instead of dropping them.
        const undiscovered = stored.filter(p => !options.includes(p))
        // Saved selection limited to discovered options: a stale stored
        // ID keeps its place in the DB but has no checkbox to toggle.
        const saved = stored.filter(p => options.includes(p))
        const checked = serverProvidersDraft ?? saved
        const dirty = serverProvidersDraft !== null && !sameProviderSet(checked, saved)
        const toggle = (provider: string) =>
          setServerProvidersDraft(
            checked.includes(provider)
              ? checked.filter(p => p !== provider)
              : [...checked, provider],
          )
        return (
          <div key={setting.key} className="py-3 space-y-2">
            <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode(setting)} />
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
            ) : (
              <div className="space-y-2">
                <div role="group" aria-label="Active server providers" className="space-y-1">
                  {options.map((provider) => (
                    <label key={provider} className="flex items-center gap-2 text-xs">
                      <Checkbox
                        checked={checked.includes(provider)}
                        onCheckedChange={() => toggle(provider)}
                        aria-label={provider}
                      />
                      <span className={`font-mono ${checked.includes(provider) ? 'text-text-primary' : 'text-text-tertiary line-through'}`}>
                        {provider}
                      </span>
                    </label>
                  ))}
                </div>
                {checked.length === 0 && (
                  <p className="flex items-center gap-1.5 text-xs text-signal-amber">
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                    No providers selected — saving this activates ALL server providers (an empty value means all).
                  </p>
                )}
                <div className="flex items-center gap-2 pt-1">
                  {dirty && (
                    <>
                      <Button size="sm" onClick={() => handleSaveServerProviders(checked, options, undiscovered)} disabled={state.saving} className="h-7 text-xs">
                        {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setServerProvidersDraft(null)} disabled={state.saving} className="h-7 text-xs">Cancel</Button>
                    </>
                  )}
                  {setting.source === 'db' && (
                    <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} disabled={state.resettingKey === setting.key} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                      <RotateCcw className="mr-1 h-3 w-3" /> Reset
                    </Button>
                  )}
                </div>
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

      // ---- PEER_AI_CONFIGS: structured editor ----
      if (setting.key === 'peer_ai_configs') {
        return (
          <div key={setting.key} className="py-3 space-y-2">
            <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode(setting)} />
            {/* The editor is the row (E2E-F28): never hidden behind a link.
                Save/Cancel appear only while a draft differs from the stored
                value (E2E-F29/F30). */}
            <PeerConfigList
              peerConfigs={peers}
              setPeerConfigs={setPeers}
              addLabel="Add peer configuration"
            />
            {(peersDirty || setting.source === 'db') && (
              <div className="flex items-center gap-2 pt-1">
                {peersDirty && (
                  <>
                    <Button size="sm" onClick={handleSavePeerConfig} disabled={state.saving} className="h-7 text-xs">
                      {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                    </Button>
                    <Button size="sm" variant="outline" onClick={clearPeerDraft} className="h-7 text-xs">Cancel</Button>
                  </>
                )}
                {setting.source === 'db' && (
                  <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                    <RotateCcw className="mr-1 h-3 w-3" /> Reset
                  </Button>
                )}
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

      // ---- ADDITIONAL_REPOS: structured editor ----
      if (setting.key === 'additional_repos') {
        return (
          <div key={setting.key} className="py-3 space-y-2">
            <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode(setting)} />
            {/* Always inline, same empty/dirty contract as the peers above. */}
            <AdditionalReposList repos={repos} setRepos={setRepos} />
            {(reposDirty || setting.source === 'db') && (
              <div className="flex items-center gap-2 pt-1">
                {reposDirty && (
                  <>
                    <Button size="sm" onClick={handleSaveRepos} disabled={state.saving} className="h-7 text-xs">
                      {state.saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setReposDraft(null)} className="h-7 text-xs">Cancel</Button>
                  </>
                )}
                {setting.source === 'db' && (
                  <Button size="sm" variant="ghost" onClick={() => handleReset(setting.key)} className="h-7 text-xs text-text-tertiary hover:text-signal-red">
                    <RotateCcw className="mr-1 h-3 w-3" /> Reset
                  </Button>
                )}
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

      // ---- Enumerated settings: a picker, not a free-text box (#379) ----
      if (setting.options?.length || setting.options_source) {
        return (
          <EnumSettingRow
            key={setting.key}
            setting={setting}
            saveToDbNode={saveToDbNode(setting)}
            dynamic={
              setting.options_source
                ? dynamicOptions[setting.options_source] ?? LOADING_OPTIONS
                : undefined
            }
            saving={state.saving}
            resetting={state.resettingKey === setting.key}
            historyKey={state.historyKey}
            history={state.history}
            historyLoading={state.historyLoading}
            onShowHistory={() => handleShowHistory(setting.key)}
            onSave={(value) => handleSave(setting.key, value)}
            onReset={() => handleReset(setting.key)}
          />
        )
      }

      // ---- Default: generic SettingRow ----
      // Lockout is one setting with a switch and a number (E2E-F34): while the
      // switch is off the number cannot be edited, and says why.
      const lockoutOff = setting.key === 'login_lockout_threshold' &&
        state.settings.some(s => s.key === 'login_lockout_enabled' && s.value === 'false')
      return (
        <SettingRow
          key={setting.key}
          setting={setting}
          saveToDbNode={saveToDbNode(setting)}
          hint={lockoutOff ? LOCKOUT_OFF_HINT : settingHint(category, persistedJiraMethod, setting.key)}
          disabled={lockoutOff}
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
    }

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
                selected={persistedJiraMethod}
                saving={state.saving}
                pendingSwitch={pendingJiraSwitch}
                error={jiraModeError}
                onChange={handleJiraMethodChange}
                onCancelSwitch={() => setPendingJiraSwitch(null)}
                onConfirmSwitch={() => {
                  const target = pendingJiraSwitch
                  setPendingJiraSwitch(null)
                  if (target) void applyJiraMode(target === 'none' ? null : target)
                }}
                // jira_auth_mode has no row of its own (E2E-F33): its label,
                // source, env line, reset and history live in this header.
                header={jiraAuthSetting ? (
                  <SettingRowHeader setting={jiraAuthSetting} saveToDbNode={saveToDbNode(jiraAuthSetting)}>
                    {jiraAuthSetting.source === 'db' && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => handleReset(jiraAuthSetting.key)}
                        disabled={state.resettingKey === jiraAuthSetting.key}
                        className="h-6 px-2 text-xs text-text-tertiary hover:text-signal-red"
                        aria-label={`Reset ${jiraAuthSetting.label}`}
                      >
                        {state.resettingKey === jiraAuthSetting.key ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <RotateCcw className="h-3 w-3" />
                        )}
                      </Button>
                    )}
                  </SettingRowHeader>
                ) : undefined}
                history={jiraAuthSetting?.source === 'db' ? (
                  <HistoryToggle
                    settingKey={jiraAuthSetting.key}
                    sensitive={jiraAuthSetting.sensitive}
                    historyKey={state.historyKey}
                    history={state.history}
                    historyLoading={state.historyLoading}
                    onShowHistory={() => handleShowHistory(jiraAuthSetting.key)}
                  />
                ) : null}
              />
            )}
            {category === 'Jira' && (persistedJiraMethod === 'oauth' || persistedJiraMethod === 'atst') && (
              <JiraOauthUrlRow
                cloudId={state.settings.find(s => s.key === 'atlassian_oauth_cloud_id')?.value ?? ''}
              />
            )}
            {category === 'AI' && cursorAuthStatus && (
              <div className="mb-3">
                <CursorAuthBanner status={cursorAuthStatus} />
              </div>
            )}
            <div className="divide-y divide-border-default">
              {rowSettings.map((setting) => (
                <div
                  key={setting.key}
                  data-setting-key={setting.key}
                  // A mark that costs no layout: no border, no size change.
                  className={highlighted === setting.key ? 'rounded-md bg-signal-blue/10' : undefined}
                >
                  {renderSettingRow(setting)}
                </div>
              ))}
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
            <Button
              size="sm"
              variant="outline"
              className="h-9 shrink-0"
              onClick={handleExport}
              disabled={exporting}
            >
              {exporting ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Download className="mr-1 h-3.5 w-3.5" />}
              Export settings
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-9 shrink-0"
              onClick={() => importFileRef.current?.click()}
              disabled={importing}
            >
              {importing ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Upload className="mr-1 h-3.5 w-3.5" />}
              Import settings
            </Button>
            <input
              ref={importFileRef}
              type="file"
              accept="application/json"
              className="hidden"
              onChange={handleImportFile}
            />
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

        {/* Export/import errors (#361) */}
        {settingsIoError && (
          <p role="alert" className="text-center text-signal-red text-sm py-2">{settingsIoError}</p>
        )}

        {/* Import summary (#361) */}
        {importResult && (
          <div role="status" className="space-y-2 rounded-lg border border-border-default bg-surface-card p-4 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-medium text-text-primary">
                Imported {Object.keys(importResult.applied).length} setting{Object.keys(importResult.applied).length === 1 ? '' : 's'}, {importResult.unchanged.length} unchanged
              </span>
              <button
                type="button"
                onClick={() => setImportResult(null)}
                className="rounded-md p-1 text-text-tertiary hover:bg-surface-hover hover:text-text-secondary"
                aria-label="Dismiss import summary"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            {importResult.restart_required.length > 0 && (
              <p className="flex items-center gap-1.5 text-signal-amber">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                Restart required for: {importResult.restart_required.join(', ')}
              </p>
            )}
            {Object.entries(importResult.applied).map(([key, diff]) => (
              <p key={key} className="font-mono text-xs text-text-secondary">
                {key}: {diff.from ?? '(not set)'} &rarr; {diff.to}
              </p>
            ))}
            {importResult.warnings?.map(warning => (
              <p key={warning} className="text-xs text-signal-amber">{warning}</p>
            ))}
          </div>
        )}

        {/* Export dialog (#361): the secrets choice lives with the export action. */}
        <AlertDialog open={exportOpen} onOpenChange={setExportOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Export settings</AlertDialogTitle>
              <AlertDialogDescription>
                Download a JSON document of the saved server settings, ready to import on another instance.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <label className="flex items-start gap-2 rounded-md border border-border-default p-3 text-sm text-text-primary">
              <Checkbox
                checked={includeSecrets}
                onCheckedChange={(checked) => setIncludeSecrets(checked === true)}
                aria-label="Include secrets in export"
                className="mt-0.5"
              />
              <span>
                Include secrets (API keys, tokens, passwords)
                {includeSecrets && (
                  <span className="mt-1 block text-xs text-signal-amber">
                    The file will contain them in plaintext. Store and transfer it securely, and delete it once imported.
                  </span>
                )}
              </span>
            </label>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  setExportOpen(false)
                  void runExport()
                }}
              >
                Export
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {/* Import confirmation (#361) */}
        <AlertDialog open={importDoc !== null} onOpenChange={(open) => !open && setImportDoc(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Import settings</AlertDialogTitle>
              <AlertDialogDescription>
                Apply the settings from {importDoc?.name}? Values in the file overwrite the current database overrides — the import is validated first and rejected as a whole if any value is invalid.
              </AlertDialogDescription>
            </AlertDialogHeader>
            {/* The page-level alert sits behind the dialog overlay — while the
                dialog stays open for a retry, the error must be visible here. */}
            {importDoc && settingsIoError && (
              <p role="alert" className="text-signal-red text-sm">{settingsIoError}</p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={e => {
                  // Keep the dialog open — handleConfirmImport closes it via
                  // importDoc only on success; a failed import must stay
                  // retryable without reselecting the file.
                  e.preventDefault()
                  void handleConfirmImport()
                }}
              >
                {importing ? 'Importing…' : 'Import'}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

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
      <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode} />
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
// Enumerated setting row (#379): a picker, never free text, so an admin cannot
// guess a value the server already knows.
// ---------------------------------------------------------------------------

interface EnumSettingRowProps {
  setting: ServerSetting
  saveToDbNode?: React.ReactNode
  /** Runtime choices for an options_source setting (undefined = fixed options). */
  dynamic?: DynamicOptions
  saving: boolean
  resetting: boolean
  historyKey: string
  history: HistoryEntry[]
  historyLoading: boolean
  onShowHistory: () => void
  onSave: (value: string) => void
  onReset: () => void
}

interface EnumPickerProps {
  setting: ServerSetting
  options: string[]
  saving: boolean
  onSave: (value: string) => void
}

function EnumSettingRow({
  setting,
  saveToDbNode,
  dynamic,
  saving,
  resetting,
  historyKey,
  history,
  historyLoading,
  onShowHistory,
  onSave,
  onReset,
}: EnumSettingRowProps) {
  const options = dynamic ? dynamic.options : setting.options ?? []
  const failed = dynamic?.status === 'failed'
  return (
    <div className="py-3 space-y-2">
      <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode}>
        {setting.source === 'db' && (
          <Button
            size="sm"
            variant="ghost"
            onClick={onReset}
            disabled={resetting}
            className="h-6 px-2 text-xs text-text-tertiary hover:text-signal-red"
            aria-label={`Reset ${setting.env_var}`}
          >
            {resetting ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
          </Button>
        )}
      </SettingRowHeader>
      {failed ? (
        <FallbackSettingInput setting={setting} saving={saving} onSave={onSave} />
      ) : setting.multiple ? (
        <MultiValuePicker
          setting={setting}
          options={options}
          disabledOptions={dynamic?.disabled ?? {}}
          loading={dynamic?.status === 'loading'}
          saving={saving}
          onSave={onSave}
        />
      ) : (
        <SingleValuePicker setting={setting} options={options} saving={saving} onSave={onSave} />
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

function SingleValuePicker({ setting, options, saving, onSave }: EnumPickerProps) {
  // A stored value the server no longer offers must stay visible (and
  // selectable) rather than silently vanish from the control.
  const choices = setting.value && !options.includes(setting.value)
    ? [...options, setting.value]
    : options
  return (
    <SearchableMultiSelect
      label={setting.env_var}
      multiple={false}
      values={setting.value ? [setting.value] : []}
      options={choices.map(value => ({ value }))}
      // Enumerated values the server lets an admin extend (a newly published
      // Datadog region) must be typable here too, not only pickable.
      allowCustom={setting.allow_custom}
      disabled={saving}
      placeholder="Not set"
      onChange={next => onSave(next[0])}
    />
  )
}

function MultiValuePicker({
  setting,
  options,
  disabledOptions,
  loading,
  saving,
  onSave,
}: EnumPickerProps & {
  disabledOptions: Record<string, string>
  loading: boolean
}) {
  // A long list is searched, not scrolled (E2E-F32); comma-separated with no
  // spaces, in the order the admin picked.
  return (
    <SearchableMultiSelect
      label={setting.env_var}
      values={parseSettingList(setting.value)}
      options={options.map(value => ({
        value,
        disabled: Boolean(disabledOptions[value]),
        hint: disabledOptions[value],
      }))}
      loading={loading}
      allowCustom={setting.allow_custom}
      disabled={saving}
      placeholder={loading ? 'Loading choices…' : 'Add…'}
      onChange={next => onSave(next.join(','))}
    />
  )
}

/** The fetch for a dynamic list failed: fall back to typing, never a dead end. */
function FallbackSettingInput({ setting, saving, onSave }: Omit<EnumPickerProps, 'options'>) {
  // A refetch (save, reset, SSE) owns the stored value: a draft made against an
  // older value is dropped instead of being written back over the new one.
  const [draft, setDraft] = useState<{ base: string; text: string } | null>(null)
  const text = draft && draft.base === setting.value ? draft.text : setting.value
  const dirty = text !== setting.value
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <Input
          value={text}
          onChange={e => setDraft({ base: setting.value, text: e.target.value })}
          onKeyDown={e => {
            if (e.key === 'Enter' && dirty) onSave(text)
          }}
          placeholder={setting.multiple ? 'value1,value2' : 'value'}
          aria-label={`Edit ${setting.env_var}`}
          className="h-9 max-w-xs font-mono text-sm"
        />
        <Button size="sm" onClick={() => onSave(text)} disabled={saving || !dirty} className="h-9 text-xs">
          {saving ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Saving</> : 'Save'}
        </Button>
      </div>
      <p className="text-xs text-signal-amber">
        Could not load the choices from the server — type{' '}
        {setting.multiple ? 'the values comma-separated' : 'the value'} and save instead.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Setting Row
// ---------------------------------------------------------------------------

interface SettingRowProps {
  setting: ServerSetting
  saveToDbNode?: React.ReactNode
  hint?: string
  /** Depends on another setting (lockout off): no editing, and why. */
  disabled?: boolean
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
  hint,
  disabled = false,
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

  // De-emphasis without a layout change: the row keeps its height and value.
  const disabledClass = disabled ? ' cursor-not-allowed opacity-50' : ''

  // Nothing stored must never look stored (E2E-F31): `is_set` is the API's
  // answer, and a server that predates it only omits it, so a value is the
  // next best signal. Masked dots and the eye exist only for a set secret.
  const isSet = setting.is_set ?? Boolean(setting.value)
  const maskSecret = setting.sensitive && isSet
  const displayValue = !setting.sensitive
    ? setting.value
    : isSet
      ? (revealed && revealedValue !== undefined ? revealedValue : '••••••••')
      : ''

  // ---- Editing mode ----
  if (isEditing) {
    return (
      <div className="py-3 space-y-2">
        <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode} />
        <Input
          ref={inputRef}
          type={setting.type === 'integer' ? 'number' : setting.sensitive ? 'password' : 'text'}
          autoComplete={setting.sensitive ? 'new-password' : 'off'}
          value={editValue}
          disabled={disabled}
          onChange={(e) => onSetEditValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSave()
            if (e.key === 'Escape') onCancelEdit()
          }}
          className="h-9 font-mono text-sm"
          aria-label={`Edit ${setting.env_var}`}
        />
        <p className="text-xs text-text-tertiary">{setting.description}</p>
        {hint && <p className="text-xs text-text-quaternary">{hint}</p>}
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onSave} disabled={saving || disabled} className="h-7 text-xs">
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
          <SettingRowHeader setting={setting} saveToDbNode={saveToDbNode}>
            {maskSecret && (
              <button
                type="button"
                onClick={onToggleReveal}
                className="rounded p-0.5 text-text-tertiary transition-colors hover:text-text-secondary"
                aria-label={revealed ? 'Hide value' : 'Reveal value'}
              >
                {revealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            )}
          </SettingRowHeader>

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
              disabled={disabled}
              className={`block max-w-full cursor-pointer truncate rounded px-1 py-0.5 text-left font-mono text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary${disabledClass}`}
              aria-label={`Edit ${setting.env_var}`}
            >
              {displayValue || <span className="italic text-text-tertiary">(empty)</span>}
            </button>
          )}

          {/* Description */}
          <p className="text-xs text-text-tertiary">{setting.description}</p>
          {hint && <p className="text-xs text-text-quaternary">{hint}</p>}

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
              disabled={disabled}
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
              <TooltipContent>
                {setting.has_env_var
                  ? `Restore from OS env ${setting.env_var}`
                  : 'Reset to default'}
              </TooltipContent>
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
// Jira auth method control (explicit selection, persisted as jira_auth_mode)
// ---------------------------------------------------------------------------

interface JiraAuthMethodControlProps {
  selected: JiraMethod | null
  saving: boolean
  pendingSwitch: JiraMethod | 'none' | null
  error: string | null
  /** The jira_auth_mode row's own header: it has no row of its own (E2E-F33). */
  header?: React.ReactNode
  history?: React.ReactNode
  /** null = no method: Jira is disabled. */
  onChange: (method: JiraMethod | null) => void
  onCancelSwitch: () => void
  onConfirmSwitch: () => void
}

// Read-only display of the oauth method's derived API base (#352): per
// Atlassian's docs the scoped api.atlassian.com gateway built from the
// cloud ID is the only valid base for OAuth 2.0 credentials — there is
// deliberately no settable URL for this method.
const ATLASSIAN_API_GATEWAY = 'https://api.atlassian.com/ex/jira'

function JiraOauthUrlRow({ cloudId }: { cloudId: string }) {
  const derived = cloudId ? `${ATLASSIAN_API_GATEWAY}/${cloudId}` : ''
  return (
    <div className="mb-3 space-y-1 rounded-md border border-border-default bg-surface-elevated px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-semibold text-text-primary">JIRA_OAUTH_URL</span>
        <span className="rounded bg-surface-hover px-1.5 py-0.5 text-[10px] font-medium text-text-tertiary">derived</span>
      </div>
      {derived ? (
        <p className="break-all font-mono text-xs text-text-secondary">{derived}</p>
      ) : (
        <p className="text-xs text-signal-amber">
          Cannot be derived — ATLASSIAN_OAUTH_CLOUD_ID is not set. Set the cloud ID
          (or JIRA_SITE_URL, used to auto-discover it) to establish the API base.
        </p>
      )}
      <p className="text-xs text-text-quaternary">
        Built from ATLASSIAN_OAUTH_CLOUD_ID — the scoped Atlassian gateway is the
        only valid base for gateway credentials (OAuth 2.0 credentials and ATST
        tokens), so it is not settable.
      </p>
    </div>
  )
}

// A select needs a real value for "nothing" — the backend stores '' when Jira
// has no method, and the five real values are the ones it validates.
const NO_JIRA_METHOD = 'none'

function JiraAuthMethodControl({
  selected,
  saving,
  pendingSwitch,
  error,
  header,
  history,
  onChange,
  onCancelSwitch,
  onConfirmSwitch,
}: JiraAuthMethodControlProps) {
  // Same searchable picker as every other enumerated setting (E2E-F32).
  const choices = [
    ...JIRA_METHODS.map(m => ({ value: m.id, label: m.label })),
    { value: NO_JIRA_METHOD, label: 'None — Jira disabled' },
  ]
  return (
    <div className="mb-3 space-y-2 rounded-md border border-border-default bg-surface-elevated px-3 py-2.5">
      {header ?? <span className="text-xs font-semibold text-text-secondary">Auth method</span>}
      <SearchableMultiSelect
        label="Jira auth method"
        className="max-w-md"
        multiple={false}
        values={[selected ?? NO_JIRA_METHOD]}
        options={choices}
        disabled={saving}
        onChange={(next) => onChange(next[0] === NO_JIRA_METHOD ? null : (next[0] as JiraMethod))}
      />
      {error && (
        <p role="alert" className="flex items-center gap-1.5 text-xs text-signal-red">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {error}
        </p>
      )}
      <p className="text-xs text-text-quaternary">
        Exactly one method is active. Choose None to disable Jira.
      </p>
      {history}
      <AlertDialog open={pendingSwitch !== null} onOpenChange={(open) => { if (!open) onCancelSwitch() }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{pendingSwitch === 'none' ? 'Disable Jira?' : 'Switch Jira auth method?'}</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingSwitch === 'none'
                ? <>Unchecking {selected ? jiraMethodLabel(selected) : 'this method'} disables Jira until another method is selected.</>
                : <>Switch from {selected ? jiraMethodLabel(selected) : 'none'} to{' '}{pendingSwitch ? jiraMethodLabel(pendingSwitch) : ''}?</>
              }
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={onCancelSwitch}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={onConfirmSwitch}>{pendingSwitch === 'none' ? 'Disable' : 'Switch'}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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
