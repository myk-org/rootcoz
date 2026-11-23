import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { readLastAiPair, subscribeLastAiPair, writeLastAiPair, type AiPair, type AiPairEntry } from '@/lib/lastAiPair'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import type { AiModel, AiModelsResponse, ProviderStatus } from '@/types'

export type { ProviderStatus }

// ============================================================================
// 1. Normalization, folding & labels
// ============================================================================

export type AiProviderOption = { value: string; label: string }

/**
 * Single shared mapping from built-in sidecar / catalog provider IDs to
 * user-facing display labels. Implementation IDs stay in the API contract and
 * option values; pickers and selected-value displays render these labels.
 */
export const KNOWN_PROVIDER_LABELS: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    // Core public capabilities / Vertex & Google aliases
    anthropic: 'Anthropic',
    claude: 'Claude',
    'google-vertex-claude': 'Claude',
    'vertex-claude': 'Claude',
    gemini: 'Gemini',
    google: 'Gemini',
    'google-vertex': 'Gemini',
    // OpenAI & Azure
    openai: 'OpenAI',
    'openai-codex': 'OpenAI Codex',
    azure: 'Azure OpenAI',
    'azure-openai-responses': 'Azure OpenAI',
    // Built-in pi-ai catalog providers
    'amazon-bedrock': 'Amazon Bedrock',
    'ant-ling': 'Ant Ling',
    baseten: 'Baseten',
    cerebras: 'Cerebras',
    'cloudflare-ai-gateway': 'Cloudflare AI Gateway',
    'cloudflare-workers-ai': 'Cloudflare Workers AI',
    deepseek: 'DeepSeek',
    fireworks: 'Fireworks',
    'github-copilot': 'GitHub Copilot',
    groq: 'Groq',
    huggingface: 'Hugging Face',
    'kimi-coding': 'Kimi',
    meta: 'Meta',
    minimax: 'MiniMax',
    'minimax-cn': 'MiniMax (CN)',
    mistral: 'Mistral',
    moonshotai: 'Moonshot AI',
    'moonshotai-cn': 'Moonshot AI (CN)',
    nvidia: 'NVIDIA',
    opencode: 'OpenCode',
    'opencode-go': 'OpenCode Go',
    openrouter: 'OpenRouter',
    'qwen-token-plan': 'Qwen',
    'qwen-token-plan-cn': 'Qwen (CN)',
    'qwen-token-plan-individual': 'Qwen Individual',
    'qwen-token-plan-individual-cn': 'Qwen Individual (CN)',
    radius: 'Radius',
    together: 'Together AI',
    typesafe: 'Typesafe',
    'vercel-ai-gateway': 'Vercel AI Gateway',
    xai: 'xAI',
    xiaomi: 'Xiaomi',
    'xiaomi-token-plan-ams': 'Xiaomi (AMS)',
    'xiaomi-token-plan-cn': 'Xiaomi (CN)',
    'xiaomi-token-plan-sgp': 'Xiaomi (SGP)',
    zai: 'Z.AI',
    'zai-coding-cn': 'Z.AI (CN)',
  }),
)

/**
 * Short qualifiers applied ONLY when two distinct provider IDs share the same
 * public display label in the same option set (e.g. both `google` and
 * `google-vertex` present simultaneously). Never shown when the label is unique.
 */
const LABEL_DISAMBIGUATION_HINTS: Readonly<Record<string, string>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, string>, {
    google: 'API',
    'google-vertex': 'Vertex',
    gemini: 'Default',
    claude: 'Default',
    'google-vertex-claude': 'Vertex',
    'vertex-claude': 'Vertex',
    azure: 'Azure',
    'azure-openai-responses': 'Responses',
  }),
)

export function normalizeProvider(provider: string): string {
  return provider.toLowerCase().trim()
}

