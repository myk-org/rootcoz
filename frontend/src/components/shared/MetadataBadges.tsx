import type { JobMetadata } from '@/types'
import { Badge } from '@/components/ui/badge'

const TIER_COLORS: Record<string, string> = {
  critical: 'bg-signal-red/15 text-signal-red border-signal-red/30',
  standard: 'bg-signal-blue/15 text-signal-blue border-signal-blue/30',
  low: 'bg-text-tertiary/15 text-text-tertiary border-text-tertiary/30',
}

export function MetadataBadges({ metadata }: { metadata: JobMetadata | null | undefined }) {
  if (!metadata) return null

  const { team, tier, version, labels } = metadata
  const hasBadges = team || tier || version || labels.length > 0
  if (!hasBadges) return null

  return (
    <div className="inline-flex flex-wrap items-center gap-1">
      {team && (
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 font-normal border-accent-blue/30 text-accent-blue">
          {team}
        </Badge>
      )}
      {tier && (
        <Badge variant="outline" className={`text-[10px] px-1.5 py-0 font-normal ${TIER_COLORS[tier] ?? 'border-border-muted text-text-secondary'}`}>
          {tier}
        </Badge>
      )}
      {version && (
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 font-normal border-border-muted text-text-secondary">
          {version}
        </Badge>
      )}
      {labels.map((label) => (
        <Badge key={label} variant="outline" className="text-[10px] px-1.5 py-0 font-normal border-signal-green/30 text-signal-green">
          {label}
        </Badge>
      ))}
    </div>
  )
}
