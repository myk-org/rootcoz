import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

export const SERVER_ACCESS_HELP = 'Organization access is unavailable. Ask an admin for access.'

/** Shown when the sidecar refused to list a provider for a user's own key.
 *
 *  That happens for multi-API builtins such as OpenRouter (pi-sidecar#892): the
 *  listing is refused outright, so the only IDs offered come from the bundled
 *  `pi_model_suggestions.json` snapshot, which is not refreshed at runtime. The
 *  wording has to say so — a plain "unverified" hint hides that the list is also
 *  possibly out of date, which is what makes a usable model look nonexistent. */
export const UNVERIFIED_MODELS_HELP = 'This provider could not be listed for your key, so these are bundled suggestions from an older pi-ai catalog. Newer models are missing — enter a model ID at your own risk.'

export function ServerAccessHint({ id }: { id: string }) {
  return <span id={id} className="sr-only">{SERVER_ACCESS_HELP}</span>
}

/** Notice for a provider whose model list could not be verified against a key.
 *  Lives below the model picker in a shared wrapper — never inline beside it. */
export function UnverifiedModelsWarning({ show }: { show: boolean }) {
  if (!show) return null
  return <p className="text-xs text-text-tertiary">{UNVERIFIED_MODELS_HELP}</p>
}

export function CredentialSourceLabel({ label, canUseServer }: { label: string; canUseServer: boolean }) {
  if (canUseServer || !label.includes('Server')) return label
  const [prefix] = label.split('Server')
  return <>{prefix}<span className="text-text-tertiary">Server</span></>
}

// Disabled options cannot receive keyboard focus; the picker trigger carries their help.
export function ServerAccessTooltip({ show, disabled = false, children }: { show: boolean; disabled?: boolean; children: React.ReactElement }) {
  if (!show) return children
  return <TooltipProvider delayDuration={200}><Tooltip>
    <TooltipTrigger asChild>{disabled ? <div>{children}</div> : children}</TooltipTrigger>
    <TooltipContent>{SERVER_ACCESS_HELP}</TooltipContent>
  </Tooltip></TooltipProvider>
}
