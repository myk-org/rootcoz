import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'

export function ApiKeyReveal({
  apiKey,
  pending,
  pendingCustomMessage,
  onAcknowledge,
}: {
  apiKey: string
  pending: boolean
  pendingCustomMessage: string
  onAcknowledge: () => void
}) {
  const [copied, setCopied] = useState(false)

  return (
    <>
      <h2 className="mb-4 font-display text-base font-semibold text-text-primary">Registration Complete</h2>
      <div className="space-y-4">
        <div className="rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4">
          <p className="text-sm font-medium text-signal-orange">⚠️ Save this API key — you won't see it again!</p>
          <p className="mt-1 text-xs text-text-tertiary">You'll need this key to log in. If you lose it, ask an admin to generate a new one.</p>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-surface-elevated p-3">
          <code className="flex-1 font-mono text-sm text-text-primary break-all select-all">{apiKey}</code>
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(apiKey)
                setCopied(true)
                setTimeout(() => setCopied(false), 2000)
              } catch { /* clipboard not available */ }
            }}
          >
            {copied ? 'Copied!' : 'Copy'}
          </Button>
        </div>
        <Button onClick={onAcknowledge} className="w-full">
          I've saved my key — Continue
        </Button>
      </div>
      {pending && (
        <div className="mt-4 rounded-md border border-signal-amber/30 bg-signal-amber/10 p-3">
          <p className="text-xs font-medium text-signal-amber">
            Your account is pending admin approval. You can save your API key now, but you won't be able to log in until an admin approves your account.
          </p>
          <CustomMessageBanner message={pendingCustomMessage} className="mt-2" />
        </div>
      )}
    </>
  )
}
