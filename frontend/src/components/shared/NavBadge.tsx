import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'

interface NavBadgeProps {
  count: number
  color: 'orange' | 'blue'
  tooltip: string
  pulse?: boolean
}

export function NavBadge({ count, color, tooltip, pulse }: NavBadgeProps) {
  if (count <= 0) return null
  const bgClass = color === 'orange' ? 'bg-signal-orange' : 'bg-signal-blue'
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={`absolute -top-1 -right-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full ${bgClass} px-1 text-[10px] font-bold text-white${pulse ? ' animate-pulse' : ''}`}>
            {count > 99 ? '99+' : count}
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom">{tooltip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
