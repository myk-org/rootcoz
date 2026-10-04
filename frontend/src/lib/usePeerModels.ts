import { useState, useEffect, useMemo } from 'react'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import type { PeerConfigWithId } from '@/components/shared/PeerConfigList'
import { fetchModelsForProvider } from '@/lib/useProviderModels'

interface PeerModelState {
  /** `peerProvidersKey` the models were fetched for (null while peers are disabled). */
  key: string | null
  /** The peer configs `key` was built from — kept alongside it so the fetch
   *  effect depends on a stable array instead of the caller's per-render one. */
  peers: PeerConfigWithId[]
  models: Record<string, ModelOption[]>
}

export function usePeerModels(
  peerConfigs: PeerConfigWithId[],
  enablePeers: boolean,
): Record<string, ModelOption[]> {
  const peerProvidersKey = useMemo(
    () => peerConfigs.map(p => p.id + ':' + p.ai_provider).join('|'),
    [peerConfigs]
  )

  // Options fetched for a previous peerProvidersKey are stale. Resetting them
  // while rendering (rather than from an effect) means no stale list is ever
  // painted, and re-enabling peers clears the options exactly as before.
  const fetchKey = enablePeers ? peerProvidersKey : null
  const [state, setState] = useState<PeerModelState>({ key: null, peers: peerConfigs, models: {} })
  if (state.key !== fetchKey) {
    const cleared: Record<string, ModelOption[]> = {}
    if (fetchKey !== null) {
      for (const p of peerConfigs) cleared[p.id] = []
    }
    setState({ key: fetchKey, peers: peerConfigs, models: cleared })
  }
  const peers = state.peers

  useEffect(() => {
    if (!enablePeers) return
    let ignore = false
    peers.forEach((peer) => {
      if (!peer.ai_provider) {
        return
      }
      fetchModelsForProvider(peer.ai_provider)
        .then(m => { if (!ignore) setState(prev => ({ ...prev, models: { ...prev.models, [peer.id]: m } })) })
        .catch(() => { if (!ignore) setState(prev => ({ ...prev, models: { ...prev.models, [peer.id]: [] } })) })
    })
    return () => { ignore = true }
  }, [enablePeers, peerProvidersKey, peers])

  return state.models
}
