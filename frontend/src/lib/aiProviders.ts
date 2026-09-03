export type AiProviderOption = { value: string; label: string }

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

/** Providers advertised by the server, plus any legacy current selection. */
export function buildProviderOptions(
  providerKeys: Iterable<string>,
  currentValues: Iterable<string> = [],
): AiProviderOption[] {
  const providers = new Set(
    [...providerKeys, ...currentValues]
      .filter(Boolean)
      .map((p) => normalizeProvider(p)),
  )
  return [...providers]
    .sort((a, b) => a.localeCompare(b))
    .map((value) => ({
      value,
      label: value.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    }))
}
