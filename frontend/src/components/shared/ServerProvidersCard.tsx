import { useEffect, useRef, useState, type ComponentProps, type FormEvent } from 'react'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { resetProviderCatalogCache, useProviderCatalog } from '@/lib/useProviderOptions'
import { normalizeProvider } from '@/lib/aiProviders'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ModelCombobox, type ModelOption } from './ModelCombobox'
import { ConfirmDialog } from './ConfirmDialog'

export type ServerProviderModel = { id: string; name?: string; included: boolean; free: boolean }
export type ServerProvider = { provider: string; has_key: boolean; verified: boolean; supports_key: boolean; /** Stored key or deployment-credential refresh eligibility; absent on older servers. */ can_refresh_models?: boolean; /** A deployment credential (manifest $VAR / ambient <PROVIDER>_API_KEY) is resolvable, listing or not; absent on older servers. */ has_deployment_credential?: boolean; key_scoped?: boolean; key_scoped_status?: string | null; key_scoped_source?: 'deployment' | 'stored' | null; models: ServerProviderModel[] }

/** GET /api/admin/server-providers — one entry per catalog + stored provider. */
type ServerProviderListResult = { providers: ServerProvider[] }

/** POST add/rotate-key — HTTP 200 with ok:false when the key is rejected. */
type VerifyKeyResult = { ok: boolean; outcome?: string; message?: string }

type KeyDialogMode = 'store' | 'edit'

/** A row only exists for a provider actually credentialed — a stored key or a
 * resolvable deployment credential. Catalog models alone are not a credential:
 * such a provider must stay in Find a provider so its key can be added. */
function isProviderRow(item: ServerProvider): boolean {
  return item.has_key === true || item.has_deployment_credential === true
}

