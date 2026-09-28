import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '@/lib/api'
import { resetProviderCatalogCache, useProviderCatalog } from '@/lib/useProviderOptions'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ModelCombobox } from './ModelCombobox'

type Provider = { provider: string; configured: boolean }

export function AiCredentials() {
  const [providers, setProviders] = useState<Provider[] | null>(null)
  const [selected, setSelected] = useState('')
  const [key, setKey] = useState('')
  const [model, setModel] = useState('')
  const { providers: catalog } = useProviderCatalog()
  const keyRef = useRef('')
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    api.get<{ providers: Provider[] }>('/api/user/ai-credentials')
      .then((result) => { if (active) setProviders(result.providers) })
      .catch(() => { if (active) setError('Could not load AI credentials') })
    return () => { active = false; keyRef.current = '' }
  }, [])

  function clearKey() {
    keyRef.current = ''
    setKey('')
  }

  const available = providers?.filter((item) => !item.configured) ?? []
  const provider = editing ?? available.find((item) => item.provider === selected)?.provider

  async function save(event: FormEvent) {
    event.preventDefault()
    const value = keyRef.current
    if (!provider || !value.trim() || !model.trim() || busy) return
    clearKey()
    setBusy(provider)
    setError('')
    setNotice('')
    try {
      const response = await api.put<{ outcome: 'accepted' | 'rejected' | 'inconclusive'; ok: boolean }>(
        `/api/user/ai-credentials/${encodeURIComponent(provider)}`, { api_key: value, model: model.trim() },
      )
      console.info('AI credential verification:', provider, response.outcome)
      if (response.outcome !== 'accepted' || !response.ok) {
        setError(response.outcome === 'rejected'
          ? 'Check the key and selected model; existing credential unchanged.'
          : 'Try again when the provider is available; existing credential unchanged.')
        return
      }
      resetProviderCatalogCache()
      setProviders((current) => current?.map((item) => item.provider === provider ? { ...item, configured: true } : item) ?? null)
      setEditing(null)
      setSelected('')
      setModel('')
      setNotice('API key verified and saved.')
    } catch {
      setError('Could not save API key')
    } finally {
      clearKey()
      setBusy(null)
    }
  }

  async function remove(provider: string) {
    if (busy) return
    clearKey()
    setBusy(provider)
    setError('')
    setNotice('')
    try {
      await api.delete(`/api/user/ai-credentials/${encodeURIComponent(provider)}`)
      resetProviderCatalogCache()
      setProviders((current) => current?.map((item) => item.provider === provider ? { ...item, configured: false } : item) ?? null)
      if (editing === provider) setEditing(null)
      setSelected('')
    } catch {
      setError('Could not remove API key')
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
        {error && <p role="alert" className="text-xs text-signal-red">{error}</p>}
        {notice && <p role="status" className="text-xs text-signal-green">{notice}</p>}
        {providers?.length === 0 && <p className="text-sm text-text-tertiary">No API-key providers available.</p>}
        {providers && <>
          {providers.some((item) => item.configured) && (
            <ul aria-label="Configured AI providers" className="divide-y divide-border-muted border-t border-border-muted">
              {providers.filter((item) => item.configured).map(({ provider: id }) => (
                <li key={id} className="flex flex-wrap items-center gap-2 py-3">
                  <span className="mr-auto font-mono text-sm text-text-primary break-all">{id}</span>
                  <span className="text-xs text-text-tertiary">Configured</span>
                  <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setSelected(''); setModel(''); setEditing(id); setError('') }} aria-label={`Replace ${id} key`}>Replace</Button>
                  <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => remove(id)} aria-label={`Remove ${id} key`}>Remove key</Button>
                </li>
              ))}
            </ul>
          )}
          {(available.length > 0 || editing) && (
            <form onSubmit={save} className="space-y-3 border-t border-border-muted pt-4">
              {editing ? (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm text-text-primary">Replace {editing} key</span>
                  <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={() => { clearKey(); setModel(''); setEditing(null); setError('') }}>Cancel</Button>
                </div>
              ) : (
                <div className="space-y-1.5">
                  <span className="block text-xs text-text-tertiary">AI provider</span>
                  <ModelCombobox value={selected} onChange={(value) => { clearKey(); setModel(''); setError(''); setSelected(value) }} options={available.map((item) => ({ id: item.provider, name: item.provider }))} placeholder="Find a provider" ariaLabel="AI provider" disabled={busy !== null} />
                </div>
              )}
              <div className="space-y-1.5">
                <span className="block text-xs text-text-tertiary">Model to verify against</span>
                <ModelCombobox value={model} onChange={(value) => { setModel(value); setError('') }} options={catalog[provider ?? ''] ?? []} placeholder="Select or enter model ID" ariaLabel="Verification model" disabled={busy !== null || !provider} />
                <p className="text-xs text-text-tertiary">If the model isn't listed, enter its ID manually.</p>
              </div>
              <div className="flex gap-2">
                <label htmlFor="ai-provider-key" className="sr-only">API key</label>
                <Input
                  id="ai-provider-key"
                  type="password"
                  autoComplete="off"
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
