import { Badge } from '@/components/ui/badge'
import type { Classification } from '@/constants/classifications'

const CLASSIFICATION_STYLES: Record<Classification, { variant: 'default' | 'destructive' | 'success' | 'warning' | 'purple' | 'outline'; label: string }> = {
  'CODE ISSUE': { variant: 'default', label: 'CODE ISSUE' },
  'PRODUCT BUG': { variant: 'warning', label: 'PRODUCT BUG' },
  'FLAKY': { variant: 'purple', label: 'FLAKY' },
  'REGRESSION': { variant: 'destructive', label: 'REGRESSION' },
  'INFRASTRUCTURE': { variant: 'outline', label: 'INFRASTRUCTURE' },
  'KNOWN_BUG': { variant: 'warning', label: 'KNOWN BUG' },
  'INTERMITTENT': { variant: 'purple', label: 'INTERMITTENT' },
}

interface ClassificationBadgeProps {
  classification: string
  className?: string
}

export function ClassificationBadge({ classification, className }: ClassificationBadgeProps) {
  const style = (classification in CLASSIFICATION_STYLES)
    ? CLASSIFICATION_STYLES[classification as Classification]
    : { variant: 'outline' as const, label: classification }
  return (
    <Badge variant={style.variant} className={className}>
      {style.label}
    </Badge>
  )
}
