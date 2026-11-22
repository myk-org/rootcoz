import { disambiguateProviderLabels, formatProviderModelLabel, normalizeProvider } from '@/lib/aiProviders'
import type { PeerDebate } from '@/types'

/** Unique peer AI labels for header badges — excludes the main AI.
 *  `peer_debate.ai_configs` is stored as `[main, ...peers]`; always skip
 *  index 0. Deduplicate on the raw provider ID and model before formatting so
 *  distinct transports sharing a public label (e.g. google vs google-vertex)
 *  never collapse into a single badge. */
export function getPeerAiLabels(
  debates: Array<{ debate: PeerDebate }>,
  originalIds?: Readonly<Record<string, string>>,
): string[] {
  const uniqueConfigs = new Map<string, { ai_provider: string; ai_model: string }>()
  for (const { debate } of debates) {
    const configs = debate.ai_configs ?? []
    for (let i = 1; i < configs.length; i++) {
      const cfg = configs[i]
      const key = `${normalizeProvider(cfg.ai_provider)}::${cfg.ai_model}`
      if (!uniqueConfigs.has(key)) {
        uniqueConfigs.set(key, cfg)
      }
    }
  }
  const labels = disambiguateProviderLabels(
    [...uniqueConfigs.values()].map((cfg) => cfg.ai_provider),
    originalIds,
  )
  return [...uniqueConfigs.values()]
    .map((cfg) =>
      formatProviderModelLabel(
        cfg.ai_provider,
        cfg.ai_model,
        originalIds,
        '/',
        labels[normalizeProvider(cfg.ai_provider)],
      ),
    )
    .sort()
}
