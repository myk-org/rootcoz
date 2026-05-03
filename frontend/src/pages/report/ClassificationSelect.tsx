import { useState } from 'react'
import { api } from '@/lib/api'
import { useReportDispatch } from './ReportContext'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { OVERRIDE_CLASSIFICATIONS } from '@/constants/classifications'

interface ClassificationSelectProps {
  jobId: string
  testName: string
  testNames?: string[]
  currentClassification: string
  childJobName?: string
  childBuildNumber?: number
}

export function ClassificationSelect({
  jobId,
  testName,
  testNames,
  currentClassification,
  childJobName,
  childBuildNumber,
}: ClassificationSelectProps) {
  const dispatch = useReportDispatch()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleChange(value: string) {
    if (value === currentClassification) return
    setError(null)
    setLoading(true)
    try {
      const namesForApi = testNames && testNames.length > 0 ? testNames : [testName]
      const results = await Promise.allSettled(
        namesForApi.map((name) =>
          api.put(`/results/${jobId}/override-classification`, {
            test_name: name,
            classification: value,
            child_job_name: childJobName ?? '',
            child_build_number: childBuildNumber ?? 0,
          }).then(() => name),
        ),
      )
      const persisted = results.filter((r): r is PromiseSettledResult<string> & { status: 'fulfilled' } => r.status === 'fulfilled').map((r) => r.value)
      const failedNames = results
        .map((r, i) => (r.status === 'rejected' ? namesForApi[i] : null))
        .filter((n): n is string => n !== null)
      if (persisted.length > 0) {
        dispatch({
          type: 'OVERRIDE_CLASSIFICATION',
          payload: { testName, testNames: persisted, classification: value, childJobName, childBuildNumber },
        })
      }
      if (failedNames.length > 0) {
        setError(`Failed to save ${failedNames.length} of ${results.length}: ${failedNames.join(', ')}`)
      }
    } catch (err) {
      console.error('Failed to save classification:', err)
      setError('Failed to save')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center gap-1">
      <Select value={currentClassification} onValueChange={handleChange} disabled={loading}>
        <SelectTrigger aria-label="Override classification" className="h-8 w-40 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {((OVERRIDE_CLASSIFICATIONS as readonly string[]).includes(currentClassification)
            ? [...OVERRIDE_CLASSIFICATIONS]
            : currentClassification
              ? [currentClassification, ...OVERRIDE_CLASSIFICATIONS]
              : [...OVERRIDE_CLASSIFICATIONS]
          ).map((c) => (
            <SelectItem key={c} value={c}>
              {c}
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
