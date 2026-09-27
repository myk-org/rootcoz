import { normalizeProvider } from '@/lib/aiProviders'
import type { AiModel, ProviderStatus } from '@/types'

export function allowsUnverified(status: Record<string, ProviderStatus>, provider: string, forceServer: boolean): boolean {
  const state = status[normalizeProvider(provider)]
  return !forceServer && state?.has_api_key === true && state.modelListingSupported === false
}

export function usableModels(providers: Record<string, AiModel[]>, provider: string, forceServer: boolean, status: Record<string, ProviderStatus> = {}, canUseServer = true): AiModel[] {
  return (providers[normalizeProvider(provider)] ?? []).filter((model) =>
    model.credential_sources?.some((source) => forceServer ? canUseServer && source === 'server' : source === 'user' || (canUseServer && source === 'server')) &&
    (model.verified !== false || (forceServer && model.credential_sources?.includes('server')) || allowsUnverified(status, provider, forceServer)),
  ).map((model) => forceServer && model.verified === false && model.credential_sources?.includes('server')
    ? { ...model, verified: true } : model)
}

export function visibleModels(providers: Record<string, AiModel[]>, provider: string, forceServer: boolean, status: Record<string, ProviderStatus>, canUseServer: boolean) {
  const available = usableModels(providers, provider, forceServer, status, canUseServer)
  const all = usableModels(providers, provider, forceServer, status)
  return all.map((model) => ({ ...model, serverLocked: !available.some((item) => item.id === model.id) }))
}

export function analysisProviderIds(providers: Record<string, AiModel[]>, status: Record<string, ProviderStatus>, forceServer: boolean): string[] {
  return [...new Set([...Object.keys(providers), ...Object.keys(status)])].filter((id) =>
    usableModels(providers, id, forceServer, status).length > 0 || allowsUnverified(status, id, forceServer),
  )
}

export function credentialLabel(models: AiModel[], manualUser = false, forceServer = false, canUseServer = true): string {
  const sources = new Set(models.flatMap((model) => model.credential_sources ?? []).filter((source) => !forceServer || source === 'server'))
  if (manualUser) sources.add('user')
  return sources.has('user') && sources.has('server') ? (canUseServer ? 'User + Server' : 'User + Server locked') : sources.has('user') ? 'User' : canUseServer ? 'Server' : 'Server locked'
}

export function isAnalysisAiAvailable(providers: Record<string, AiModel[]>, status: Record<string, ProviderStatus>, provider: string, model: string, forceServer: boolean, canUseServer = true): boolean {
  if (forceServer && !canUseServer) return false
  const models = usableModels(providers, provider, forceServer, status, canUseServer)
  if (!provider.trim() || !model.trim()) return false
  if (allowsUnverified(status, provider, forceServer)) return true
  return models.some((option) => option.id === model && option.verified !== false)
}
