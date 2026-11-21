export type AiProviderOption = { value: string; label: string }

export function normalizeProvider(provider: string): string {
  return provider.toLowerCase().trim()
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