/** POST refresh-models — re-run of key-scoped discovery with the stored key. */
type RefreshModelsResult = { ok: boolean; status: string; model_count: number }

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
  // Store/Edit key run in a dialog, so the model field starts empty and the
  // picker stays closed instead of inheriting form state.
  const [keyDialog, setKeyDialog] = useState<{ provider: string; mode: KeyDialogMode } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [removeTarget, setRemoveTarget] = useState<{ provider: string; warning: RemoveWarning } | null>(null)
  const { providers: catalog, originalIds } = useProviderCatalog()

  function refreshProviders(): Promise<ServerProvider[] | null> {
    return api.get<ServerProviderListResult>('/api/admin/server-providers')
      .then((result) => { setProviders(result.providers); return result.providers })
      .catch(() => { setError('Could not load server providers'); return null })
  }

  useEffect(() => {
    let active = true
    api.get<ServerProviderListResult>('/api/admin/server-providers')
      .then((result) => { if (active) setProviders(result.providers) })
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

  // GET lists every catalog + registry provider; rows are filtered to the ones
  // credentialed. Keyless entries that run on deployment credentials (env keys,
  // gcloud/Vertex mount) keep model + free management without key controls.
  const listed = providers ?? []
  const rows = listed.filter(isProviderRow)
  // A provider already shown as a row is managed from that row (Store/Edit
  // key, Models) — offering it in Find a provider would duplicate the path.
  const added = new Set(rows.map((item) => normalizeProvider(item.provider)))
  // Only providers the server accepts keys for are offered — ambient/
  // deployment-credential providers would just 400 on key verification.
  const keySupported = new Set(listed.filter((item) => item.supports_key).map((item) => normalizeProvider(item.provider)))
  // The catalog hook folds keys for lookups; submissions must use the
  // exact-case ID the backend's supported-provider check expects. The
  // listing now carries registry-only providers (models: []), so the union of
  // catalog and listing is what the admin can add a key for: catalog order
  // first, then the remaining listed providers alphabetically. Folded-key
  // de-duplication keeps a provider present in both sources once.
  const candidates = new Map<string, string>(
    Object.keys(catalog).map((id) => [id, originalIds[id] ?? id]),
  )
  for (const id of listed.map((item) => item.provider).sort((a, b) => normalizeProvider(a).localeCompare(normalizeProvider(b)))) {
    const folded = normalizeProvider(id)
    if (!candidates.has(folded)) candidates.set(folded, id)
  }
  const addable = [...candidates]
    .filter(([folded]) => !added.has(folded) && keySupported.has(folded))
    .map(([, id]) => id)

  // Both key actions open a dialog with an empty model field — nothing is
  // preselected on the admin's behalf.
  function openKeyDialog(providerId: string, mode: KeyDialogMode) {
    setError('')
    setNotice('')
    setKeyDialog({ provider: providerId, mode })
  }

  async function runTest() {
    const value = keyRef.current
    if (!selected || !value.trim() || !model.trim() || busy) return
    clearKey()
    setBusy(selected)
    setError('')
    setNotice('')
    try {
      const res = await api.post<VerifyKeyResult>('/api/admin/server-providers',
        { provider: selected, api_key: value, verification_model: model.trim() })
      // The backend reports rejected/inconclusive keys as HTTP 200 ok:false.
      if (!res.ok) {
        setTested(false)
        setError(res.message ?? `Key ${res.outcome}`)
        return
      }
      resetProviderCatalogCache()
      await refreshProviders()
      setTested(true)
      setNotice('Provider verified — add it to finish.')
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

  // Confirm-first: build the warning from the loaded list (the backend DELETE
  // removes immediately and only reports what it removed).
  function requestRemove(providerId: string) {
    if (busy) return
    const entry = listed.find((item) => item.provider === providerId)
    setRemoveTarget({
      provider: providerId,
      warning: {
        models: (entry?.models ?? []).filter((m) => m.included).map((m) => m.id),
        free: (entry?.models ?? []).filter((m) => m.free).map((m) => m.id),
      },
    })
  }

  async function confirmRemove() {
    if (!removeTarget || busy) return
    const providerId = removeTarget.provider
    setBusy(providerId)
    setError('')
    setNotice('')
    try {
      await api.delete(`/api/admin/server-providers/${encodeURIComponent(providerId)}`)
      await afterRemoved(providerId)
      setRemoveTarget(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Already deleted elsewhere — treat as removed.
        await afterRemoved(providerId)
        setRemoveTarget(null)
      } else {
        setError('Could not remove provider')
      }
    } finally {
      setBusy(null)
    }
  }

  async function afterRemoved(providerId: string) {
    resetProviderCatalogCache()
    await refreshProviders()
    setNotice(`${providerId} removed.`)
  }

  // Re-runs key-scoped discovery with the stored key (#350) — picks up
  // permission drift without re-entering the key. The success notice only
  // shows when the follow-up list refetch also succeeded — a refreshed
  // listing with a stale card would claim success while showing old data.
  async function refreshModels(providerId: string) {
    if (busy) return
    setBusy(providerId)
    setError('')
    setNotice('')
    try {
      const res = await api.post<RefreshModelsResult>(
        `/api/admin/server-providers/${encodeURIComponent(providerId)}/refresh-models`)
      resetProviderCatalogCache()
      const refetched = await refreshProviders()
      if (refetched) {
        if (res.status === 'supported') {
          setNotice(`Refreshed key-scoped models for ${providerId}: ${res.model_count} model(s).`)
        } else if (refetched.find((p) => p.provider === providerId)?.key_scoped) {
          // A failed refresh kept the last captured listing (#350 r3) —
          // the notice must not imply a static-catalog fallback while a
          // snapshot is still in use.
          setNotice(`Key-scoped listing ${res.status} for ${providerId}; the previous captured listing is still in use.`)
        } else {
          setNotice(`Key-scoped listing ${res.status} for ${providerId}; selection uses the static catalog.`)
        }
      }
    } catch (err) {
      setError(extractApiDetail(err) ?? 'Could not refresh models')
    } finally {
      setBusy(null)
    }
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
        {providers !== null && rows.length === 0 && <p className="text-sm text-text-tertiary">No server providers configured.</p>}
        {rows.length > 0 && (
          <ul aria-label="Configured server providers" className="divide-y divide-border-muted border-t border-border-muted">
            {rows.map((item) => {
              // has_key implies a stored key the backend can always refresh
              // with; older servers omit can_refresh_models on keyless rows.
              const canRefresh = item.can_refresh_models ?? item.has_key
              const refreshButton = (
                <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => refreshModels(item.provider)} aria-label={`Refresh ${item.provider} models`}>Refresh models</Button>
              )
              return (
                <li key={item.provider} className="space-y-2 py-3">
                  <TooltipProvider delayDuration={200}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="mr-auto flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm text-text-primary break-all">{item.provider}</span>
                        {item.key_scoped_source === 'deployment' && (
                          // No key is stored for this provider (has_key stays false);
                          // the listing comes from the deployment's own credential.
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Badge variant="outline">Listing from deployment credential</Badge>
                            </TooltipTrigger>
                            <TooltipContent>
                              Model list is verified live against the deployment-provided key — no key is stored here.
                            </TooltipContent>
                          </Tooltip>
                        )}
                      </span>
                      {item.has_key ? (
                        <>
                          <Badge variant={item.verified ? 'success' : 'warning'}>{item.verified ? 'Verified' : 'Unverified'}</Badge>
                          {item.key_scoped ? (
                            <>
                              <Badge variant="outline" title="Model selection is restricted to what the stored key can access">Key-scoped</Badge>
                              {item.key_scoped_status === 'unavailable' && (
                                // A failed refresh kept the snapshot (#350 r3) —
                                // surface the failure even though rows exist.
                                <span
                                  className="text-xs text-text-tertiary"
                                  title="Model listing failed — showing the last captured list; try Refresh models"
                                >
                                  listing failed — showing last capture
                                </span>
                              )}
                            </>
                          ) : (
                            <span
                              className="text-xs text-text-tertiary"
                              title={item.key_scoped_status === 'unavailable'
                                ? 'Model listing failed — try Refresh models; until then selection uses the static catalog'
                                : 'Model listing unavailable for this key — selection uses the static catalog; try Refresh models'}
                            >
                              {item.key_scoped_status === 'unavailable' ? 'listing failed' : 'static catalog'}
                            </span>
                          )}
                          {canRefresh && refreshButton}
                          <HintButton variant="outline" size="sm" disabled={busy !== null} onClick={() => openKeyDialog(item.provider, 'edit')} aria-label={`Edit ${item.provider} key`} hint="Replaces the stored API key for this provider; the new key is verified before it is stored.">Edit key</HintButton>
                          <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => requestRemove(item.provider)} aria-label={`Remove ${item.provider}`}>Remove</Button>
                        </>
                      ) : (
                        <>
                          {/* Credential-derived, not listing-derived: the key can be
                              promoted even when no listing was captured. */}
                          {item.has_deployment_credential && <Badge variant="outline">Deployment credentials</Badge>}
                          {canRefresh && refreshButton}
                          {item.supports_key && item.has_deployment_credential && (
                            <HintButton variant="outline" size="sm" disabled={busy !== null} onClick={() => openKeyDialog(item.provider, 'store')} aria-label={`Store ${item.provider} API key`} hint="Promotes the deployment credential into RootCoz's encrypted store — pick a model to verify it; nothing is typed.">Store API key</HintButton>
                          )}
                        </>
                      )}
                    </div>
                  </TooltipProvider>
                  <ProviderModels provider={item.provider} models={item.models} />
                </li>
              )
            })}
          </ul>
        )}
        {(addable.length > 0) && (
          <form onSubmit={(event) => { event.preventDefault(); finishAdd() }} className="space-y-3 border-t border-border-muted pt-4">
            <div className="space-y-1.5">
              <span className="block text-xs text-text-tertiary">AI provider</span>
              <ModelCombobox value={selected} onChange={(value) => { clearKey(); setModel(''); invalidateTest(); setSelected(value) }} options={addable.map((id) => ({ id, name: id }))} placeholder="Find a provider" ariaLabel="Provider" disabled={busy !== null} />
            </div>
            <div className="space-y-1.5">
              <span className="block text-xs text-text-tertiary">Model to verify against</span>
              <ModelCombobox value={model} onChange={(value) => { setModel(value); invalidateTest() }} options={catalog[normalizeProvider(selected)] ?? []} placeholder="Select or enter model ID" ariaLabel="Verification model" disabled={busy !== null || !selected} />
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
                placeholder="Enter API key"
              />
              <Button type="button" size="sm" disabled={busy !== null || !selected || !model.trim() || !key.trim()} onClick={runTest}>{busy ? 'Testing…' : 'Test provider'}</Button>
              <Button type="submit" size="sm" disabled={busy !== null || !tested}>+ Add provider</Button>
            </div>
          </form>
        )}
        {keyDialog && (
          <ProviderKeyDialog
            provider={keyDialog.provider}
            mode={keyDialog.mode}
            models={catalog[normalizeProvider(keyDialog.provider)] ?? []}
            onClose={() => setKeyDialog(null)}
            onStored={(message) => {
              setKeyDialog(null)
              setNotice(message)
              resetProviderCatalogCache()
              refreshProviders()
            }}
          />
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
 * partial) over the visible, filter-matching subset, per-model include
 * checkmarks and a free pill on every row. Changes autosave (PUT) with a
 * "Saved" notice; free ⊂ included — marking a model free auto-includes it,
 * unchecking include clears the free flag. Select all only flips visible
 * models — hidden ones keep their included and free flags.
 */
function ProviderModels({ provider, models }: { provider: string; models: ServerProviderModel[] }) {
  // draft: admin's in-flight selection; saved: failure-recovery baseline
  // (last server-confirmed list); synced: snapshot the state was synced to.
  // One state object so the async rollback can read the latest `saved`
  // through a functional update — a stale closure must never win.
  const [edit, setEdit] = useState({ draft: models, saved: models, synced: models })
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  // Monotonic edit sequence: a failed save only rolls the draft back when
  // it is still the latest queued edit — a later save's outcome wins.
  const seqRef = useRef(0)
  const chainRef = useRef(Promise.resolve())
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  // A fresh server snapshot (add/remove/rotate refetch) resets the draft —
  // adjust state during render instead of a setState-in-effect sync. The
  // failure-recovery baseline follows the refreshed snapshot, so a later
  // failed PUT restores the post-refetch selection, not the pre-refetch one.
  if (edit.synced !== models) {
    setEdit({ draft: models, saved: models, synced: models })
  }

  useEffect(() => () => clearTimeout(noticeTimer.current), [])

  const { draft } = edit

  function update(next: ServerProviderModel[]) {
    setEdit((prev) => ({ ...prev, draft: next }))
    setError('')
    const seq = ++seqRef.current
    // Serialize saves: every mutation PUTs the full list, so queued saves are
    // last-write-wins and an error reverts to the last saved snapshot.
    chainRef.current = chainRef.current.then(async () => {
      try {
        await api.put(`/api/admin/server-providers/${encodeURIComponent(provider)}/models`, { models: next })
        setEdit((prev) => ({ ...prev, saved: next }))
        setNotice('Saved')
        clearTimeout(noticeTimer.current)
        noticeTimer.current = setTimeout(() => setNotice(''), 2500)
        // Free flags in every model picker come from the shared catalog cache.
        resetProviderCatalogCache()
      } catch {
        // Only the latest queued edit may settle the visible draft; when a
        // newer edit was queued behind this failure, its success (or its own
        // failure) decides what the admin sees.
        if (seqRef.current === seq) {
          setEdit((prev) => ({ ...prev, draft: prev.saved }))
          setError('Could not save models')
        }
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

  const query = filter.toLowerCase()
  const filtered = draft.filter((m) =>
    m.id.toLowerCase().includes(query) || (m.name ?? '').toLowerCase().includes(query))

  // Select all toggles only the visible (filter-matching) models; hidden
  // ones keep their included and free flags untouched.
  function toggleAll() {
    const all = filtered.every((m) => m.included)
    const visible = new Set(filtered.map((m) => m.id))
    update(draft.map((m) => visible.has(m.id)
      ? { ...m, included: !all, free: !all ? m.free : false }
      : m))
  }

  const includedCount = draft.filter((m) => m.included).length
  const visibleIncluded = filtered.filter((m) => m.included).length
  const allIncluded = filtered.length > 0 && visibleIncluded === filtered.length
  const partial = visibleIncluded > 0 && !allIncluded

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

/**
 * Row action button with a hover hint. shadcn tooltip, never a native title:
 * the row already uses tooltips for its badges.
 */
function HintButton({ hint, ...props }: { hint: string } & ComponentProps<typeof Button>) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button {...props} />
      </TooltipTrigger>
      <TooltipContent>{hint}</TooltipContent>
    </Tooltip>
  )
}

/**
 * Store / Edit key dialog for one server provider (#378).
 *
 * The parent mounts it only while open, so the model field starts empty and
 * the picker starts closed on every open — nothing is defaulted to the first
 * model, and no key survives a close/reopen. The dialog content takes the
 * initial focus instead of the combobox: ModelCombobox opens its list on
 * focus, so an autofocused input would pop the dropdown before the admin
 * touched anything.
 */
function ProviderKeyDialog({ provider, mode, models, onClose, onStored }: {
  provider: string
  mode: KeyDialogMode
  models: ModelOption[]
  onClose: () => void
  onStored: (notice: string) => void
}) {
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const contentRef = useRef<HTMLDivElement>(null)
  const editing = mode === 'edit'
  // A provider with nothing to offer in the picker (registry-only listing,
  // listing not captured yet) must still be storable: the server then picks a
  // model with the very credential being verified and rejects with "enter a
  // model id" when it finds none. Requiring a model here would leave the key
  // unstoreable, so the field is optional only when the picker is empty.
  const modelOptional = models.length === 0
  const canSubmit = (modelOptional || model.trim() !== '') && (!editing || apiKey.trim() !== '')

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !canSubmit) return
    setBusy(true)
    setError('')
    try {
      // Store carries no key at all — the server promotes the credential the
      // deployment already resolves. Rotate takes the newly typed one.
      // verification_model is omitted (never sent empty) when the admin left
      // the field blank: the server discovers one instead.
      const payload = {
        ...(editing ? { provider, api_key: apiKey } : {}),
        ...(model.trim() ? { verification_model: model.trim() } : {}),
      }
      const res = editing
        ? await api.post<VerifyKeyResult>(
            `/api/admin/server-providers/${encodeURIComponent(provider)}/rotate-key`,
            payload)
        : await api.post<VerifyKeyResult>(
            `/api/admin/server-providers/${encodeURIComponent(provider)}/store-deployment-key`,
            payload)
      // Rejected/inconclusive verification is HTTP 200 ok:false — keep the
      // dialog open with the server's message so the admin can fix the model.
      if (!res.ok) {
        setError(res.message ?? `Key ${res.outcome ?? 'rejected'}`)
        return
      }
      onStored(editing ? 'API key verified and rotated.' : `${provider} deployment credential stored.`)
    } catch (err) {
      // 404 (no deployment credential) and 409 (key already stored) carry the
      // server's explanation; anything else is a plain retry.
      setError(extractApiDetail(err)
        ?? (editing ? 'Could not update the API key' : 'Could not store the deployment credential'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent
        ref={contentRef}
        tabIndex={-1}
        onOpenAutoFocus={(event) => { event.preventDefault(); contentRef.current?.focus() }}
      >
        <DialogHeader>
          <DialogTitle>{editing ? `Edit ${provider} key` : `Store ${provider} API key`}</DialogTitle>
          <DialogDescription>
            {editing
              ? 'Replaces the stored API key for this provider. The new key is verified with a real sidecar turn before it is stored.'
              : 'Promotes the deployment credential RootCoz already resolves for this provider into its encrypted store — nothing is pasted. Pick a model to verify it against; the model must be one the credential actually reaches.'}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <div className="space-y-1.5">
            <span className="block text-xs text-text-tertiary">Model to verify against</span>
            <ModelCombobox value={model} onChange={setModel} options={models} placeholder="Select or type a model ID" ariaLabel="Verification model" disabled={busy} />
            <p className="text-xs text-text-tertiary">
              {modelOptional
                ? 'No models listed for this provider — a model will be discovered from the credential to verify against.'
                : 'If the model isn&apos;t listed, enter its ID manually.'}
            </p>
          </div>
          {editing && (
            <div className="space-y-1.5">
              <label htmlFor="server-provider-new-key" className="block text-xs text-text-tertiary">New API key</label>
              <Input
                id="server-provider-new-key"
                type="password"
                autoComplete="new-password"
                value={apiKey}
                disabled={busy}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="New API key"
              />
            </div>
          )}
          {error && <p role="alert" className="text-xs text-signal-red">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button type="submit" disabled={busy || !canSubmit}>
              {busy ? 'Verifying…' : editing ? 'Save key' : 'Store key'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
