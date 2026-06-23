import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select'
import {
  REVIEW_STATUS_OPTIONS,
  REVIEW_STATUS_LABELS,
  type ReviewStatusFilter as ReviewStatusValue,
} from '@/lib/review-status'

interface ReviewStatusFilterProps {
  value: ReviewStatusValue
  onChange: (value: ReviewStatusValue) => void
}

export function ReviewStatusFilter({ value, onChange }: ReviewStatusFilterProps) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as ReviewStatusValue)}>
      <SelectTrigger
        aria-label="Review status filter"
        className={`w-full sm:w-auto ${value !== 'all' ? 'border-signal-blue' : ''}`}
      >
        <span className="text-sm">Review: {REVIEW_STATUS_LABELS[value]}</span>
      </SelectTrigger>
      <SelectContent>
        {REVIEW_STATUS_OPTIONS.map((opt) => (
          <SelectItem key={opt} value={opt}>{REVIEW_STATUS_LABELS[opt]}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
