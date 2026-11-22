import * as React from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface InlineClearButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'type' | 'children'> {
  /** Accessible label describing what this button clears (e.g. "Clear search"). */
  label: string
}

/**
 * Shared inline clear (×) control used inside text inputs and clearable
 * single-value filter pickers so every clear affordance shares one shape.
 */
export function InlineClearButton({ label, className, ...props }: InlineClearButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      className={cn(
        'inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent',
        className,
      )}
      {...props}
    >
      <X className="h-3.5 w-3.5" aria-hidden="true" />
    </button>
  )
}
