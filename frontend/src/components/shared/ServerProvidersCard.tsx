import { useEffect, useRef, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import { resetProviderCatalogCache, useProviderCatalog } from '@/lib/useProviderOptions'
import { normalizeProvider } from '@/lib/aiProviders'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ModelCombobox } from './ModelCombobox'
import { ConfirmDialog } from './ConfirmDialog'

export type ServerProviderModel = { id: string; name?: string; included: boolean; free: boolean }
export type ServerProvider = { provider: string; verified: boolean; models: ServerProviderModel[] }

type RemoveWarning = { models: string[]; free: string[] }

/**
 * Admin management of server AI providers (Server Settings → AI).
 *
 * Mirrors the user provider-keys flow (AiCredentials): provider from the live
 * catalog → key → verification model → Test provider, with Add gated on a
 * passed test. Each added provider exposes a model multi-select that decides
 * which models run on server credentials, and a free pill per model row that
 * replaces the former standalone "Free AI models" card (free ⊂ included).
 */
export function ServerProvidersCard() {
  const [providers, setProviders] = useState<ServerProvider[] | null>(null)
  const [selected, setSelected] = useState('')
  const [key, setKey] = useState('')
  const keyRef = useRef('')
  const [model, setModel] = useState('')
  // Gate: "+ Add provider" stays disabled until the POST test passes. The
  // POST tests and adds server-side, so Add only finalizes the form.
  const [tested, setTested] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [removeTarget, setRemoveTarget] = useState<{ provider: string; warning: RemoveWarning } | null>(null)
  const { providers: catalog } = useProviderCatalog()

  function refreshProviders() {
    return api.get<ServerProvider[]>('/api/admin/server-providers')
      .then((result) => { setProviders(result) })
      .catch(() => { setError('Could not load server providers') })
  }

  useEffect(() => {
    let active = true
    api.get<ServerProvider[]>('/api/admin/server-providers')
      .then((result) => { if (active) setProviders(result) })
      .catch(() => { if (active) setError('Could not load server providers') })
    return () => { active = false; keyRef.current = '' }
  }, [])

  function clearKey() {
    keyRef.current = ''
    setKey('')
  }

  function invalidateTest() {
    setTested(false)
    setError('')
  }

  const listed = providers ?? []
  const added = new Set(listed.map((item) => normalizeProvider(item.provider)))
  const addable = Object.keys(catalog).filter((id) => !added.has(normalizeProvider(id)))
  const provider = editing ?? selected

  async function runTest() {
    const value = keyRef.current
    if (!provider || !value.trim() || !model.trim() || busy) return
    clearKey()
    setBusy(provider)
    setError('')
    setNotice('')
    try {
      if (editing) {
        await api.post(`/api/admin/server-providers/${encodeURIComponent(editing)}/rotate`,
          { api_key: value, verification_model: model.trim() })
        resetProviderCatalogCache()
        await refreshProviders()
        setEditing(null)
        setSelected('')
        setModel('')
        setNotice('API key verified and rotated.')
      } else {
        await api.post('/api/admin/server-providers',
          { provider: selected, api_key: value, verification_model: model.trim() })
        setTested(true)
        resetProviderCatalogCache()
        await refreshProviders()
        setNotice('Provider verified — add it to finish.')
      }
    } catch (err) {
      setTested(false)
      setError(extractApiDetail(err) ?? 'Provider verification failed; nothing was added.')
    } finally {
      setBusy(null)
    }
  }

  // The test POST already added the provider server-side; this only closes
  // the form, mirroring the user flow's post-save state.
  function finishAdd() {
    if (!tested) return
    setSelected('')
    setModel('')
    setTested(false)
    setNotice('Server provider added.')
  }

  async function requestRemove(providerId: string) {
    if (busy) return
    setBusy(providerId)
    setError('')
    setNotice('')
    try {
      const result = await api.delete<{ warning?: RemoveWarning }>(
        `/api/admin/server-providers/${encodeURIComponent(providerId)}`)
      const warning = result?.warning
      if (warning && (warning.models?.length || warning.free?.length)) {
        // Server withholds removal while models are affected — confirm first.
        setRemoveTarget({ provider: providerId, warning })
      } else {
        await afterRemoved(providerId)
      }
    } catch {
      setError('Could not remove provider')
    } finally {
      setBusy(null)
    }
  }

  async function confirmRemove() {
    if (!removeTarget || busy) return
    const providerId = removeTarget.provider
    setBusy(providerId)
    try {
      await api.delete(`/api/admin/server-providers/${encodeURIComponent(providerId)}`, { confirm: true })
      await afterRemoved(providerId)
      setRemoveTarget(null)
    } catch {
      setError('Could not remove provider')
    } finally {
      setBusy(null)
    }
  }

  async function afterRemoved(providerId: string) {
    resetProviderCatalogCache()
    await refreshProviders()
    if (editing === providerId) setEditing(null)
    setNotice(`${providerId} removed.`)
  }

  return (
    <Card className="mt-3 border-border-muted">
      <CardContent className="space-y-4 p-5">
        <div>
          <h3 className="font-display text-sm font-semibold text-text-primary">Server providers</h3>
          <p className="mt-1 text-xs text-text-tertiary">
            Provider keys stored on the server; models you include here run on server credentials.
            Mark models free to let every user run them while the{' '}
            <span className="font-medium">Enable free models</span> setting is on. Existing keys are never shown here.
          </p>
        </div>
        {error && <p role="alert" className="text-xs text-signal-red">{error}</p>}
        {notice && <p role="status" className="text-xs text-signal-green">{notice}</p>}
        {providers?.length === 0 && <p className="text-sm text-text-tertiary">No server providers configured.</p>}
        {providers && listed.length > 0 && (
          <ul aria-label="Configured server providers" className="divide-y divide-border-muted border-t border-border-muted">
            {listed.map((item) => (
              <li key={item.provider} className="space-y-2 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="mr-auto font-mono text-sm text-text-primary break-all">{item.provider}</span>
                  <Badge variant={item.verified ? 'success' : 'warning'}>{item.verified ? 'Verified' : 'Unverified'}</Badge>
                  <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setSelected(''); setModel(''); setTested(false); setEditing(item.provider); setError('') }} aria-label={`Rotate ${item.provider} key`}>Rotate key</Button>
                  <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => requestRemove(item.provider)} aria-label={`Remove ${item.provider}`}>Remove</Button>
                </div>
                <ProviderModels provider={item.provider} models={item.models} />
              </li>
            ))}
          </ul>
        )}
        {(addable.length > 0 || editing) && (
          <form onSubmit={(event) => { event.preventDefault(); finishAdd() }} className="space-y-3 border-t border-border-muted pt-4">
            {editing ? (
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-text-primary">Rotate {editing} key</span>
                <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setModel(''); setTested(false); setEditing(null); setError('') }}>Cancel</Button>
              </div>
            ) : (
              <div className="space-y-1.5">
                <span className="block text-xs text-text-tertiary">AI provider</span>
                <ModelCombobox value={selected} onChange={(value) => { clearKey(); setModel(''); invalidateTest(); setSelected(value) }} options={addable.map((id) => ({ id, name: id }))} placeholder="Find a provider" ariaLabel="Provider" disabled={busy !== null} />
              </div>
            )}
            <div className="space-y-1.5">
              <span className="block text-xs text-text-tertiary">Model to verify against</span>
              <ModelCombobox value={model} onChange={(value) => { setModel(value); invalidateTest() }} options={catalog[normalizeProvider(provider ?? '')] ?? []} placeholder="Select or enter model ID" ariaLabel="Verification model" disabled={busy !== null || !provider} />
              <p className="text-xs text-text-tertiary">If the model isn&apos;t listed, enter its ID manually.</p>
            </div>
            <div className="flex gap-2">
              <label htmlFor="server-provider-key" className="sr-only">API key</label>
              <Input
                id="server-provider-key"
                type="password"
                autoComplete="new-password"
                value={key}
                disabled={busy !== null}
                onChange={(event) => { keyRef.current = event.target.value; setKey(event.target.value); invalidateTest() }}
                placeholder={editing ? 'New API key' : 'Enter API key'}
              />
              <Button type="button" size="sm" disabled={busy !== null || !provider || !model.trim() || !key.trim()} onClick={runTest}>{busy ? 'Testing…' : 'Test provider'}</Button>
              {!editing && (
                <Button type="submit" size="sm" disabled={busy !== null || !tested}>+ Add provider</Button>
              )}
            </div>
          </form>
        )}
        <ConfirmDialog
          open={removeTarget !== null}
          onOpenChange={(open) => { if (!open) setRemoveTarget(null) }}
          title={`Remove ${removeTarget?.provider ?? ''}`}
          description={
            removeTarget
              ? `Selected models no longer available on server credentials: ${removeTarget.warning.models.join(', ') || 'none'}. Free models removed: ${removeTarget.warning.free.join(', ') || 'none'}.`
              : ''
          }
          confirmLabel="Remove provider"
          variant="destructive"
          onConfirm={confirmRemove}
          loading={busy === removeTarget?.provider}
        />
      </CardContent>
    </Card>
  )
}

