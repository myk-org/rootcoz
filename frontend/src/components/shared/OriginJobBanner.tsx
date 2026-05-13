import { Link } from 'react-router-dom'
import { RotateCw } from 'lucide-react'

interface OriginJobBannerProps {
  originJobId: string
  originJobName: string
}

/**
 * Subtle info banner shown when viewing a re-analyzed job.
 * Links back to the original analysis.
 */
export function OriginJobBanner({ originJobId, originJobName }: OriginJobBannerProps) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-accent-blue/20 bg-accent-blue/5 px-3 py-1.5 text-xs text-text-secondary">
      <RotateCw className="h-3 w-3 text-accent-blue shrink-0" />
      <span>
        Re-analysis of{' '}
        <Link
          to={`/results/${originJobId}`}
          className="font-medium text-text-link hover:underline"
        >
          {originJobName}
        </Link>
      </span>
    </div>
  )
}
