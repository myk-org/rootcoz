import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { CredentialSourceLabel, ServerAccessHint, ServerAccessTooltip } from '@/components/shared/CredentialSourceLabel'
import { useId } from 'react'
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
  const { providers, providerStatus } = useProviderCatalog(forceServer)
  const { canUseServerProviders } = useAuth()
  const hintId = useId()
  const options = buildProviderOptions(analysisProviderIds(providers, providerStatus, forceServer))
  const current = normalizeProvider(value)
  const available = options.some((option) => option.value === current)
  const selectedLabel = credentialLabel(usableModels(providers, current, forceServer, providerStatus), allowsUnverified(providerStatus, current, forceServer), forceServer)
  const hasDeniedServer = !canUseServerProviders && options.some((option) => credentialLabel(usableModels(providers, option.value, forceServer, providerStatus)).includes('Server'))
  return (
    <Select value={available ? current : undefined} onValueChange={onChange}>
      {hasDeniedServer && <ServerAccessHint id={hintId} />}
      <ServerAccessTooltip show={available && !canUseServerProviders && selectedLabel.includes('Server')}>
        <SelectTrigger aria-label={label} aria-describedby={hasDeniedServer ? hintId : undefined}>
          <SelectValue placeholder={current ? `${current} (unavailable)` : 'Select provider...'} />
        </SelectTrigger>
      </ServerAccessTooltip>
      <SelectContent>
        {options.map((option) => {
          const models = usableModels(providers, option.value, forceServer, providerStatus)
          const available = usableModels(providers, option.value, forceServer, providerStatus, canUseServerProviders).length > 0 || (!forceServer && allowsUnverified(providerStatus, option.value, false))
          const source = credentialLabel(models, allowsUnverified(providerStatus, option.value, forceServer), forceServer)
          return <ServerAccessTooltip key={option.value} show={!canUseServerProviders && source.includes('Server')} disabled={!available}>
            <SelectItem value={option.value} disabled={!available}>{option.label} · <CredentialSourceLabel label={source} canUseServer={canUseServerProviders} /></SelectItem>
          </ServerAccessTooltip>
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
  const { providers, providerStatus } = useProviderCatalog(forceServer)
  const { canUseServerProviders } = useAuth()
  const unverified = allowsUnverified(providerStatus, provider, forceServer)
  return <>
    <ModelCombobox value={value} onChange={onChange} options={visibleModels(providers, provider, forceServer, providerStatus, canUseServerProviders)} ariaLabel={label} forceServer={forceServer} canUseServer={canUseServerProviders} placeholder={unverified ? 'Enter model ID' : 'Default model'} />
    {unverified && <p className="mt-1 text-xs text-text-tertiary">Models are not verified for this key. Suggestions are unverified; enter a model ID at your own risk.</p>}
  </>
}
