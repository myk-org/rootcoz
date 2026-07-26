export type AiProviderOption = { value: string; label: string }

/** Canonical providers only — CLI models appear under these when CLI_AGENTS is set. */
export const AI_PROVIDER_OPTIONS: readonly AiProviderOption[] = [
  { value: 'claude', label: 'Claude' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'cursor', label: 'Cursor' },
]

/** @deprecated Use AI_PROVIDER_OPTIONS */
export const BASE_AI_PROVIDER_OPTIONS = AI_PROVIDER_OPTIONS

/** Legacy aliases accepted from older settings / URLs. */
const LEGACY_PROVIDER_ALIASES: Record<string, string> = {
  'cursor-cli': 'cursor',
  'claude-cli': 'claude',
  'gemini-cli': 'gemini',
}

export function normalizeProvider(provider: string): string {
  const p = provider.toLowerCase().trim()
  return LEGACY_PROVIDER_ALIASES[p] ?? p
}

/** Only providers with models (plus any currently selected values). */
export function buildProviderOptions(
  enabledProviders: Iterable<string>,
  currentValues: Iterable<string> = [],
): AiProviderOption[] {
  const enabled = new Set(
    [...enabledProviders, ...currentValues]
      .filter(Boolean)
      .map((p) => normalizeProvider(p)),
  )
  return AI_PROVIDER_OPTIONS.filter((o) => enabled.has(o.value))
}
