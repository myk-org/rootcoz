import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold font-display transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-signal-blue/15 text-signal-blue",
        destructive: "border-transparent bg-signal-red/15 text-signal-red",
        success: "border-transparent bg-signal-green/15 text-signal-green",
        warning: "border-transparent bg-signal-orange/15 text-signal-orange",
        purple: "border-transparent bg-signal-purple/15 text-signal-purple",
        outline: "border-border-default text-text-secondary",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
