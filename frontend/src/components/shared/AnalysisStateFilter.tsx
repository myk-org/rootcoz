import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select'
import {
  ANALYSIS_STATE_OPTIONS,
  ANALYSIS_STATE_LABELS,
  type AnalysisStateFilter,
} from '@/lib/analysis-state'

interface AnalysisStateFilterProps {
  value: AnalysisStateFilter
  onChange: (value: AnalysisStateFilter) => void
}

export function AnalysisStateFilterControl({ value, onChange }: AnalysisStateFilterProps) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as AnalysisStateFilter)}>
      <SelectTrigger
        aria-label="Mode filter"
        className={`w-full sm:w-auto ${value !== 'all' ? 'border-signal-blue' : ''}`}
      >
        <span className="text-sm">Mode: {ANALYSIS_STATE_LABELS[value]}</span>
      </SelectTrigger>
      <SelectContent>
        {ANALYSIS_STATE_OPTIONS.map((opt) => (
          <SelectItem key={opt} value={opt}>{ANALYSIS_STATE_LABELS[opt]}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
