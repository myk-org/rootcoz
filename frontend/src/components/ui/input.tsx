import * as React from "react"
import { cn } from "@/lib/utils"
import { CONTROL_CLASS } from "@/lib/controlStyles"

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          CONTROL_CLASS,
          "flex w-full px-4 py-1 text-sm text-text-primary transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
        ref={ref}
        {...props}
      />
    )
  },
)
Input.displayName = "Input"

export { Input }
