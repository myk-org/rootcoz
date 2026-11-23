import { useCallback, useContext, useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { AuthContext } from '@/lib/auth'
import { normalizeProvider, resetProviderCatalogCache, useProviderCatalog } from '@/lib/providerCatalog'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ModelCombobox } from './ModelCombobox'

type Provider = { provider: string; configured: boolean; supports_user_keys: boolean }

const FALLBACK_LOAD_ERROR = 'Could not load AI credentials'
const BOOTSTRAP_ADMIN_REMEDY =
  'Sign in with a registered account to store personal AI provider keys.'

function describeLoadError(err: unknown): {
  message: string
  remedy: string | null
  noCredentialStore: boolean
} {
  const detail = extractApiDetail(err)
  if (!detail) {
    return { message: FALLBACK_LOAD_ERROR, remedy: null, noCredentialStore: false }
  }
  // 403 from _credential_user() means the signed-in identity (bootstrap admin)
  // has no database user row, so it cannot store personal keys — surface the
  // server's reason and tell the user what to do instead of a dead end.
  const noCredentialStore =
    err instanceof ApiError &&
    err.status === 403 &&
    /bootstrap admin|database user|no credential store/i.test(detail)
  return {
    message: detail,
    remedy: noCredentialStore ? BOOTSTRAP_ADMIN_REMEDY : null,
    noCredentialStore,
  }
}

