import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { useAuth } from '@/lib/auth'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'
import { allowsUnverified, usableModels, visibleModels, analysisProviderIds, credentialLabel } from '@/lib/analysisAi'

export function AnalysisProviderSelect({ value, onChange, forceServer, label = 'AI Provider' }: {
  value: string
  onChange: (value: string) => void
  forceServer: boolean
  label?: string
}) {
  const { providers, providerStatus } = useProviderCatalog()
  const { canUseServerProviders } = useAuth()
  const options = buildProviderOptions(analysisProviderIds(providers, providerStatus, forceServer))
  const current = normalizeProvider(value)
  const available = options.some((option) => option.value === current)
  return (
    <Select value={available ? current : undefined} onValueChange={onChange}>
      <SelectTrigger aria-label={label}>
        <SelectValue placeholder={current ? `${current} (unavailable)` : 'Select provider...'} />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => {
          const models = usableModels(providers, option.value, forceServer, providerStatus)
          const available = usableModels(providers, option.value, forceServer, providerStatus, canUseServerProviders).length > 0 || (!forceServer && allowsUnverified(providerStatus, option.value, false))
          return <SelectItem key={option.value} value={option.value} disabled={!available}>{option.label} · {credentialLabel(models, allowsUnverified(providerStatus, option.value, forceServer), forceServer, canUseServerProviders)}{!available && ' · Ask an admin for access'}</SelectItem>
        })}
      </SelectContent>
    </Select>
  )
}

export function AnalysisModelSelect({ provider, value, onChange, forceServer, label = 'AI Model' }: {
  provider: string
  value: string
  onChange: (value: string) => void
  forceServer: boolean
  label?: string
}) {
  const { providers, providerStatus } = useProviderCatalog()
  const { canUseServerProviders } = useAuth()
  const unverified = allowsUnverified(providerStatus, provider, forceServer)
  return <>
    <ModelCombobox value={value} onChange={onChange} options={visibleModels(providers, provider, forceServer, providerStatus, canUseServerProviders)} ariaLabel={label} strict={!unverified} forceServer={forceServer} canUseServer={canUseServerProviders} placeholder={unverified ? 'Enter model ID' : 'Default model'} />
    {unverified && <p className="mt-1 text-xs text-text-tertiary">Models are not verified for this key. Suggestions are unverified; enter a model ID at your own risk.</p>}
  </>
}
