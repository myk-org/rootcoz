import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useProviderCatalog, useProviderOptions } from '@/lib/useProviderOptions'
import { normalizeProvider } from '@/lib/aiProviders'
import { allowsUnverified, credentialLabel, usableModels } from '@/lib/analysisAi'

export type { AiProviderOption } from '@/lib/aiProviders'
export { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'

interface ProviderSelectProps {
  value: string
  onChange: (value: string) => void
  className?: string
  compact?: boolean
  showCredentialSources?: boolean
}

export function ProviderSelect({ value, onChange, className, compact, showCredentialSources = false }: ProviderSelectProps) {
  const normalized = normalizeProvider(value)
  const options = useProviderOptions(normalized)
  const { providers, providerStatus } = useProviderCatalog()

  return (
    <Select value={normalized || undefined} onValueChange={onChange}>
      <SelectTrigger className={cn(compact && 'w-[120px] h-8 text-xs', className)}>
        <SelectValue placeholder="Provider" />
      </SelectTrigger>
      <SelectContent>
        {options.map((opt) => {
          const models = showCredentialSources ? [
            ...usableModels(providers, opt.value, false, providerStatus).filter((model) => model.verified !== false),
            ...usableModels(providers, opt.value, true, providerStatus).map((model) => ({ ...model, credential_sources: ['server' as const] })),
          ] : []
          const manualUser = showCredentialSources && allowsUnverified(providerStatus, opt.value, false)
          return <SelectItem key={opt.value} value={opt.value}>
            {opt.label}{(models.length || manualUser) ? ` · ${credentialLabel(models, manualUser)}` : ''}
          </SelectItem>
        })}
      </SelectContent>
    </Select>
  )
}
