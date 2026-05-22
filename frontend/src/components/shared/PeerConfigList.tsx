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
}

export function PeerConfigList({
  peerConfigs,
  setPeerConfigs,
  peerModels,
  maxRounds,
  setMaxRounds,
}: PeerConfigListProps) {
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
                value={peer.ai_provider}
                onValueChange={(v) =>
                  setPeerConfigs((prev) =>
                    prev.map((p) => (p.id === peer.id ? { ...p, ai_provider: v } : p))
                  )
                }
              >
                <SelectTrigger className="w-[120px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="claude">Claude</SelectItem>
                  <SelectItem value="gemini">Gemini</SelectItem>
                  <SelectItem value="cursor">Cursor</SelectItem>
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
              onChange={(val) =>
                setPeerConfigs((prev) =>
                  prev.map((p) =>
                    p.id === peer.id ? { ...p, ai_model: val } : p
                  )
                )
              }
              options={peerModels[peer.id] ?? []}
              placeholder="Model"
            />
          </div>
        ))}
      </div>
      <button
        type="button"
        className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
        onClick={() =>
          setPeerConfigs((prev) => [
            ...prev,
            { id: crypto.randomUUID(), ai_provider: 'claude', ai_model: '' },
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
