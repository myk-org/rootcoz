import { useEffect, useRef } from 'react'
import type { GroupedFailure } from '@/types'
import { BULK_SELECT_CHECKBOX_CLASS } from '@/lib/constants'
import { useReportDispatch, useReportState } from './ReportContext'

/** Build the selection payloads for every group of a listed section. */
function toSelection(groups: GroupedFailure[], childJobName?: string, childBuildNumber?: number) {
  return groups.map((g) => ({
    id: g.id,
    testNames: g.tests.map((t) => t.test_name),
    childJobName,
    childBuildNumber,
  }))
}

interface GroupSelectAllProps {
  /** Groups currently listed in this section (the selection scope). */
  groups: GroupedFailure[]
  /** Human-readable scope, e.g. "Failures" or the child job name. */
  scopeLabel: string
  childJobName?: string
  childBuildNumber?: number
}

/** Select-all checkbox scoped to the failures listed in one section. */
export function GroupSelectAll({ groups, scopeLabel, childJobName, childBuildNumber }: GroupSelectAllProps) {
  const { selection } = useReportState()
  const dispatch = useReportDispatch()
  const ref = useRef<HTMLInputElement>(null)

  const selected = groups.filter((g) => selection[g.id]).length
  const allSelected = groups.length > 0 && selected === groups.length
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = selected > 0 && !allSelected
  }, [selected, allSelected])

  return (
    <label className="flex items-center gap-1.5 text-xs text-text-tertiary cursor-pointer">
      <input
        ref={ref}
        type="checkbox"
        checked={allSelected}
        onChange={() => dispatch({ type: 'SET_GROUP_SELECTION', payload: { groups: toSelection(groups, childJobName, childBuildNumber), selected: !allSelected } })}
        className={BULK_SELECT_CHECKBOX_CLASS}
        aria-label={`Select all failures in ${scopeLabel}`}
      />
      Select all{groups.length > 0 ? ` (${groups.length})` : ''}
    </label>
  )
}
