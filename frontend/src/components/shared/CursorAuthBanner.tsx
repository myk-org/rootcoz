import { AlertTriangle } from 'lucide-react'
import type { ProviderStatus } from '@/types'

interface CursorAuthBannerProps {
  status: ProviderStatus
  className?: string
}

/** Admin-facing notice when Cursor models are unavailable / browser login expired. */
export function CursorAuthBanner({ status, className }: CursorAuthBannerProps) {
  const hint =
    status.hint ||
    (status.has_api_key
      ? 'CURSOR_API_KEY is set (it does not expire) but Cursor is unavailable. Check sidecar env/restart/network.'
      : 'Cursor browser login (`agent login`) expired. Set CURSOR_API_KEY on the server — that key does not expire and always works when set.')

  return (
    <div
      className={`rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-3 flex items-start gap-3 ${className ?? ''}`}
      role="status"
    >
      <AlertTriangle className="h-4 w-4 text-signal-orange shrink-0 mt-0.5" />
      <div className="flex flex-col gap-1 min-w-0">
        <p className="text-sm font-medium text-text-primary">
          {status.has_api_key
            ? 'Cursor unavailable (API key is set)'
            : 'Cursor browser login expired'}
          {status.reason ? ` (${status.reason})` : ''}
        </p>
        <p className="text-xs text-text-secondary break-words">{hint}</p>
        {!status.has_api_key && (
          <p className="text-xs text-text-tertiary">
            <code className="text-[11px]">CURSOR_API_KEY</code> does not expire.
            Prefer it over <code className="text-[11px]">agent login</code> on Dev/prod.
          </p>
        )}
      </div>
    </div>
  )
}
