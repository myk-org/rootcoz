import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

interface ProviderSelectProps {
  value: string
  onChange: (value: string) => void
  className?: string
  compact?: boolean
}

export function ProviderSelect({ value, onChange, className, compact }: ProviderSelectProps) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className={cn(compact && 'w-[120px] h-8 text-xs', className)}>
        <SelectValue placeholder="Provider" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="claude">Claude</SelectItem>
        <SelectItem value="gemini">Gemini</SelectItem>
        <SelectItem value="cursor">Cursor</SelectItem>
      </SelectContent>
    </Select>
  )
}
