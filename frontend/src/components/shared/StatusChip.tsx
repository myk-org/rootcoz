import { Badge } from '@/components/ui/badge'

const STATUS_MAP: Record<string, { variant: 'default' | 'destructive' | 'success' | 'warning' | 'outline'; label: string }> = {
  completed: { variant: 'success', label: 'Completed' },
  running: { variant: 'default', label: 'Running' },
  waiting: { variant: 'warning', label: 'Waiting' },
  pending: { variant: 'outline', label: 'Pending' },
  failed: { variant: 'destructive', label: 'Failed' },
  timeout: { variant: 'warning', label: 'Analysis Timed Out' },
}

interface StatusChipProps {
  status: string
  className?: string
}

export function StatusChip({ status, className }: StatusChipProps) {
  const style = STATUS_MAP[status] ?? { variant: 'outline' as const, label: status }
  return (
    <Badge variant={style.variant} className={className}>
      {style.label}
    </Badge>
  )
}
