import { Badge } from '@/components/ui/badge'
import type { BadgeLabel } from '@/constants/classifications'

type BadgeVariant = 'default' | 'destructive' | 'success' | 'warning' | 'purple' | 'outline'

const BADGE_STYLES: Record<BadgeLabel, { variant: BadgeVariant; label: string }> = {
  // Root cause axis
  'CODE ISSUE': { variant: 'default', label: 'CODE ISSUE' },
  'PRODUCT BUG': { variant: 'warning', label: 'PRODUCT BUG' },
  'INFRASTRUCTURE': { variant: 'outline', label: 'INFRASTRUCTURE' },
  // Pattern axis
  'NEW': { variant: 'success', label: 'NEW' },
  'REGRESSION': { variant: 'destructive', label: 'REGRESSION' },
  'FLAKY': { variant: 'purple', label: 'FLAKY' },
  'INTERMITTENT': { variant: 'purple', label: 'INTERMITTENT' },
  'KNOWN_BUG': { variant: 'warning', label: 'KNOWN BUG' },
  'PERSISTENT': { variant: 'destructive', label: 'PERSISTENT' },
}

interface ClassificationBadgeProps {
  classification: string
  className?: string
}

export function ClassificationBadge({ classification, className }: ClassificationBadgeProps) {
  const style = Object.hasOwn(BADGE_STYLES, classification)
    ? BADGE_STYLES[classification as BadgeLabel]
    : { variant: 'outline' as const, label: classification }
  return (
    <Badge variant={style.variant} className={className}>
      {style.label}
    </Badge>
  )
}
