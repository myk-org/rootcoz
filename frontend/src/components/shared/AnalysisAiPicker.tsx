import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'
import { allowsUnverified, usableModels, analysisProviderIds, credentialLabel } from '@/lib/analysisAi'

export function AnalysisProviderSelect({ value, onChange, forceServer, label = 'AI Provider' }: {
  value: string
  onChange: (value: string) => void
  forceServer: boolean
  label?: string
}) {
  const { providers, providerStatus } = useProviderCatalog()
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
          return <SelectItem key={option.value} value={option.value}>{option.label} · {credentialLabel(models, allowsUnverified(providerStatus, option.value, forceServer), forceServer)}</SelectItem>
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
  const unverified = allowsUnverified(providerStatus, provider, forceServer)
  return <>
    <ModelCombobox value={value} onChange={onChange} options={usableModels(providers, provider, forceServer, providerStatus)} ariaLabel={label} strict={!unverified} forceServer={forceServer} placeholder={unverified ? 'Enter model ID' : 'Default model'} />
    {unverified && <p className="mt-1 text-xs text-text-tertiary">Models are not verified for this key. Suggestions are unverified; enter a model ID at your own risk.</p>}
  </>
}
