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
            "flex h-9 w-full items-center justify-between rounded-full border border-border-default bg-surface-elevated px-4 py-2 text-sm text-text-primary ring-offset-surface-card transition-colors hover:bg-surface-hover focus:outline-none focus:ring-2 focus:ring-border-accent focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50 [&>span]:line-clamp-1",
            className,
          )}
        >
          <span>{triggerText}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-30" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="p-1">
        {[...new Set([...options, ...selected])].map((option) => (
          <DropdownMenuCheckboxItem
            key={option}
            checked={selected.has(option)}
            onCheckedChange={() => onToggle(option)}
            onSelect={(e) => e.preventDefault()}
          >
            {option}
          </DropdownMenuCheckboxItem>
        ))}
        {selected.size > 0 && (
          <>
            <DropdownMenuSeparator />
            <button
              type="button"
              onClick={() => onClear()}
              className="flex w-full cursor-default select-none items-center rounded-lg py-1.5 pl-3 pr-3 text-sm text-text-tertiary outline-none hover:bg-surface-hover"
            >
              Clear all
            </button>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
