import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

type Provider = { provider: string; configured: boolean }

export function AiCredentials() {
  const [providers, setProviders] = useState<Provider[] | null>(null)
  const [key, setKey] = useState('')
  const keyRef = useRef('')
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    api.get<{ providers: Provider[] }>('/api/user/ai-credentials')
      .then((result) => { if (active) { setProviders(result.providers); console.info('AI credential status loaded') } })
      .catch((err) => { if (active) { setError('Could not load AI credentials'); console.error('Failed to load AI credentials:', err) } })
    return () => { active = false; keyRef.current = '' }
  }, [])

  function clearKey() {
    keyRef.current = ''
    setKey('')
  }

  async function save(event: FormEvent, provider: string) {
    event.preventDefault()
    const value = keyRef.current.trim()
    if (!value || busy) return
    clearKey()
    setBusy(provider)
    setError('')
    try {
      await api.put(`/api/user/ai-credentials/${encodeURIComponent(provider)}`, { api_key: value })
      setProviders((current) => current?.map((item) => item.provider === provider ? { ...item, configured: true } : item) ?? null)
      setEditing(null)
      console.info('AI credential saved for provider:', provider)
    } catch {
      setError('Could not save API key')
      console.error('Failed to save AI credential for provider:', provider)
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
    try {
      await api.delete(`/api/user/ai-credentials/${encodeURIComponent(provider)}`)
      setProviders((current) => current?.map((item) => item.provider === provider ? { ...item, configured: false } : item) ?? null)
      if (editing === provider) setEditing(null)
      console.info('AI credential removed for provider:', provider)
    } catch {
      setError('Could not remove API key')
      console.error('Failed to remove AI credential for provider:', provider)
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
        {providers?.length === 0 && <p className="text-sm text-text-tertiary">No API-key providers available.</p>}
        {providers?.map(({ provider, configured }) => (
          <div key={provider} className="space-y-2 border-t border-border-muted pt-4">
            <div className="flex items-center justify-between gap-3">
              <span className="font-mono text-sm text-text-primary break-all">{provider}</span>
              <span className="shrink-0 text-xs text-text-tertiary">{configured ? 'Configured' : 'Not configured'}</span>
            </div>
            <form onSubmit={(event) => save(event, provider)} className="flex gap-2">
              <label htmlFor={`ai-key-${provider}`} className="sr-only">{provider} API key</label>
              <Input
                id={`ai-key-${provider}`}
                type="password"
                autoComplete="off"
                value={editing === provider ? key : ''}
                disabled={busy !== null}
                onChange={(event) => { setEditing(provider); keyRef.current = event.target.value; setKey(event.target.value) }}
                onFocus={() => { if (editing !== provider) { clearKey(); setEditing(provider) } }}
                placeholder={configured ? 'Replace key' : 'Enter key'}
              />
              <Button type="submit" size="sm" disabled={busy !== null || editing !== provider || !key.trim()} aria-label={`Save ${provider} key`}>Save</Button>
            </form>
            {configured && <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => remove(provider)} aria-label={`Remove ${provider} key`}>Remove key</Button>}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
