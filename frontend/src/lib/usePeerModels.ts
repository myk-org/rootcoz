import { useState, useEffect, useMemo } from 'react'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { fetchModelsForProvider } from '@/lib/useProviderModels'

export function usePeerModels(
  peerConfigs: PeerConfigWithId[],
  enablePeers: boolean,
): Record<string, ModelOption[]> {
  const [peerModels, setPeerModels] = useState<Record<string, ModelOption[]>>({})

  const peerProvidersKey = useMemo(
    () => peerConfigs.map(p => p.id + ':' + p.ai_provider).join('|'),
    [peerConfigs]
  )

  useEffect(() => {
    if (!enablePeers) return
    let ignore = false
    // Reset all peer models before fetching to clear stale options
    const resetModels: Record<string, ModelOption[]> = {}
    peerConfigs.forEach(p => { resetModels[p.id] = [] })
    setPeerModels(resetModels)
    peerConfigs.forEach((peer) => {
      if (!peer.ai_provider) {
        return
      }
      fetchModelsForProvider(peer.ai_provider)
        .then(m => { if (!ignore) setPeerModels(prev => ({ ...prev, [peer.id]: m })) })
        .catch(() => { if (!ignore) setPeerModels(prev => ({ ...prev, [peer.id]: [] })) })
    })
    return () => { ignore = true }
  }, [enablePeers, peerProvidersKey])

  return peerModels
}
