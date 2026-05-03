import { TableHead } from '@/components/ui/table'
import { cn } from '@/lib/utils'

export type SortDirection = 'asc' | 'desc'

interface SortableHeaderProps {
  label: string
  sortKey: string
  currentSort: string
  currentDirection: SortDirection
  onSort: (key: string) => void
  className?: string
}

export function SortableHeader({
  label,
  sortKey,
  currentSort,
  currentDirection,
  onSort,
  className,
}: SortableHeaderProps) {
  const active = currentSort === sortKey
  return (
    <TableHead
      className={cn('cursor-pointer select-none transition-colors hover:text-text-primary', className)}
      onClick={() => onSort(sortKey)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <span className={cn('text-[10px]', active ? 'text-text-primary' : 'text-text-tertiary')}>
          {active ? (currentDirection === 'asc' ? '\u25B2' : '\u25BC') : '\u21C5'}
        </span>
      </span>
    </TableHead>
  )
}