/**
 * Per-provider model multi-select: filter box, Select all (indeterminate when
 * partial), per-model include checkmarks and a free pill on every row.
 * Changes autosave (PUT) with a "Saved" notice; free ⊂ included — marking a
 * model free auto-includes it, unchecking include clears the free flag.
 */
function ProviderModels({ provider, models }: { provider: string; models: ServerProviderModel[] }) {
  const [draft, setDraft] = useState(models)
  const [synced, setSynced] = useState(models)
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const savedRef = useRef(models)
  const chainRef = useRef(Promise.resolve())
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  // A fresh server snapshot (add/remove refetch) resets the draft — adjust
  // state during render instead of a setState-in-effect sync.
  if (synced !== models) {
    setSynced(models)
    setDraft(models)
  }

  useEffect(() => () => clearTimeout(noticeTimer.current), [])

  function update(next: ServerProviderModel[]) {
    setDraft(next)
    setError('')
    // Serialize saves: every mutation PUTs the full list, so queued saves are
    // last-write-wins and an error reverts to the last saved snapshot.
    chainRef.current = chainRef.current.then(async () => {
      try {
        await api.put(`/api/admin/server-providers/${encodeURIComponent(provider)}/models`, { models: next })
        savedRef.current = next
        setNotice('Saved')
        clearTimeout(noticeTimer.current)
        noticeTimer.current = setTimeout(() => setNotice(''), 2500)
        // Free flags in every model picker come from the shared catalog cache.
        resetProviderCatalogCache()
      } catch {
        setDraft(savedRef.current)
        setError('Could not save models')
      }
    })
  }

  function toggleIncluded(id: string) {
    update(draft.map((m) => m.id === id
      ? { ...m, included: !m.included, free: !m.included ? m.free : false }
      : m))
  }

  function toggleFree(id: string) {
    update(draft.map((m) => m.id === id
      ? { ...m, free: !m.free, included: !m.free ? true : m.included }
      : m))
  }

  function toggleAll() {
    const all = draft.every((m) => m.included)
    update(draft.map((m) => ({ ...m, included: !all, free: !all ? m.free : false })))
  }

  const query = filter.toLowerCase()
  const filtered = draft.filter((m) =>
    m.id.toLowerCase().includes(query) || (m.name ?? '').toLowerCase().includes(query))
  const includedCount = draft.filter((m) => m.included).length
  const allIncluded = draft.length > 0 && includedCount === draft.length
  const partial = includedCount > 0 && !allIncluded

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button type="button" variant="outline" size="sm" aria-label={`${provider} models`}>
            Models ({includedCount} of {draft.length})
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-96 p-0">
          <div className="border-b border-border-default p-2">
            <Input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Filter models..."
              aria-label="Filter models"
              className="h-8 text-sm"
            />
          </div>
          <div className="max-h-64 overflow-y-auto p-1">
            <label className="flex items-center gap-2 rounded px-2 py-1.5 text-xs font-medium text-text-primary hover:bg-surface-hover">
              <input
                type="checkbox"
                checked={allIncluded}
                ref={(el) => { if (el) el.indeterminate = partial }}
                onChange={toggleAll}
                aria-label="Select all"
                className="h-3.5 w-3.5 accent-signal-blue"
              />
              Select all
            </label>
            {filtered.map((m) => (
              <div key={m.id} className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-surface-hover">
                <input
                  type="checkbox"
                  checked={m.included}
                  onChange={() => toggleIncluded(m.id)}
                  aria-label={m.id}
                  className="h-3.5 w-3.5 shrink-0 accent-signal-blue"
                />
                <span className={`min-w-0 truncate font-mono text-xs ${m.included ? 'text-text-primary' : 'text-text-tertiary'}`}>{m.id}</span>
                {m.name && m.name !== m.id && (
                  <span className="min-w-0 truncate text-xs text-text-tertiary">{m.name}</span>
                )}
                <button
                  type="button"
                  aria-pressed={m.free}
                  aria-label={`Toggle ${m.id} free`}
                  title={m.free ? 'Marked free — click to clear' : 'Click to mark free'}
                  onClick={() => toggleFree(m.id)}
                  className={`ml-auto shrink-0 rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${m.free ? 'border-signal-green bg-signal-green/15 text-signal-green' : 'border-dashed border-border-default text-text-tertiary'}`}
                >
                  Free
                </button>
              </div>
            ))}
            {filtered.length === 0 && <p className="px-2 py-1.5 text-xs text-text-tertiary">No models match.</p>}
          </div>
        </PopoverContent>
      </Popover>
      {notice && <span role="status" className="text-xs text-signal-green">{notice}</span>}
      {error && <span role="alert" className="text-xs text-signal-red">{error}</span>}
    </div>
  )
}
