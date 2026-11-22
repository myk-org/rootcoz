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
 * Resolve display labels for a set of distinct provider IDs, adding a short
 * transport qualifier only when two distinct IDs collide on the same base label.
 */
export function disambiguateProviderLabels(
  ids: Iterable<string>,
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
): AiProviderOption[] {
  const labels = disambiguateProviderLabels([...providerKeys, ...currentValues], originalIds)
  return Object.keys(labels)
    .map((value) => ({ value, label: labels[value] }))
    .sort((a, b) => a.label.localeCompare(b.label) || a.value.localeCompare(b.value))
}
