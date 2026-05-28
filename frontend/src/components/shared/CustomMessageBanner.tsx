interface CustomMessageBannerProps {
  message: string
  className?: string
}

export function CustomMessageBanner({ message, className = '' }: CustomMessageBannerProps) {
  if (!message) return null
  return (
    <div className={`rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4 ${className}`}>
      <p className="text-sm font-medium text-signal-orange">{message}</p>
    </div>
  )
}
