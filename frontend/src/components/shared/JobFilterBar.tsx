import { AnalysisStateFilterControl } from '@/components/shared/AnalysisStateFilter'
import { DateRangePresetFilter } from '@/components/shared/DateRangePresetFilter'
import { FilterPresetsMenu } from '@/components/shared/FilterPresetsMenu'
import { MetadataClearButton, MetadataDropdowns } from '@/components/shared/MetadataFilterBar'
import { MultiSelectFilter } from '@/components/shared/MultiSelectFilter'
import { ReviewStatusFilter } from '@/components/shared/ReviewStatusFilter'
import { SearchInput } from '@/components/shared/SearchInput'
import { SearchableMultiSelect } from '@/components/ui/searchable-multi-select'
import { TestNameFilter } from '@/components/shared/TestNameFilter'
import type { MetadataOptions } from '@/components/shared/useMetadataOptions'
import type { AnalysisStateFilter } from '@/lib/analysis-state'
import type { FilterPresetPage } from '@/lib/filterPresets'
import type { FilterValues, TestNameMode } from '@/lib/jobFilters'
import type { ReviewStatusFilter as ReviewStatusValue } from '@/lib/review-status'

const STATUS_OPTIONS = ['completed', 'running', 'waiting', 'pending', 'failed', 'timeout', 'aborted'] as const

export interface JobFilterBarProps {
  /** Which preset set this bar saves to. */
  page: FilterPresetPage
  /** The URL the presets menu reads the current filters from. */
  searchParams: URLSearchParams
  onApplyPreset: (filters: FilterValues) => void
  search: { value: string; onChange: (value: string) => void; placeholder: string }
  submitters: {
    values: string[]
    options: string[]
    loading: boolean
    onChange: (values: string[]) => void
  }
  metadata: {
    options: MetadataOptions
    teams: Set<string>
    tiers: Set<string>
    versions: Set<string>
    onTeamToggle: (value: string) => void
    onTierToggle: (value: string) => void
    onVersionToggle: (value: string) => void
    onTeamClear: () => void
    onTierClear: () => void
    onVersionClear: () => void
  }
  statuses: { selected: Set<string>; onToggle: (value: string) => void; onClear: () => void }
  reviewStatus: { value: ReviewStatusValue; onChange: (value: ReviewStatusValue) => void }
  analysisState: { value: AnalysisStateFilter; onChange: (value: AnalysisStateFilter) => void }
  dateRange: { from: string; to: string; onChange: (from: string, to: string) => void }
  /** Clearing every filter the bar holds; the button says nothing when there are none. */
  clearAll: { hasFilters: boolean; onClear: () => void }
  /** Dashboard only: filtering *jobs* by the name of a failing test (#546 B). */
  testName?: { name: string; mode: TestNameMode; onChange: (name: string, mode: TestNameMode) => void }
}

/**
 * The one filter row both job pages show (#546 A).
 *
 * Every control here already existed; this is the composition and the order, in
 * one place — a new dimension is added once, and both pages get it. The pages
 * own only where their state lives (URL params vs. a reducer) through the
 * callbacks below.
 */
export function JobFilterBar({
  page,
  searchParams,
  onApplyPreset,
  search,
  submitters,
  metadata,
  statuses,
  reviewStatus,
  analysisState,
  dateRange,
  clearAll,
  testName,
}: JobFilterBarProps) {
  return (
    <div data-testid={`${page}-filters`} className="flex flex-wrap gap-3 items-center">
      <FilterPresetsMenu page={page} searchParams={searchParams} onApply={onApplyPreset} />
      <SearchInput
        value={search.value}
        onChange={search.onChange}
        placeholder={search.placeholder}
        className="w-full sm:w-64"
      />
      {testName && <TestNameFilter name={testName.name} mode={testName.mode} onChange={testName.onChange} />}
      <SearchableMultiSelect
        label="Submitted by"
        values={submitters.values}
        options={submitters.options.map((value) => ({ value }))}
        loading={submitters.loading}
        // Picked options are exact names; a typed term is a partial one and the
        // server matches substrings, so both work from one control.
        allowCustom
        onChange={submitters.onChange}
        className="w-full sm:w-48"
      />
      <MetadataDropdowns {...metadata} />
      <MultiSelectFilter
        label="All statuses"
        options={[...STATUS_OPTIONS]}
        selected={statuses.selected}
        onToggle={statuses.onToggle}
        onClear={statuses.onClear}
        className="w-full sm:w-40"
      />
      <ReviewStatusFilter value={reviewStatus.value} onChange={reviewStatus.onChange} />
      <AnalysisStateFilterControl value={analysisState.value} onChange={analysisState.onChange} />
      <DateRangePresetFilter from={dateRange.from} to={dateRange.to} onChange={dateRange.onChange} />
      <MetadataClearButton hasFilters={clearAll.hasFilters} onClearAll={clearAll.onClear} />
    </div>
  )
}