function titleCaseProvider(value: string): string {
  return value.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function ownRecordValue(
  record: Readonly<Record<string, string>> | undefined,
  key: string,
): string | undefined {
  return record && Object.hasOwn(record, key) ? record[key] : undefined
}

/** Result of folding catalog record keys through normalizeProvider (lowercase). */
export type FoldedCatalogRecord<T> = {
  folded: Record<string, T>
  /** folded key → exact-case key from the server, for verbatim submission and display. */
  originalIds: Record<string, string>
}

/**
 * Fold a catalog record's keys through normalizeProvider, keeping the exact-case
 * server spelling in `originalIds` and dropping ambiguous case-distinct variants.
 */
export function foldCatalogKeys<T>(
  record: Readonly<Record<string, T>> | undefined,
): FoldedCatalogRecord<T> {
  const seen = new Map<string, string>()
  const folded: Record<string, T> = Object.create(null) as Record<string, T>
  const originalIds: Record<string, string> = Object.create(null) as Record<string, string>
  if (!record) return { folded, originalIds }
  for (const [key, value] of Object.entries(record)) {
    const normalized = normalizeProvider(key)
    const existing = seen.get(normalized)
    if (existing === undefined) {
      seen.set(normalized, key)
      folded[normalized] = value
      originalIds[normalized] = key
    } else if (existing !== key) {
      delete folded[normalized]
      delete originalIds[normalized]
    }
  }
  return { folded, originalIds }
}

/** Extract folded-key → exact-case-key map from a raw catalog providers record. */
export function extractOriginalProviderIds(
  record: Readonly<Record<string, unknown>> | undefined,
): Record<string, string> {
  return foldCatalogKeys(record).originalIds
}

/**
 * Format a provider ID for user-facing display (pickers, selected chips,
 * active session headers, report badges).
 *
 * Resolution order:
 * 1. Pre-computed collision-aware map (`disambiguatedLabels`) when provided.
 * 2. Built-in sidecar ID mapping (`KNOWN_PROVIDER_LABELS`).
 * 3. Operator-configured custom provider from the catalog (`originalIds`):
 *    preserves explicit mixed casing verbatim, or title-cases all-lowercase
 *    custom names.
 * 4. Mixed-case custom provider name before the catalog loads (preserved verbatim).
 * 5. Unmapped non-catalog IDs pass through raw instead of being silently
 *    disguised as derived public labels.
 */
export function formatProviderLabel(
  provider: string,
  originalIds?: Readonly<Record<string, string>>,
  disambiguatedLabels?: Readonly<Record<string, string>>,
): string {
  const trimmed = provider.trim()
  if (!trimmed) return ''
  const key = normalizeProvider(trimmed)
  const disambiguated = ownRecordValue(disambiguatedLabels, key)
  if (disambiguated !== undefined) return disambiguated

  const known = ownRecordValue(KNOWN_PROVIDER_LABELS, key)
  if (known !== undefined) return known

  const catalogSpelling = ownRecordValue(originalIds, key)
  if (catalogSpelling !== undefined) {
    return /[A-Z]/.test(catalogSpelling) ? catalogSpelling : titleCaseProvider(catalogSpelling)
  }

  return trimmed
}

/**
 * Collect non-empty provider IDs from any mix of single values and iterables.
 * Shared across components so provider-label disambiguation uses one canonical
 * collection step instead of repeating inline array construction.
 */
export function collectProviderIds(
  ...sources: ReadonlyArray<Iterable<string | null | undefined> | string | null | undefined>
): string[] {
  const ids: string[] = []
  for (const source of sources) {
    if (!source) continue
    if (typeof source === 'string') {
      const trimmed = source.trim()
      if (trimmed) ids.push(trimmed)
      continue
    }
    for (const raw of source) {
      if (!raw) continue
      const trimmed = raw.trim()
      if (trimmed) ids.push(trimmed)
    }
  }
  return ids
}

/**
 * Resolve display labels for a set of distinct provider IDs, adding a short
 * transport qualifier only when two distinct IDs collide on the same base label.
 */
export function disambiguateProviderLabels(
  ids: Iterable<string | null | undefined>,
  originalIds?: Readonly<Record<string, string>>,
): Record<string, string> {
  const uniqueIds = new Set<string>()
  const rawSpellings = new Map<string, string>()
  for (const raw of ids) {
    if (!raw) continue
    const trimmed = raw.trim()
    if (!trimmed) continue
    const normalized = normalizeProvider(trimmed)
    uniqueIds.add(normalized)
    if (!rawSpellings.has(normalized)) {
      rawSpellings.set(normalized, trimmed)
    }
  }

  const byLabel = new Map<string, string[]>()
  const baseLabels = new Map<string, string>()
  for (const id of uniqueIds) {
    const label = formatProviderLabel(rawSpellings.get(id) ?? id, originalIds)
    baseLabels.set(id, label)
    const group = byLabel.get(label)
    if (group) group.push(id)
    else byLabel.set(label, [id])
  }

  const resolved: Record<string, string> = Object.create(null) as Record<string, string>
  for (const [id, base] of baseLabels) {
    const collisions = byLabel.get(base)
    if (collisions && collisions.length > 1) {
      const hint = ownRecordValue(LABEL_DISAMBIGUATION_HINTS, id) ?? id
      resolved[id] = `${base} (${hint})`
    } else {
      resolved[id] = base
    }
  }
  return resolved
}

/**
 * Format a provider+model display pair consistently across headers, chips,
 * tooltips, and badges. Uses `formatProviderLabel` and handles optional models.
 */
export function formatProviderModelLabel(
  provider: string | undefined,
  model?: string | undefined,
  originalIds?: Readonly<Record<string, string>>,
  separator = ' / ',
  disambiguatedLabels?: Readonly<Record<string, string>>,
): string {
  const label = provider ? formatProviderLabel(provider, originalIds, disambiguatedLabels) : ''
  if (!label) return ''
  return model ? `${label}${separator}${model}` : label
}

/** Providers advertised by the server, plus any legacy current selection. */
export function buildProviderOptions(
  providerKeys: Iterable<string>,
  currentValues: Iterable<string> = [],
  originalIds?: Readonly<Record<string, string>>,
  disambiguatedLabels?: Readonly<Record<string, string>>,
): AiProviderOption[] {
  const combined = collectProviderIds(providerKeys, currentValues)
  const fallbackLabels = disambiguatedLabels
    ? undefined
    : disambiguateProviderLabels(combined, originalIds)
  const uniqueKeys = new Set<string>()
  for (const raw of combined) {
    uniqueKeys.add(normalizeProvider(raw))
  }
  return [...uniqueKeys]
    .map((value) => ({
      value,
      label:
        ownRecordValue(disambiguatedLabels, value)
        ?? ownRecordValue(fallbackLabels, value)
        ?? formatProviderLabel(value, originalIds, disambiguatedLabels),
    }))
    .sort((a, b) => a.label.localeCompare(b.label) || a.value.localeCompare(b.value))
}

// ============================================================================
// 2. Availability, grants & credential filtering
// ============================================================================

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

export function usableModels(
  providers: Record<string, AiModel[]>,
  provider: string,
  forceServer: boolean,
  status: Record<string, ProviderStatus> = {},
  canUseServer = true,
  canUseFree = false,
): AiModel[] {
  // A user without the server grant may still use an admin-declared free
  // pair when they hold the free-providers grant; free models pass the
  // server-credential filter in both toggle states.
  const serverAllowed = (model: AiModel) => canUseServer || (canUseFree && model.free === true)
  return (providers[normalizeProvider(provider)] ?? []).filter((model) =>
    model.credential_sources?.some((source) =>
      forceServer
        ? serverAllowed(model) && source === 'server'
        : source === 'user' || (serverAllowed(model) && source === 'server'),
    ) &&
    (model.verified !== false || (forceServer && model.credential_sources?.includes('server')) || allowsUnverified(status, provider, forceServer)),
  ).map((model) =>
    forceServer && model.verified === false && model.credential_sources?.includes('server')
      ? { ...model, verified: true }
      : model,
  )
}

export function visibleModels(
  providers: Record<string, AiModel[]>,
  provider: string,
  forceServer: boolean,
  status: Record<string, ProviderStatus>,
  canUseServer: boolean,
  canUseFree = false,
) {
  const available = usableModels(providers, provider, forceServer, status, canUseServer, canUseFree)
  const all = usableModels(providers, provider, forceServer, status)
  return all.map((model) => ({ ...model, serverLocked: !available.some((item) => item.id === model.id) }))
}

export function analysisProviderIds(
  providers: Record<string, AiModel[]>,
  status: Record<string, ProviderStatus>,
  forceServer: boolean,
): string[] {
  return [...new Set([...Object.keys(providers), ...Object.keys(status)])].filter((id) =>
    usableModels(providers, id, forceServer, status).length > 0 || allowsUnverified(status, id, forceServer),
  )
}

export function credentialLabel(models: AiModel[], manualUser = false, forceServer = false): string {
  const sources = new Set(models.flatMap((model) => model.credential_sources ?? []).filter((source) => !forceServer || source === 'server'))
  if (manualUser) sources.add('user')
  return sources.has('user') && sources.has('server') ? 'User + Server' : sources.has('user') ? 'User' : 'Server'
}

export function isAnalysisAiAvailable(
  providers: Record<string, AiModel[]>,
  status: Record<string, ProviderStatus>,
  provider: string,
  model: string,
  forceServer: boolean,
  canUseServer = true,
  canUseFree = false,
): boolean {
  if (forceServer && !canUseServer && !canUseFree) return false
  const models = usableModels(providers, provider, forceServer, status, canUseServer, canUseFree)
  if (!provider.trim() || !model.trim()) return false
  // A pair the sidecar would refuse to open a session for is not available:
  // a saved or preselected pair must not enable Start just because it is listed
  // or because the provider's listing is unavailable.
  if (models.some((option) => option.id === model && option.session_supported === false)) return false
  if (allowsUnverified(status, provider, forceServer)) return true
  return models.some((option) => option.id === model && option.verified !== false)
}

/**
 * Which part of a pair the loaded catalog cannot serve, so a notice can name it: the
 * provider when the catalog does not list it, the model when it lists the provider
 * but not a usable model, and the session when the model is listed yet the sidecar
 * refuses to open a session for it.
 */
export type AnalysisAiUnavailablePart = 'provider' | 'model' | 'session'

export function analysisAiUnavailablePart(
  providers: Record<string, AiModel[]>,
  status: Record<string, ProviderStatus>,
  provider: string,
  model: string,
  canUseServer = true,
  canUseFree = false,
): AnalysisAiUnavailablePart {
  const id = normalizeProvider(provider)
  // `providers` is what the picker offers; a provider_status key alone is not
  // enough to consider the provider available (#591).
  if (!(id in providers)) return 'provider'
  const listed = usableModels(providers, provider, false, status, canUseServer, canUseFree)
  return listed.some((option) => option.id === model && option.session_supported === false) ? 'session' : 'model'
}

export function isAnalysisFormAiUnavailable(
  providers: Record<string, AiModel[]>,
  status: Record<string, ProviderStatus>,
  primary: { ai_provider: string; ai_model: string },
  peers: { ai_provider: string; ai_model: string }[],
  enablePeers: boolean,
  forceServer: boolean,
  canUseServer: boolean,
  canUseFree = false,
  deferPrimary = false,
): boolean {
  return (!deferPrimary && !isAnalysisAiAvailable(providers, status, primary.ai_provider, primary.ai_model, forceServer, canUseServer, canUseFree)) ||
    (enablePeers && (peers.length === 0 || peers.some((peer) => !isAnalysisAiAvailable(providers, status, peer.ai_provider, peer.ai_model, forceServer, canUseServer, canUseFree))))
}

// ============================================================================
// 3. Shared catalog cache & hooks
// ============================================================================

export type CatalogState = {
  providerKeys: string[]
  providers: AiModelsResponse['providers']
  /** @deprecated Use providerKeys. */
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
  /** folded provider id → exact-case catalog id. */
  originalIds: Record<string, string>
  /** The request has answered (even with nothing) — not "still loading". */
  settled: boolean
  /** The request succeeded — an empty provider list is an answer, a failure is not. */
  loaded: boolean
}

const EMPTY_CATALOG: CatalogState = { providerKeys: [], providers: {}, enabled: [], providerStatus: {}, originalIds: {}, settled: false, loaded: false }
/** A request that answered with nothing: terminal, unlike `EMPTY_CATALOG`. */
const SETTLED_EMPTY_CATALOG: CatalogState = { ...EMPTY_CATALOG, settled: true }

/** Shared in-flight / completed catalog so concurrent hook mounts share one fetch. */
const catalogInflight = new Map<string, Promise<CatalogState>>()
const catalogCache = new Map<string, CatalogState>()
let catalogGeneration = 0
/** Mounted useProviderCatalog consumers — notified on cache reset. */
const catalogSubscribers = new Set<() => void>()

function catalogKeyFor(username: string, isAdmin: boolean, authenticated: boolean): string {
  if (!authenticated || !username) return 'anon'
  return `${username}:${isAdmin ? '1' : '0'}`
}

/*
 * Provider IDs are case-sensitive in the sidecar, but every consumer looks
 * providers up lowercased, so keys are folded once at load via foldCatalogKeys.
 * Exact-case IDs are kept in originalIds for verbatim submission and display;
 * colliding case-distinct variants are dropped.
 */

function loadProviderCatalog(cacheKey: string): Promise<CatalogState> {
  const cached = catalogCache.get(cacheKey)
  if (cached) return Promise.resolve(cached)
  const inflight = catalogInflight.get(cacheKey)
  if (inflight) return inflight
  const generation = catalogGeneration
  const req = api
    .get<AiModelsResponse>('/api/ai-models')
    .then((res) => {
      const { folded: providers, originalIds } = foldCatalogKeys(res.providers ?? {})
      const providerStatus = foldCatalogKeys(res.provider_status ?? {}).folded
      const providerKeys = Object.keys(providers)
      const next: CatalogState = {
        providerKeys,
        providers,
        enabled: providerKeys.filter((p) => (providers[p] ?? []).length > 0),
        providerStatus,
        originalIds,
        settled: true,
        loaded: true,
      }
      if (generation === catalogGeneration && catalogInflight.get(cacheKey) === req) {
        catalogCache.set(cacheKey, next)
        catalogInflight.delete(cacheKey)
      }
      return next
    })
    .catch((err) => {
      if (catalogInflight.get(cacheKey) === req) catalogInflight.delete(cacheKey)
      throw err
    })
  catalogInflight.set(cacheKey, req)
  return req
}

/**
 * Clear shared catalog cache and notify mounted useProviderCatalog consumers
 * so they refetch (login/logout/admin refresh/tests).
 */
export function resetProviderCatalogCache(): void {
  catalogGeneration++
  catalogInflight.clear()
  catalogCache.clear()
  // Wake every mounted hook — cacheKey alone does not change on refresh.
  for (const notify of [...catalogSubscribers]) {
    notify()
  }
}

/** @deprecated Prefer resetProviderCatalogCache — kept for existing tests. */
export function _resetProviderCatalogCacheForTests(): void {
  resetProviderCatalogCache()
}

/** Provider ids that currently have at least one discovered model. */
export function useEnabledProviders(): string[] {
  const { enabled } = useProviderCatalog()
  return enabled
}

/**
 * Models catalog + per-provider listing capability status.
 *
 * The catalog already carries a `credential_sources` list per model and the
 * per-user server grant, so one request serves every consumer: a granted user
 * sees server and own-key models, everyone else own-key (and free) models.
 */
export function useProviderCatalog(): CatalogState {
  const { username, isAdmin, authenticated } = useAuth()
  const cacheKey = catalogKeyFor(username, isAdmin, authenticated)
  const [reloadToken, setReloadToken] = useState(0)
  const [state, setState] = useState<{ key: string; catalog: CatalogState }>(() => ({
    key: cacheKey,
    catalog: catalogCache.get(cacheKey) ?? EMPTY_CATALOG,
  }))

  useEffect(() => {
    const notify = () => setReloadToken((n) => n + 1)
    catalogSubscribers.add(notify)
    return () => {
      catalogSubscribers.delete(notify)
    }
  }, [])

  useEffect(() => {
    let ignore = false
    const generation = catalogGeneration
    loadProviderCatalog(cacheKey)
      .then((next) => {
        if (!ignore && generation === catalogGeneration) setState({ key: cacheKey, catalog: next })
      })
      .catch(() => {
        if (!ignore && generation === catalogGeneration) setState({ key: cacheKey, catalog: SETTLED_EMPTY_CATALOG })
      })
    return () => {
      ignore = true
    }
  }, [cacheKey, reloadToken])

  return state.key === cacheKey ? state.catalog : catalogCache.get(cacheKey) ?? EMPTY_CATALOG
}

/**
 * Provider dropdown options: catalog providers plus any current selection.
 */
export function useProviderOptions(
  currentValues: string | string[] | undefined = undefined,
): AiProviderOption[] {
  const { providerKeys, originalIds } = useProviderCatalog()
  const currentKey = Array.isArray(currentValues)
    ? currentValues.join('\0')
    : (currentValues ?? '')

  return useMemo(() => {
    const current = currentKey ? currentKey.split('\0') : []
    return buildProviderOptions(providerKeys, current, originalIds)
  }, [providerKeys, currentKey, originalIds])
}

/**
 * Memoized collision-aware provider label map from explicit catalog IDs plus
 * any additional provider IDs (session, message, peer, or report history).
 */
export function useProviderLabels(
  catalogIds: Iterable<string | null | undefined>,
  extraIds: Iterable<string | null | undefined> = [],
  originalIds?: Readonly<Record<string, string>>,
): Record<string, string> {
  const serializedIds = collectProviderIds(catalogIds, extraIds).join('\0')
  return useMemo(
    () => disambiguateProviderLabels(serializedIds ? serializedIds.split('\0') : [], originalIds),
    [serializedIds, originalIds],
  )
}

/**
 * Live catalog + memoized collision-aware provider labels covering all catalog
 * keys plus any extra provider IDs referenced by the calling view.
 */
export function useCatalogProviderLabels(
  extraIds: Iterable<string | null | undefined> = [],
) {
  const catalog = useProviderCatalog()
  const catalogKeys = catalog.providerKeys ?? Object.keys(catalog.providers ?? {})
  const providerLabels = useProviderLabels(catalogKeys, extraIds, catalog.originalIds)
  return { ...catalog, providerLabels }
}

// ============================================================================
// 4. Per-provider model listing & admin refresh
// ============================================================================

/**
 * Fetch available models for a single AI provider.
 * Returns an empty array if `provider` is falsy.
 */
export async function fetchModelsForProvider(provider: string): Promise<ModelOption[]> {
  const normalized = normalizeProvider(provider)
  if (!normalized) return []
  const res = await api.get<{ models: ModelOption[] }>(
    `/api/ai-models?provider=${encodeURIComponent(normalized)}`,
  )
  return res.models ?? []
}

/**
 * Provider models with admin refresh support.
 * Resets the shared catalog cache synchronously after refresh so mounted
 * `useProviderCatalog` consumers refetch without a dynamic import boundary.
 */
export function useProviderModels(provider: string) {
  const normalized = normalizeProvider(provider)
  const [state, setState] = useState<{ provider: string; models: ModelOption[] }>({
    provider: '',
    models: [],
  })
  const [refreshing, setRefreshing] = useState(false)
  const providerRef = useRef(normalized)
  const providerSeqRef = useRef(0)
  const refreshSeqRef = useRef(0)
  const mountedRef = useRef(false)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    providerRef.current = normalized
    providerSeqRef.current++
    if (!normalized) return
    let ignore = false
    const seq = refreshSeqRef.current
    fetchModelsForProvider(normalized)
      .then((models) => {
        if (!ignore && refreshSeqRef.current === seq) {
          setState({ provider: normalized, models })
        }
      })
      .catch(() => {
        if (!ignore && refreshSeqRef.current === seq) {
          setState({ provider: normalized, models: [] })
        }
      })
    return () => {
      ignore = true
    }
  }, [normalized])

  const refresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current
    const currentProvider = providerRef.current
    const startProviderSeq = providerSeqRef.current
    setRefreshing(true)
    try {
      await api.post('/api/admin/ai-models/refresh')
      resetProviderCatalogCache()
      if (
        mountedRef.current &&
        currentProvider &&
        providerRef.current === currentProvider &&
        providerSeqRef.current === startProviderSeq &&
        refreshSeqRef.current === seq
      ) {
        const models = await fetchModelsForProvider(currentProvider)
        if (
          mountedRef.current &&
          providerRef.current === currentProvider &&
          providerSeqRef.current === startProviderSeq &&
          refreshSeqRef.current === seq
        ) {
          setState({ provider: currentProvider, models })
        }
      }
    } catch (err) {
      console.error('Failed to refresh AI models:', err)
    } finally {
      if (mountedRef.current && refreshSeqRef.current === seq) {
        setRefreshing(false)
      }
    }
  }, [])

  return {
    models: state.provider === normalized ? state.models : [],
    refresh,
    refreshing,
  }
}

