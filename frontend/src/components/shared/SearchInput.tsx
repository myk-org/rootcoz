import { Search } from 'lucide-react'
import { InlineClearButton } from '@/components/ui/inline-clear-button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

interface SearchInputProps {
  value: string
  onChange: (value: string) => void
  /** Optional immediate clear handler (defaults to `onChange('')`). */
  onClear?: () => void
  clearLabel?: string
  placeholder?: string
  className?: string
}

export function SearchInput({
  value,
  onChange,
  onClear,
  clearLabel = 'Clear search',
  placeholder = 'Search...',
  className,
}: SearchInputProps) {
  return (
    <div className={cn('relative', className)}>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cn('pl-9', value && 'pr-8')}
      />
      {value && (
        <InlineClearButton
          label={clearLabel}
          onClick={() => (onClear ? onClear() : onChange(''))}
          className="absolute right-2 top-1/2 -translate-y-1/2"
        />
      )}
    </div>
  )
}
