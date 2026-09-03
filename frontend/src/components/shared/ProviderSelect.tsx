import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useProviderOptions } from '@/lib/useProviderOptions'
import { normalizeProvider } from '@/lib/aiProviders'

export type { AiProviderOption } from '@/lib/aiProviders'
export { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'

interface ProviderSelectProps {
  value: string
  onChange: (value: string) => void
  className?: string
  compact?: boolean
}

export function ProviderSelect({ value, onChange, className, compact }: ProviderSelectProps) {
  const normalized = normalizeProvider(value)
  const options = useProviderOptions(normalized)

  return (
    <Select value={normalized || undefined} onValueChange={onChange}>
      <SelectTrigger className={cn(compact && 'w-[120px] h-8 text-xs', className)}>
        <SelectValue placeholder="Provider" />
      </SelectTrigger>
      <SelectContent>
        {options.map((opt) => (
          <SelectItem key={opt.value} value={opt.value}>
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
