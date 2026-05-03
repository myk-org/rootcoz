import { Input } from '@/components/ui/input'
import { Calendar, X } from 'lucide-react'

interface DateRangeFilterProps {
  from: string
  to: string
  onFromChange: (value: string) => void
  onToChange: (value: string) => void
  onClear?: () => void
}

export function DateRangeFilter({ from, to, onFromChange, onToChange, onClear }: DateRangeFilterProps) {
  return (
    <div className="flex items-center gap-1.5">
      <Calendar className="h-3.5 w-3.5 text-text-tertiary" />
      <Input
        type="date"
        value={from}
        max={to || undefined}
        onChange={(e) => onFromChange(e.target.value)}
        className="h-9 w-auto px-2 text-xs"
        aria-label="Filter from date"
      />
      <span className="text-xs text-text-tertiary">–</span>
      <Input
        type="date"
        value={to}
        min={from || undefined}
        onChange={(e) => onToChange(e.target.value)}
        className="h-9 w-auto px-2 text-xs"
        aria-label="Filter to date"
      />
      {(from || to) && (
        <button
          type="button"
          onClick={() => { if (onClear) { onClear() } else { onFromChange(''); onToChange('') } }}
          className="text-xs text-text-tertiary hover:text-text-secondary"
          aria-label="Clear date filter"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )
}
