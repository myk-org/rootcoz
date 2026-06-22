import { useState } from 'react'
import { api } from '@/lib/api'
import { useReportDispatch } from './ReportContext'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PATTERNS } from '@/constants/classifications'

interface PatternSelectProps {
  jobId: string
  testName: string
  testNames?: string[]
  currentPattern: string
  childJobName?: string
  childBuildNumber?: number
}

export function PatternSelect({
  jobId,
  testName,
  testNames,
  currentPattern,
  childJobName,
  childBuildNumber,
}: PatternSelectProps) {
  const dispatch = useReportDispatch()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleChange(value: string) {
    if (value === currentPattern) return
    setError(null)
    setLoading(true)
    try {
      // Single API call for the representative test — the backend propagates
      // to all tests in the same error signature group automatically.
      await api.put(`/results/${jobId}/override-pattern`, {
        test_name: testName,
        pattern: value,
        child_job_name: childJobName ?? '',
        child_build_number: childBuildNumber ?? 0,
      })
      dispatch({
        type: 'OVERRIDE_PATTERN',
        payload: { testName, testNames, pattern: value, childJobName, childBuildNumber },
      })
    } catch (err) {
      console.error('Failed to save pattern:', err)
      setError('Failed to save')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center gap-1">
      <Select value={currentPattern} onValueChange={handleChange} disabled={loading}>
        <SelectTrigger aria-label="Override pattern" className="h-8 w-36 text-xs">
          <SelectValue placeholder="Pattern..." />
        </SelectTrigger>
        <SelectContent>
          {((PATTERNS as readonly string[]).includes(currentPattern)
            ? [...PATTERNS]
            : currentPattern
              ? [currentPattern, ...PATTERNS]
              : [...PATTERNS]
          ).map((p) => (
            <SelectItem key={p} value={p}>
              {p === 'KNOWN_BUG' ? 'KNOWN BUG' : p}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {error && (
        <span aria-live="polite" className="text-signal-red text-xs">
          {error}
        </span>
      )}
    </div>
  )
}
