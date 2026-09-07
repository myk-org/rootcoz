import { User } from 'lucide-react'

interface JobAttributionProps {
  submittedBy?: string
  analyzedBy?: string
  className?: string
  iconClassName?: string
}

export function JobAttribution({
  submittedBy,
  analyzedBy,
  className = 'inline-flex items-center gap-1 text-xs text-text-tertiary',
  iconClassName = 'h-3 w-3',
}: JobAttributionProps) {
  if (!submittedBy && !analyzedBy) return null
  const showAnalyzer = !!analyzedBy && analyzedBy !== submittedBy
  return (
    <span className={className}>
      {submittedBy && (
        <span className="inline-flex items-center gap-1">
          <User className={iconClassName} />
          {submittedBy}
        </span>
      )}
      {showAnalyzer && (
        <span className={submittedBy ? 'ml-1' : undefined}>
          {submittedBy ? '· analyzed ' : 'analyzed '}
          {analyzedBy}
        </span>
      )}
    </span>
  )
}
