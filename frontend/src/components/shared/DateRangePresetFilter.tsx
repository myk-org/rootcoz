import { useState, useMemo } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Calendar, Check } from 'lucide-react'

/** Preset definition — label + days-back (0 = all time). */
interface Preset {
  label: string
  days: number
}

const PRESETS: Preset[] = [
  { label: 'All time', days: 0 },
  { label: 'Last 7 days', days: 7 },
  { label: 'Last 14 days', days: 14 },
  { label: 'Last 30 days', days: 30 },
  { label: 'Last year', days: 365 },
]

function toDateString(d: Date): string {
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function todayString(): string {
  return toDateString(new Date())
}

function daysAgoString(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return toDateString(d)
}

/** Format custom date range for button label: "May 1 – May 28" */
function formatDateLabel(from: string, to: string): string {
  const fmt = (s: string) => {
    const d = new Date(s + 'T00:00:00')
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  }
  if (from && to) return `${fmt(from)} – ${fmt(to)}`
  if (from) return `From ${fmt(from)}`
  if (to) return `Until ${fmt(to)}`
  return 'All time'
}

export interface DateRangePresetFilterProps {
  from: string
  to: string
  onChange: (from: string, to: string) => void
}

export function DateRangePresetFilter({ from, to, onChange }: DateRangePresetFilterProps) {
  const [open, setOpen] = useState(false)
  const [showCustom, setShowCustom] = useState(false)

  /** Determine active preset (if any). */
  const activePreset = useMemo(() => {
    if (!from && !to) return PRESETS[0] // All time
    const today = todayString()
    if (to && to !== today) return null // Custom end date
    for (const p of PRESETS) {
      if (p.days === 0) continue
      if (from === daysAgoString(p.days)) return p
    }
    return null
  }, [from, to])

  const buttonLabel = activePreset
    ? activePreset.label
    : formatDateLabel(from, to)

  function applyPreset(preset: Preset) {
    if (preset.days === 0) {
      onChange('', '')
    } else {
      onChange(daysAgoString(preset.days), todayString())
    }
    setShowCustom(false)
    setOpen(false)
  }

  function handleCustomClick() {
    setShowCustom(true)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-9 gap-1.5 text-xs font-medium"
          aria-label="Date range filter"
        >
          <Calendar className="h-3.5 w-3.5 text-text-tertiary" />
          {buttonLabel}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-56 p-1">
        <div className="flex flex-col">
          {PRESETS.map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => applyPreset(preset)}
              className="flex items-center justify-between rounded-sm px-3 py-1.5 text-sm text-text-primary hover:bg-surface-hover transition-colors"
            >
              {preset.label}
              {activePreset?.label === preset.label && !showCustom && (
                <Check className="h-3.5 w-3.5 text-signal-blue" />
              )}
            </button>
          ))}

          <div className="my-1 h-px bg-border-default" />

          <button
            type="button"
            onClick={handleCustomClick}
            className="flex items-center justify-between rounded-sm px-3 py-1.5 text-sm text-text-primary hover:bg-surface-hover transition-colors"
          >
            Custom range…
            {!activePreset && !showCustom && (from || to) && (
              <Check className="h-3.5 w-3.5 text-signal-blue" />
            )}
          </button>

          {showCustom && (
            <div className="mt-2 flex flex-col gap-2 px-3 pb-2">
              <label className="text-xs text-text-tertiary">
                From
                <Input
                  type="date"
                  value={from}
                  max={to || undefined}
                  onChange={(e) => onChange(e.target.value, to)}
                  className="mt-1 h-8 text-xs"
                  aria-label="Custom from date"
                />
              </label>
              <label className="text-xs text-text-tertiary">
                To
                <Input
                  type="date"
                  value={to}
                  min={from || undefined}
                  onChange={(e) => onChange(from, e.target.value)}
                  className="mt-1 h-8 text-xs"
                  aria-label="Custom to date"
                />
              </label>
            </div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
