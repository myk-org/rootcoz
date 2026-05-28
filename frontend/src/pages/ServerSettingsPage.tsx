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
} from 'lucide-react'

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
  | { type: 'TOGGLE_REVEAL'; key: string }
  | { type: 'EXPAND_ALL' }
  | { type: 'COLLAPSE_ALL'; categories: string[] }

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
    case 'TOGGLE_REVEAL': {
      const next = new Set(state.revealedKeys)
      if (next.has(action.key)) next.delete(action.key)
      else next.add(action.key)
      return { ...state, revealedKeys: next }
    }
    case 'EXPAND_ALL':
      return { ...state, collapsedCategories: new Set<string>() }
    case 'COLLAPSE_ALL':
      return { ...state, collapsedCategories: new Set<string>(action.categories) }
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

  // ---- Save setting ----
  async function handleSave(key: string, value: string) {
    dispatch({ type: 'SAVE_START' })
    try {
      await api.put('/api/admin/settings', { settings: { [key]: value } })
      dispatch({ type: 'SAVE_SUCCESS' })
      fetchSettings()
    } catch (err) {
      let msg = 'Failed to save'
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        msg = body?.detail ?? `Save failed (${err.status})`
      }
      dispatch({ type: 'SAVE_ERROR', error: msg })
    }
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
    dispatch({ type: 'SAVE_START' })
    try {
      await api.put('/api/admin/settings', { settings: { [key]: newValue } })
      dispatch({ type: 'SAVE_SUCCESS' })
      fetchSettings()
    } catch (err) {
      let msg = 'Failed to save'
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        msg = body?.detail ?? `Save failed (${err.status})`
      }
      dispatch({ type: 'SAVE_ERROR', error: msg })
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
                        {settings.map((setting) => (
                          <SettingRow
                            key={setting.key}
                            setting={setting}
                            isEditing={state.editingKey === setting.key}
                            editValue={state.editValue}
                            saving={state.saving}
                            resetting={state.resettingKey === setting.key}
                            revealed={state.revealedKeys.has(setting.key)}
                            onStartEdit={() =>
                              dispatch({
                                type: 'START_EDIT',
                                key: setting.key,
                                currentValue: setting.value,
                              })
                            }
                            onCancelEdit={() => dispatch({ type: 'CANCEL_EDIT' })}
                            onSetEditValue={(v) => dispatch({ type: 'SET_EDIT_VALUE', value: v })}
                            onSave={() => handleSave(setting.key, state.editValue)}
                            onReset={() => handleReset(setting.key)}
                            onToggleReveal={() => dispatch({ type: 'TOGGLE_REVEAL', key: setting.key })}
                            onBooleanToggle={() => handleBooleanToggle(setting.key, setting.value)}
                          />
                        ))}
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
// Setting Row
// ---------------------------------------------------------------------------

interface SettingRowProps {
  setting: ServerSetting
  isEditing: boolean
  editValue: string
  saving: boolean
  resetting: boolean
  revealed: boolean
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

  const displayValue = setting.sensitive && !revealed ? '••••••••' : setting.value

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
