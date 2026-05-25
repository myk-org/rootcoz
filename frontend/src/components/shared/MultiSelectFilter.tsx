import { useState } from 'react'
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

  const [open, setOpen] = useState(false)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className={cn(
            "flex h-9 w-full items-center justify-between rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm text-text-primary ring-offset-surface-card transition-colors hover:bg-white/10 focus:outline-none focus:ring-2 focus:ring-border-accent focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50 [&>span]:line-clamp-1",
            className,
          )}
        >
          <span>{triggerText}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-30" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="relative z-50 max-h-96 min-w-[8rem] overflow-hidden rounded-xl border border-white/10 bg-surface-card p-1 shadow-lg backdrop-blur-sm data-[state=open]:animate-fade-in"
        align="start"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        {options.map((option) => {
          const isActive = selected.has(option)
          return (
            <button
              key={option}
              type="button"
              onClick={() => onToggle(option)}
              className="relative flex w-full cursor-default select-none items-center rounded-lg py-1.5 pl-3 pr-8 text-sm text-text-primary outline-none hover:bg-white/10"
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
              onClick={() => { onClear(); setOpen(false) }}
              className="flex w-full cursor-default select-none items-center rounded-lg py-1.5 pl-3 pr-8 text-sm text-text-tertiary outline-none hover:bg-white/10"
            >
              Clear all
            </button>
          </>
        )}
      </PopoverContent>
    </Popover>
  )
}
