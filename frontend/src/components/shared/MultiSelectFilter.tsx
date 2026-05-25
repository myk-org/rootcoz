import { cn } from '@/lib/utils'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ChevronDown, Check } from 'lucide-react'

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
    <Popover>
      <PopoverTrigger asChild>
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
      </PopoverTrigger>
      <PopoverContent
        className="relative z-50 max-h-96 min-w-[8rem] overflow-hidden rounded-md border border-border-default bg-surface-card p-1 shadow-lg data-[state=open]:animate-fade-in"
        align="start"
      >
        {options.map((option) => {
          const isActive = selected.has(option)
          return (
            <button
              key={option}
              type="button"
              onClick={() => onToggle(option)}
              className="relative flex w-full cursor-default select-none items-center rounded-sm py-1.5 pl-2 pr-8 text-sm text-text-primary outline-none hover:bg-surface-hover"
            >
              {option}
              {isActive && (
                <span className="absolute right-2 flex h-3.5 w-3.5 items-center justify-center">
                  <Check className="h-4 w-4" />
                </span>
              )}
            </button>
          )
        })}
        {selected.size > 0 && (
          <>
            <hr className="my-1 border-border-muted" />
            <button
              type="button"
              onClick={onClear}
              className="flex w-full cursor-default select-none items-center rounded-sm py-1.5 pl-2 pr-8 text-sm text-text-tertiary outline-none hover:bg-surface-hover"
            >
              Clear all
            </button>
          </>
        )}
      </PopoverContent>
    </Popover>
  )
}