export function AiCredentials() {
  const username = useContext(AuthContext)?.username ?? ''
  const [providers, setProviders] = useState<Provider[] | null>(null)
  const [selected, setSelected] = useState('')
  const [key, setKey] = useState('')
  const [model, setModel] = useState('')
  const { providers: catalog, providerStatus: rawStatus } = useProviderCatalog()
  const catalogStatus = rawStatus ?? {}
  const keyRef = useRef('')
  const noCredentialStoreRef = useRef(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<{ message: string; remedy: string | null } | null>(null)
  const [mutationError, setMutationError] = useState('')
  const [notice, setNotice] = useState('')

  // Eligibility flags mirror a server setting that can change while the
  // page is open (AMBIENT_AUTH_PROVIDERS needs no restart), so the list is
  // refetched on focus and after every mutation instead of patched locally.
  // Keep loadError separate from mutationError so a focus reload never clears
  // a rejected-key or save/remove error message.
  const fetchProviders = useCallback((isActive: () => boolean = () => true) => {
    return api.get<{ providers: Provider[] }>('/api/user/ai-credentials')
      .then((result) => {
        if (!isActive()) return
        noCredentialStoreRef.current = false
        setProviders(result.providers)
        setLoadError(null)
      })
      .catch((err: unknown) => {
        if (!isActive()) return
        const { message, remedy, noCredentialStore } = describeLoadError(err)
        noCredentialStoreRef.current = noCredentialStore
        setLoadError({ message, remedy })
      })
  }, [])

  useEffect(() => {
    let active = true
    const isActive = () => active
    // Reset per-account state when the authenticated identity changes (e.g.
    // account switched in another tab while Settings stayed mounted).
    noCredentialStoreRef.current = false
    setProviders(null)
    setLoadError(null)
    setMutationError('')
    setNotice('')
    void fetchProviders(isActive)
    const onFocus = () => {
      if (!noCredentialStoreRef.current) void fetchProviders(isActive)
    }
    window.addEventListener('focus', onFocus)
    return () => { active = false; window.removeEventListener('focus', onFocus); keyRef.current = '' }
  }, [fetchProviders, username])

  function clearKey() {
    keyRef.current = ''
    setKey('')
  }

  const listed = providers ?? []
  // Providers the user can still save a key for. A stored key for a provider
  // that stopped accepting user keys (e.g. an ambient-auth provider) stays
  // visible with a Remove action only — the save endpoint would reject it.
  const addable = listed.filter((item) => item.supports_user_keys)
  // Picking one is a replace, which is what the form already submits.
  const provider = editing ?? addable.find((item) => item.provider === selected)?.provider
  // Keys the sidecar accepted but that yielded nothing selectable. Surfaced per
  // provider, without needing a selection, so the row is never a dead end.
  // Only a provider that *does* advertise listing qualifies: a manual-only key
  // also reports zero, yet the user can still enter a model ID by hand.
  const starvedKeys = listed
    .filter(
      (item) => {
        // The credentials endpoint returns exact sidecar provider IDs (e.g.
        // "EnMaaS") while the catalog is keyed by the folded (lowercase) ID.
        const status = catalogStatus[normalizeProvider(item.provider)]
        return item.configured &&
          status?.has_api_key === true &&
          status?.modelListingSupported === true &&
          status?.model_count === 0
      },
    )
    .map((item) => item.provider)

  async function save(event: FormEvent) {
    event.preventDefault()
    const value = keyRef.current
    if (!provider || !value.trim() || !model.trim() || busy) return
    clearKey()
    setBusy(provider)
    setMutationError('')
    setNotice('')
    try {
      const response = await api.put<{ outcome: 'accepted' | 'rejected' | 'inconclusive'; ok: boolean }>(
        `/api/user/ai-credentials/${encodeURIComponent(provider)}`, { api_key: value, model: model.trim() },
      )
      console.info('AI credential verification:', provider, response.outcome)
      if (response.outcome !== 'accepted' || !response.ok) {
        setMutationError(response.outcome === 'rejected'
          ? 'Check the key and selected model; existing credential unchanged.'
          : 'Try again when the provider is available; existing credential unchanged.')
        return
      }
      resetProviderCatalogCache()
      await fetchProviders()
      setEditing(null)
      setSelected('')
      setModel('')
      setNotice('API key verified and saved.')
    } catch (err) {
      setMutationError(extractApiDetail(err) ?? 'Could not save API key')
    } finally {
      clearKey()
      setBusy(null)
    }
  }

  async function remove(provider: string) {
    if (busy) return
    clearKey()
    setBusy(provider)
    setMutationError('')
    setNotice('')
    try {
      await api.delete(`/api/user/ai-credentials/${encodeURIComponent(provider)}`)
      resetProviderCatalogCache()
      await fetchProviders()
      if (editing === provider) setEditing(null)
      setSelected('')
    } catch (err) {
      setMutationError(extractApiDetail(err) ?? 'Could not remove API key')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card className="mt-5 border-border-muted">
      <CardContent className="space-y-4 p-5">
        <div>
          <h2 className="font-display text-sm font-semibold text-text-primary">AI provider keys</h2>
          <p className="mt-1 text-xs text-text-tertiary">Your keys are stored on the server. Existing keys are never shown here.</p>
        </div>
        {loadError && (
          <div
            role="alert"
            className={
              loadError.remedy
                ? 'space-y-1 rounded-md border border-signal-amber/30 bg-signal-amber/10 px-3 py-2 text-xs'
                : 'text-xs text-signal-red'
            }
          >
            <p className={loadError.remedy ? 'font-medium text-signal-amber' : undefined}>{loadError.message}</p>
            {loadError.remedy && <p className="text-text-secondary">{loadError.remedy}</p>}
          </div>
        )}
        {mutationError && <p role="alert" className="text-xs text-signal-red">{mutationError}</p>}
        {notice && <p role="status" className="text-xs text-signal-green">{notice}</p>}
        {starvedKeys.map((id) => (
          <p key={id} role="alert" className="text-xs text-signal-amber">
            Your {id} key is saved, but it returned no usable models. Remove the key and
            add it again to re-check.
          </p>
        ))}
        {providers?.length === 0 && <p className="text-sm text-text-tertiary">No API-key providers available.</p>}
        {providers && <>
          {listed.some((item) => item.configured) && (
            <ul aria-label="Configured AI providers" className="divide-y divide-border-muted border-t border-border-muted">
              {listed.filter((item) => item.configured).map(({ provider: id, supports_user_keys: canReplace }) => (
                <li key={id} className="flex flex-wrap items-center gap-2 py-3">
                  <span className="mr-auto font-mono text-sm text-text-primary break-all">{id}</span>
                  <span className="text-xs text-text-tertiary">{canReplace ? 'Configured' : 'Configured — user keys no longer accepted'}</span>
                  {canReplace && <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setSelected(''); setModel(''); setEditing(id); setMutationError('') }} aria-label={`Replace ${id} key`}>Replace</Button>}
                  <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => remove(id)} aria-label={`Remove ${id} key`}>Remove key</Button>
                </li>
              ))}
            </ul>
          )}
          {(addable.length > 0 || editing) && (
            <form onSubmit={save} className="space-y-3 border-t border-border-muted pt-4">
              {editing ? (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm text-text-primary">Replace {editing} key</span>
                  <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setModel(''); setEditing(null); setMutationError('') }}>Cancel</Button>
                </div>
              ) : (
                <div className="space-y-1.5">
                  <span className="block text-xs text-text-tertiary">AI provider</span>
                  <ModelCombobox value={selected} onChange={(value) => { clearKey(); setModel(''); setMutationError(''); setSelected(value) }} options={addable.map((item) => ({ id: item.provider, name: item.configured ? 'configured' : item.provider }))} placeholder="Find a provider" ariaLabel="AI provider" disabled={busy !== null} />
                </div>
              )}
              <div className="space-y-1.5">
                <span className="block text-xs text-text-tertiary">Model to verify against</span>
                <ModelCombobox value={model} onChange={(value) => { setModel(value); setMutationError('') }} options={catalog[normalizeProvider(provider ?? '')] ?? []} placeholder="Select or enter model ID" ariaLabel="Verification model" disabled={busy !== null || !provider} />
                <p className="text-xs text-text-tertiary">If the model isn't listed, enter its ID manually.</p>
              </div>
              <div className="flex gap-2">
                <label htmlFor="ai-provider-key" className="sr-only">API key</label>
                <Input
                  id="ai-provider-key"
                  type="password"
                  autoComplete="new-password"
                  value={key}
                  disabled={busy !== null}
                  onChange={(event) => { keyRef.current = event.target.value; setKey(event.target.value) }}
                  placeholder={editing ? 'New API key' : 'Enter API key'}
                />
                <Button type="submit" size="sm" disabled={busy !== null || !provider || !model.trim() || !key.trim()}>{editing ? 'Replace key' : '+ Add key'}</Button>
              </div>
            </form>
          )}
        </>}
      </CardContent>
    </Card>
  )
}
