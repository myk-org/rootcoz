import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

export const SERVER_ACCESS_HELP = 'Organization access is unavailable. Ask an admin for access.'

export function ServerAccessHint({ id }: { id: string }) {
  return <span id={id} className="sr-only">{SERVER_ACCESS_HELP}</span>
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
