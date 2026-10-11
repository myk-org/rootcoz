import { Input } from '@/components/ui/input'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { AnalysisProviderSelect, AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { usableModels, allowsUnverified, analysisProviderIds } from '@/lib/analysisAi'
import { useAuth } from '@/lib/auth'
import { toIntInRange } from '@/lib/utils'
import { Plus, Trash2 } from 'lucide-react'
import type { AiConfig } from '@/types'

export type PeerConfigWithId = AiConfig & { id: string }

interface PeerConfigListProps {
  peerConfigs: PeerConfigWithId[]
  setPeerConfigs: React.Dispatch<React.SetStateAction<PeerConfigWithId[]>>
  maxRounds: number
  setMaxRounds: React.Dispatch<React.SetStateAction<number>>
  forceServer?: boolean
}

export function PeerConfigList({
  peerConfigs,
  setPeerConfigs,
  maxRounds,
  setMaxRounds,
  forceServer = false,
}: PeerConfigListProps) {
  const { canUseServerProviders, canUseFreeProviders } = useAuth()
  const { providers, providerStatus } = useProviderCatalog(forceServer)

  const updatePeer = (id: string, patch: Partial<PeerConfigWithId>) => {
    setPeerConfigs((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  }

  // Default a new peer to the first provider the pickers themselves offer.
  const defaultProvider = analysisProviderIds(providers, providerStatus, forceServer)
    .find((id) =>
      usableModels(providers, id, forceServer, providerStatus, canUseServerProviders, canUseFreeProviders).length > 0 ||
      (!forceServer && allowsUnverified(providerStatus, id, false)),
    ) ?? ''

  return (
    <>
      <div className="space-y-2">
        {peerConfigs.map((peer, i) => (
          <div
            key={peer.id}
            className="bg-surface-elevated border border-border-default rounded-lg p-2.5 space-y-2"
          >
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-text-secondary">Peer {i + 1}</span>
              <div className="flex-1" />
              <button
                type="button"
                aria-label={`Remove peer ${i + 1}`}
                className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
                onClick={() => setPeerConfigs((prev) => prev.filter((p) => p.id !== peer.id))}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="space-y-1.5">
              <FieldLabel>AI Provider</FieldLabel>
              <AnalysisProviderSelect
                value={peer.ai_provider}
                onChange={(v) => updatePeer(peer.id, { ai_provider: v, ai_model: '' })}
                forceServer={forceServer}
                label={`Peer ${i + 1} provider`}
              />
            </div>
            <div className="space-y-1.5">
              <FieldLabel>AI Model</FieldLabel>
              <AnalysisModelSelect
                provider={peer.ai_provider}
                value={peer.ai_model}
                onChange={(val) => updatePeer(peer.id, { ai_model: val })}
                forceServer={forceServer}
                label={`Peer ${i + 1} model`}
              />
            </div>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
        onClick={() =>
          setPeerConfigs((prev) => [
            ...prev,
            { id: crypto.randomUUID(), ai_provider: defaultProvider, ai_model: '' },
          ])
        }
      >
        <Plus className="h-3.5 w-3.5" />
        Add Peer
      </button>
      <div className="space-y-1.5">
        <FieldLabel>Max Rounds</FieldLabel>
        <Input
          type="number"
          min={1}
          max={10}
          value={maxRounds}
          onChange={(e) => setMaxRounds(toIntInRange(e.target.value, 1, 10, 1))}
          className="w-24"
        />
      </div>
    </>
  )
}
