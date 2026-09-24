import { Input } from '@/components/ui/input'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { FieldLabel } from '@/components/shared/FieldLabel'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { useProviderOptions, useProviderCatalog } from '@/lib/useProviderOptions'
import { usableModels, credentialLabel, allowsUnverified, analysisProviderIds } from '@/lib/analysisAi'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'
import { toIntInRange } from '@/lib/utils'
import { Plus, Trash2 } from 'lucide-react'
import type { AiConfig } from '@/types'

export type PeerConfigWithId = AiConfig & { id: string }

interface PeerConfigListProps {
  peerConfigs: PeerConfigWithId[]
  setPeerConfigs: React.Dispatch<React.SetStateAction<PeerConfigWithId[]>>
  peerModels: Record<string, ModelOption[]>
  maxRounds: number
  setMaxRounds: React.Dispatch<React.SetStateAction<number>>
  forceServer?: boolean
  strict?: boolean
}

export function PeerConfigList({
  peerConfigs,
  setPeerConfigs,
  peerModels,
  maxRounds,
  setMaxRounds,
  forceServer = false,
  strict = false,
}: PeerConfigListProps) {
  const legacyOptions = useProviderOptions(peerConfigs.map((p) => p.ai_provider))
  const { providers, providerStatus } = useProviderCatalog()
  const providerOptions = strict
    ? buildProviderOptions(analysisProviderIds(providers, providerStatus, forceServer))
    : legacyOptions

  const updatePeer = (id: string, patch: Partial<PeerConfigWithId>) => {
    setPeerConfigs((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  }

  return (
    <>
      <div className="space-y-2">
        {peerConfigs.map((peer, i) => (
          <div
            key={peer.id}
            className="bg-surface-elevated border border-border-default rounded-lg p-2.5 space-y-2"
          >
            <div className="flex items-center gap-2">
              <Select
                value={(!strict || providerOptions.some((option) => option.value === normalizeProvider(peer.ai_provider))) ? normalizeProvider(peer.ai_provider) || undefined : undefined}
                onValueChange={(v) => updatePeer(peer.id, { ai_provider: v, ai_model: '' })}
              >
                <SelectTrigger className="w-[160px]" aria-label={`Peer ${i + 1} provider`}>
                  <SelectValue placeholder={peer.ai_provider ? `${peer.ai_provider} (unavailable)` : 'Select provider'} />
                </SelectTrigger>
                <SelectContent>
                  {providerOptions.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}{strict ? ` · ${credentialLabel(usableModels(providers, opt.value, forceServer, providerStatus), allowsUnverified(providerStatus, opt.value, forceServer), forceServer)}` : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
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
            <ModelCombobox
              value={peer.ai_model}
              onChange={(val) => updatePeer(peer.id, { ai_model: val })}
              options={strict ? usableModels(providers, peer.ai_provider, forceServer, providerStatus) : peerModels[peer.id] ?? []}
              strict={strict && !allowsUnverified(providerStatus, peer.ai_provider, forceServer)}
              forceServer={strict && forceServer}
              ariaLabel={`Peer ${i + 1} model`}
              placeholder={strict && allowsUnverified(providerStatus, peer.ai_provider, forceServer) ? 'Enter model ID' : 'Model'}
            />
            {strict && allowsUnverified(providerStatus, peer.ai_provider, forceServer) && <p className="text-xs text-text-tertiary">Models are not verified for this key. Suggestions are unverified; enter a model ID at your own risk.</p>}
          </div>
        ))}
      </div>
      <button
        type="button"
        className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
        onClick={() =>
          setPeerConfigs((prev) => [
            ...prev,
            { id: crypto.randomUUID(), ai_provider: strict ? providerOptions[0]?.value ?? '' : 'claude', ai_model: '' },
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
