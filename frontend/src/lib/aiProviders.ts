export type AiProviderOption = { value: string; label: string }

/**
 * Single shared mapping from built-in sidecar / catalog provider IDs to
 * user-facing display labels. Implementation IDs stay in the API contract and
 * option values; pickers and selected-value chips render these labels instead.
 */
export const KNOWN_PROVIDER_LABELS: Readonly<Record<string, string>> = {
  // Core public capabilities / Vertex & Google aliases
  anthropic: 'Anthropic',
  claude: 'Claude',
  'google-vertex-claude': 'Claude',
  'vertex-claude': 'Claude',
  gemini: 'Gemini',
  google: 'Gemini',
  'google-vertex': 'Gemini',
  cursor: 'Cursor',
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
}

export function normalizeProvider(provider: string): string {
  return provider.toLowerCase().trim()
}

function titleCaseProvider(value: string): string {
  return value.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * Format any provider ID for user-facing display (dropdown options, selected
 * chips, active session headers, report badges).
 *
 * Built-in sidecar IDs map through KNOWN_PROVIDER_LABELS; legacy transport
 * wrappers (`acpx-*`, `cli-*`, `*-cli`) are stripped before lookup; operator-
 * configured custom providers preserve their catalog casing via `originalIds`.
 */
export function formatProviderLabel(
  provider: string,
  originalIds?: Readonly<Record<string, string>>,
): string {
  const trimmed = provider.trim()
  if (!trimmed) return ''
  const key = normalizeProvider(trimmed)
  if (key in KNOWN_PROVIDER_LABELS) return KNOWN_PROVIDER_LABELS[key]

  const unwrapped = key.replace(/^(?:acpx|cli)-/, '').replace(/-cli$/, '')
  if (unwrapped && unwrapped !== key) {
    if (unwrapped in KNOWN_PROVIDER_LABELS) return KNOWN_PROVIDER_LABELS[unwrapped]
    return titleCaseProvider(unwrapped)
  }

  const exact = originalIds?.[key] ?? trimmed
  // Preserve explicit operator casing from models.json (e.g. mixed-case gateways)
  // so picker options and selected-value chips stay identical.
  if (/[A-Z]/.test(exact)) return exact
  return titleCaseProvider(exact)
}

/** Providers advertised by the server, plus any legacy current selection. */
export function buildProviderOptions(
  providerKeys: Iterable<string>,
  currentValues: Iterable<string> = [],
  originalIds?: Readonly<Record<string, string>>,
): AiProviderOption[] {
  const effectiveOriginals: Record<string, string> = { ...originalIds }
  const providers = new Set<string>()
  for (const raw of [...providerKeys, ...currentValues]) {
    if (!raw) continue
    const trimmed = raw.trim()
    if (!trimmed) continue
    const normalized = normalizeProvider(trimmed)
    providers.add(normalized)
    if (!(normalized in effectiveOriginals)) {
      effectiveOriginals[normalized] = trimmed
    }
  }
  return [...providers]
    .map((value) => ({
      value,
      label: formatProviderLabel(value, effectiveOriginals),
    }))
    .sort((a, b) => a.label.localeCompare(b.label) || a.value.localeCompare(b.value))
}