// ============================================================================
// 5. Remembered user AI pair selection
// ============================================================================

/**
 * The last AI pair this user picked, shared by every AI-assisted surface (issue
 * creation, comment intent, feedback, chat) and remembered across page loads.
 * Three states: nothing remembered falls back to the surface defaults, an explicitly
 * cleared pair means the server default ('' — fallbacks ignored), and a remembered
 * pair is used as-is. A half pair (provider picked, model not yet, or a model still
 * being typed) is a draft local to this mount and is never persisted.
 *
 * `respectCleared: false` treats a cleared pair as "use my fallbacks" instead, for
 * surfaces whose fallbacks are a pair to run (chat's server config, the new-analysis
 * form's server default).
 * A fallback is judged by the same catalog rule as a remembered pair: one the server
 * cannot serve is no selection at all (#591), and the stored default is never written.
 */
export function useAiSelection(
  fallbackProvider = '',
  fallbackModel = '',
  { respectCleared = true }: { respectCleared?: boolean } = {},
) {
  const {
    username, canUseServerProviders, canUseFreeProviders, authenticated, loading: authLoading,
  } = useAuth()
  const { providers, providerKeys, providerStatus, settled: catalogSettled } = useProviderCatalog()
  // An empty or failed /api/ai-models says nothing about any pair.
  const catalogLoaded = providerKeys.length > 0
  // Re-reads on every write for this user, so all mounted surfaces stay in sync.
  const entry = useSyncExternalStore(
    useCallback((onChange) => subscribeLastAiPair(username, onChange), [username]),
    useCallback(() => readLastAiPair(username), [username]),
  )
  // The in-progress pick, tagged with the entry it was derived from: any write for
  // this user (from here or another surface) and any username change drops it, so a
  // stale draft can never shadow the remembered pair.
  const [draft, setDraft] = useState<{ entry: AiPairEntry | null; pair: AiPair } | null>(null)
  const remembered = entry?.pair ?? null
  const clearedIgnored = !respectCleared && remembered?.provider === '' && remembered.model === ''
  const sel = draft && draft.entry === entry ? draft.pair : (clearedIgnored ? null : remembered)
  // Writes go to the user this selection was read for, never to whatever username
  // is current — an in-tab logout→login must not copy a pair into the next account.
  const owner = entry?.owner ?? username

  const setAiPair = useCallback((provider: string, model: string, { commit = true } = {}) => {
    const pair = { provider: normalizeProvider(provider), model }
    if (provider && model) {
      // Keystrokes in the model combobox arrive per character; only a pick, blur or
      // Enter may store them, or a half-typed model id becomes the remembered pair.
      if (commit) {
        writeLastAiPair(owner, pair)
        setDraft(null)
      } else setDraft({ entry, pair })
    } else if (provider || model) {
      // Mid-selection: keep the previous entry, the user has not finished picking.
      setDraft({ entry, pair })
    } else if (commit) {
      // "Server default" is a decision, so it is stored, not left absent.
      writeLastAiPair(owner, pair)
      setDraft(null)
    } else setDraft({ entry, pair })
  }, [entry, owner])

  // The one availability rule, shared by a remembered pair and a surface fallback:
  // a pair the loaded catalog cannot serve is not a selection (#591).
  const usable = useCallback((provider: string, model: string) => (
    catalogLoaded && isAnalysisAiAvailable(providers, providerStatus, provider, model, false, canUseServerProviders, canUseFreeProviders)
  ), [catalogLoaded, providers, providerStatus, canUseServerProviders, canUseFreeProviders])

  // A remembered pair this user can no longer use would be a dead selection, so it is
  // dropped and the surface falls back to its own default. Judged on the entry only —
  // a draft is this mount's business and must not keep the check from running.
  const pair = entry?.pair
  useEffect(() => {
    if (!entry || entry.owner !== username) return
    if (!pair || !pair.provider || !pair.model) return
    // An empty or failed /api/ai-models must not wipe a good pair, and the server
    // grants are still false until /api/auth/me resolves.
    if (!catalogLoaded || !authenticated || authLoading) return
    if (usable(pair.provider, pair.model)) return
    console.debug('[ai-pair] dropped unavailable remembered pair', `${pair.provider}/${pair.model}`)
    // Removing the entry is enough: any draft keyed to it stops being shown.
    writeLastAiPair(entry.owner, null)
  }, [entry, pair, username, catalogLoaded, authenticated, authLoading, usable])

  // The fallback (a surface's own default, or the stored server default) is applied
  // only once the catalog proves this server serves it. Before that nothing is applied:
  // guessing would issue a model listing for a provider absent from the catalog, which
  // the server answers 400 — the bug this guard exists to prevent. The stored default is
  // read, never written, cleared or replaced here.
  const fallbackApplied = fallbackProvider === '' || usable(fallbackProvider, fallbackModel)
  useEffect(() => {
    if (fallbackProvider === '' || fallbackApplied || !catalogLoaded || authLoading) return
    console.debug('[ai-pair] ignored an unavailable default pair', `${fallbackProvider}/${fallbackModel}`)
  }, [fallbackApplied, fallbackProvider, fallbackModel, catalogLoaded, authLoading])

  return {
    aiProvider: sel ? sel.provider : fallbackApplied ? normalizeProvider(fallbackProvider) : '',
    aiModel: sel ? sel.model : fallbackApplied ? fallbackModel : '',
    // A supplied fallback is undecided, not absent, until the catalog answers _and_
    // /api/auth/me resolves the grants `usable()` reads: a surface that sends the pair
    // (issue preview) must wait instead of posting an empty one (#591).
    aiPending: fallbackProvider !== '' && !sel && (!catalogSettled || authLoading),
    setAiPair,
  }
}
