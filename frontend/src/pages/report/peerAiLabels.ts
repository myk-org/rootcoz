import type { PeerDebate } from '@/types'

/** Friendly `provider/model` label for one debate AI config. */
function aiConfigLabel(cfg: { ai_provider: string; ai_model: string }): string {
  return `${cfg.ai_provider}/${cfg.ai_model}`
}

/** Unique peer AI labels for header badges — excludes the main AI.
 *  `peer_debate.ai_configs` is stored as `[main, ...peers]`; always skip
 *  index 0. Do not filter by label equality — a peer may share the main's
 *  provider/model and must still appear in the badges. */
export function getPeerAiLabels(debates: Array<{ debate: PeerDebate }>): string[] {
  const seen = new Set<string>()
  for (const { debate } of debates) {
    const configs = debate.ai_configs ?? []
    for (let i = 1; i < configs.length; i++) {
      seen.add(aiConfigLabel(configs[i]))
    }
  }
  return [...seen].sort()
}
