import { normalizeProvider } from '@/lib/aiProviders'
import type { AiModel, ProviderStatus } from '@/types'

/**
 * Request override for a user-chosen AI pair. Only a complete provider+model pair is
 * transmitted: a bare provider makes the server substitute its own model, which may not
 * exist for that provider, so an incomplete selection is treated as no selection.
 * Spread into the payload; a partial selection yields an empty object.
 */
export function completeAiPairOverride(provider: string, model: string): Partial<{ ai_provider: string; ai_model: string }> {
  return provider && model ? { ai_provider: provider, ai_model: model } : {}
}

export function allowsUnverified(status: Record<string, ProviderStatus>, provider: string, forceServer: boolean): boolean {
  const state = status[normalizeProvider(provider)]
  return !forceServer && state?.has_api_key === true && state.modelListingSupported === false
}

export function usableModels(providers: Record<string, AiModel[]>, provider: string, forceServer: boolean, status: Record<string, ProviderStatus> = {}, canUseServer = true, canUseFree = false): AiModel[] {
  // A user without the server grant may still use an admin-declared free
  // pair when they hold the free-providers grant; free models pass the
  // server-credential filter in both toggle states.
  const serverAllowed = (model: AiModel) => canUseServer || (canUseFree && model.free === true)
  return (providers[normalizeProvider(provider)] ?? []).filter((model) =>
    model.credential_sources?.some((source) => forceServer ? serverAllowed(model) && source === 'server' : source === 'user' || (serverAllowed(model) && source === 'server')) &&
    (model.verified !== false || (forceServer && model.credential_sources?.includes('server')) || allowsUnverified(status, provider, forceServer)),
  ).map((model) => forceServer && model.verified === false && model.credential_sources?.includes('server')
    ? { ...model, verified: true } : model)
}

export function visibleModels(providers: Record<string, AiModel[]>, provider: string, forceServer: boolean, status: Record<string, ProviderStatus>, canUseServer: boolean, canUseFree = false) {
  const available = usableModels(providers, provider, forceServer, status, canUseServer, canUseFree)
  const all = usableModels(providers, provider, forceServer, status)
  return all.map((model) => ({ ...model, serverLocked: !available.some((item) => item.id === model.id) }))
}

export function analysisProviderIds(providers: Record<string, AiModel[]>, status: Record<string, ProviderStatus>, forceServer: boolean): string[] {
  return [...new Set([...Object.keys(providers), ...Object.keys(status)])].filter((id) =>
    usableModels(providers, id, forceServer, status).length > 0 || allowsUnverified(status, id, forceServer),
  )
}

export function credentialLabel(models: AiModel[], manualUser = false, forceServer = false): string {
  const sources = new Set(models.flatMap((model) => model.credential_sources ?? []).filter((source) => !forceServer || source === 'server'))
  if (manualUser) sources.add('user')
  return sources.has('user') && sources.has('server') ? 'User + Server' : sources.has('user') ? 'User' : 'Server'
}

export function isAnalysisAiAvailable(providers: Record<string, AiModel[]>, status: Record<string, ProviderStatus>, provider: string, model: string, forceServer: boolean, canUseServer = true, canUseFree = false): boolean {
  if (forceServer && !canUseServer && !canUseFree) return false
  const models = usableModels(providers, provider, forceServer, status, canUseServer, canUseFree)
  if (!provider.trim() || !model.trim()) return false
  if (allowsUnverified(status, provider, forceServer)) return true
  return models.some((option) => option.id === model && option.verified !== false)
}

export function isAnalysisFormAiUnavailable(
  providers: Record<string, AiModel[]>, status: Record<string, ProviderStatus>,
  primary: { ai_provider: string; ai_model: string }, peers: { ai_provider: string; ai_model: string }[],
  enablePeers: boolean, forceServer: boolean, canUseServer: boolean, canUseFree = false, deferPrimary = false,
): boolean {
  return (!deferPrimary && !isAnalysisAiAvailable(providers, status, primary.ai_provider, primary.ai_model, forceServer, canUseServer, canUseFree)) ||
    (enablePeers && (peers.length === 0 || peers.some((peer) => !isAnalysisAiAvailable(providers, status, peer.ai_provider, peer.ai_model, forceServer, canUseServer, canUseFree))))
}
