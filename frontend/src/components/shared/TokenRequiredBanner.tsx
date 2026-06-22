import { AlertTriangle } from 'lucide-react'

interface TokenRequiredBannerProps {
  provider: 'GitHub' | 'Jira'
  action?: string
}

export function TokenRequiredBanner({ provider, action = 'create issues' }: TokenRequiredBannerProps) {
  return (
    <div className="rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4 flex items-start gap-3">
      <AlertTriangle className="h-5 w-5 text-signal-orange shrink-0 mt-0.5" />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-text-primary">
          {provider} token required
        </p>
        <p className="text-xs text-text-secondary">
          Set up your {provider} token in Profile Settings to {action}.
        </p>
        <a href="/settings" target="_blank" rel="noopener noreferrer"
           className="text-xs text-text-link hover:underline mt-1 inline-flex items-center gap-1">
          Open Profile Settings →
        </a>
      </div>
    </div>
  )
}
