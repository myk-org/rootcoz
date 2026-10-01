import { Link } from 'react-router-dom'
import { RotateCw } from 'lucide-react'

interface ReanalysisBannerProps {
  /** Label describing the linked job, e.g. "Re-analysis of <name>". */
  label: string
  jobId: string
}

/**
 * Subtle info banner linking between an analysis and its re-analysis.
 */
function ReanalysisBanner({ label, jobId }: ReanalysisBannerProps) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-accent-blue/20 bg-accent-blue/5 px-3 py-1.5 text-xs text-text-secondary">
      <RotateCw className="h-3 w-3 text-accent-blue shrink-0" />
      <span>
        {label}{' '}
        <Link
          to={`/results/${jobId}`}
          className="font-medium text-text-link hover:underline"
        >
          view
        </Link>
      </span>
    </div>
  )
}

/** Shown on a re-analyzed job — links back to the original analysis. */
export function OriginJobBanner({ originJobId, originJobName }: { originJobId: string; originJobName: string }) {
  return (
    <ReanalysisBanner label={`Re-analysis of ${originJobName}:`} jobId={originJobId} />
  )
}

/** Shown on an original job — links to its latest re-analysis. */
export function ReanalyzedForwardBanner({ reanalysisJobId }: { reanalysisJobId: string }) {
  return (
    <ReanalysisBanner label="This job was re-analyzed →" jobId={reanalysisJobId} />
  )
}
