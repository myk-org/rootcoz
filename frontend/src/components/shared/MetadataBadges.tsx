import type { JobMetadata } from '@/types'
import { Badge } from '@/components/ui/badge'

const TIER_COLORS: Record<string, string> = {
  critical: 'bg-signal-red/15 text-signal-red border-signal-red/30',
  standard: 'bg-signal-blue/15 text-signal-blue border-signal-blue/30',
  low: 'bg-text-tertiary/15 text-text-tertiary border-text-tertiary/30',
}

function normalizeTags(tags: readonly string[] | null | undefined): string[] {
  if (!Array.isArray(tags)) return []
  const unique = new Set<string>()
  for (const tag of tags) {
    if (typeof tag === 'string' && tag.length > 0) unique.add(tag)
  }
  return Array.from(unique)
}

function hasMetadataBadges(metadata: JobMetadata | null | undefined): boolean {
  if (!metadata) return false
  const labels = metadata.labels ?? []
  return Boolean(metadata.team || metadata.tier || metadata.version || labels.length > 0)
}

export function MetadataBadges({ metadata }: { metadata: JobMetadata | null | undefined }) {
  if (!metadata || !hasMetadataBadges(metadata)) return null

  const { team, tier, version } = metadata
  const labels = metadata.labels ?? []

  return (
    <div className="inline-flex flex-wrap items-center gap-1">
      {team && (
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 font-normal border-signal-blue/30 text-signal-blue">
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

export function JobTagBadges({
  tags,
  className = 'inline-flex flex-wrap items-center gap-1',
}: {
  tags: readonly string[] | null | undefined
  className?: string
}) {
  const visibleTags = normalizeTags(tags)
  if (visibleTags.length === 0) return null

  return (
    <div className={className}>
      {visibleTags.map((tag) => (
        <span
          key={tag}
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
            tag === 're-analyze'
              ? 'bg-signal-orange/10 text-signal-orange'
              : 'bg-signal-blue/10 text-signal-blue'
          }`}
        >
          {tag}
        </span>
      ))}
    </div>
  )
}

export function JobMetadataAndTags({
  metadata,
  tags,
  className = 'flex flex-wrap items-center gap-1.5',
  testId,
}: {
  metadata: JobMetadata | null | undefined
  tags: readonly string[] | null | undefined
  className?: string
  testId?: string
}) {
  if (!hasMetadataBadges(metadata) && normalizeTags(tags).length === 0) return null

  return (
    <div className={className} data-testid={testId}>
      <MetadataBadges metadata={metadata} />
      <JobTagBadges tags={tags} />
    </div>
  )
}
