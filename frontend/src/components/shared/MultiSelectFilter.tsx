import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface MultiSelectFilterProps {
  label: string
  options: string[]
  selected: Set<string>
  onToggle: (value: string) => void
  onClear: () => void
  className?: string
}

export function MultiSelectFilter({ label, options, selected, onToggle, onClear, className }: MultiSelectFilterProps) {
  const triggerText =
    selected.size === 0
      ? label
      : selected.size === 1
        ? [...selected][0]
        : `${selected.size} selected`

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className={cn(
            "flex h-9 w-full items-center justify-between rounded-md border border-border-default bg-surface-elevated px-3 py-2 text-sm text-text-primary shadow-sm ring-offset-surface-card placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-border-accent disabled:cursor-not-allowed disabled:opacity-50 [&>span]:line-clamp-1",
            className,
          )}
        >
          <span>{triggerText}</span>
          <ChevronDown className="h-4 w-4 opacity-50" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="p-1">
        {[...new Set([...options, ...selected])].map((option) => (
          <DropdownMenuCheckboxItem
            key={option}
            checked={selected.has(option)}
            onCheckedChange={() => onToggle(option)}
          >
            {option}
          </DropdownMenuCheckboxItem>
        ))}
        {selected.size > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem
              checked={false}
              onCheckedChange={() => onClear()}
            >
              Clear all
            </DropdownMenuCheckboxItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
