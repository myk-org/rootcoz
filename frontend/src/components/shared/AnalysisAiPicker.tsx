import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { CredentialSourceLabel, ServerAccessHint, ServerAccessTooltip, UnverifiedModelsWarning } from '@/components/shared/CredentialSourceLabel'
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

/** Resolve whether a provider's model list is unverified for the current key. */
export function AnalysisModelNotice({ provider, forceServer }: { provider: string; forceServer: boolean }) {
  const { providerStatus } = useProviderCatalog(forceServer)
  return <UnverifiedModelsWarning show={allowsUnverified(providerStatus, provider, forceServer)} />
}

export function AnalysisModelSelect({ provider, value, onChange, forceServer, label = 'AI Model', hideNotice = false }: {
  provider: string
  value: string
  onChange: (value: string) => void
  forceServer: boolean
  label?: string
  /** Row layouts render the notice themselves, below the whole provider+model row. */
  hideNotice?: boolean
}) {
  const { providers, providerStatus } = useProviderCatalog(forceServer)
  const { canUseServerProviders } = useAuth()
  const unverified = allowsUnverified(providerStatus, provider, forceServer)
  // A single wrapper: the notice must stay under the model picker. Returning a
  // fragment would let the caller lay the notice out as its own flex/grid item,
  // which is what pushed it beside the picker and into the next grid cell.
  return <div className="space-y-1">
    <ModelCombobox value={value} onChange={onChange} options={visibleModels(providers, provider, forceServer, providerStatus, canUseServerProviders)} ariaLabel={label} forceServer={forceServer} canUseServer={canUseServerProviders} placeholder={unverified ? 'Enter model ID' : 'Default model'} />
    {!hideNotice && <UnverifiedModelsWarning show={unverified} />}
  </div>
}
