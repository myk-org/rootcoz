import type { ReactNode } from 'react'

interface CustomMessageBannerProps {
  message: string
  className?: string
  /** Optional action (e.g. a refresh button) placed beside the message. */
  action?: ReactNode
}

export function CustomMessageBanner({ message, className = '', action }: CustomMessageBannerProps) {
  if (!message) return null
  const text = <p className="text-sm font-medium text-signal-orange">{message}</p>
  return (
    <div className={`rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4 ${className}`}>
      {action ? (
        <div className="flex flex-wrap items-center gap-2">
          {text}
          {action}
        </div>
      ) : (
        text
      )}
    </div>
  )
}
