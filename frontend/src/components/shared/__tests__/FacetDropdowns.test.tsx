import { useState } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FacetDropdowns } from '../FacetDropdowns'
import { JobFilterBar } from '../JobFilterBar'
import {
  applyFiltersToParams,
  applyJobFiltersToParams,
  EMPTY_JOB_FILTERS,
  hasActiveJobFilters,
  jobFiltersFromParams,
  JOB_FILTER_KEYS,
} from '@/lib/jobFilters'
import { FILTER_PRESET_KEYS, resetLastAppliedPresetForTests, type FilterPreset } from '@/lib/filterPresets'
import { applyFacetOptionChange, selectedOptionLabelsForFacet } from '@/lib/tagFacets'
import type { FilterFacet } from '@/types'

const mockAuth = { isOperator: true }
vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    put: (...args: unknown[]) => apiPut(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
  extractApiDetail: (err: unknown) =>
    err instanceof Error ? err.message : 'request failed',
}))

const SAMPLE_FACETS: FilterFacet[] = [
  {
    id: 'platform',
    name: 'Platform',
    options: [
      { label: 'AWS', tags: ['aws'], count: 12, missing_tags: [] },
      { label: 'Cloud', tags: ['aws', 'gcp'], count: 14, missing_tags: [] },
      { label: 'Bare metal', tags: ['bare-metal'], count: 5, missing_tags: [] },
      { label: 'vSphere', tags: ['vsphere'], count: 0, missing_tags: [] },
      { label: 'Legacy', tags: ['old-platform'], count: 0, missing_tags: ['old-platform'] },
    ],
  },
  {
    id: 'storage',
    name: 'Storage class',
    options: [
      { label: 'OCS RBD', tags: ['ocs-rbd'], count: 8, missing_tags: [] },
      { label: 'io2', tags: ['io2-csi'], count: 3, missing_tags: [] },
    ],
  },
]

function JobFilterBarHarness() {
  const [searchParams, setSearchParams] = useState(() => new URLSearchParams())
  const values = jobFiltersFromParams(searchParams, FILTER_PRESET_KEYS.dashboard)
  const hasFilters = hasActiveJobFilters(values)

  return (
    <div>
      <div data-testid="url-state">{searchParams.toString()}</div>
      <JobFilterBar
        page="dashboard"
        searchParams={searchParams}
        onApplyPreset={(filters) =>
          setSearchParams((prev) => applyFiltersToParams(prev, filters, FILTER_PRESET_KEYS.dashboard))
        }
        search={{ value: values.search, onChange: () => {}, placeholder: 'Filter jobs...' }}
        submitters={{ values: [], options: [], loading: false, onChange: () => {} }}
        metadata={{
          options: { teams: [], tiers: [], versions: [], allLabels: ['aws', 'bare-metal', 'io2-csi'] },
          teams: new Set(),
          tiers: new Set(),
          versions: new Set(),
          onTeamToggle: () => {},
          onTierToggle: () => {},
          onVersionToggle: () => {},
          onTeamClear: () => {},
          onTierClear: () => {},
          onVersionClear: () => {},
        }}
        statuses={{ selected: new Set(), onToggle: () => {}, onClear: () => {} }}
        reviewStatus={{ value: 'all', onChange: () => {} }}
        analysisState={{ value: 'all', onChange: () => {} }}
        dateRange={{ from: '', to: '', onChange: () => {} }}
        facets={{
          items: SAMPLE_FACETS,
          selectedLabels: values.label,
          onLabelsChange: (nextLabels) =>
            setSearchParams((prev) =>
              applyJobFiltersToParams(prev, { ...values, label: nextLabels }, FILTER_PRESET_KEYS.dashboard),
            ),
        }}
        clearAll={{
          hasFilters,
          onClear: () =>
            setSearchParams((prev) =>
              applyJobFiltersToParams(prev, EMPTY_JOB_FILTERS, FILTER_PRESET_KEYS.dashboard),
            ),
        }}
      />
    </div>
  )
}

describe('FacetDropdowns (#658)', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiPut.mockReset()
    apiDelete.mockReset()
    resetLastAppliedPresetForTests()
  })

  it('renders facets with per-option counts and hides fully missing options unless selected', () => {
    const onLabelsChange = vi.fn()
    render(
      <FacetDropdowns
        facets={SAMPLE_FACETS}
        selectedLabels={[]}
        onLabelsChange={onLabelsChange}
      />,
    )

    fireEvent.click(screen.getByRole('combobox', { name: 'Add Platform' }))

    expect(screen.getByRole('option', { name: 'AWS (12)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Bare metal (5)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'vSphere (0)' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Legacy/ })).toBeNull()
  })

  it('writes flat label= arrays for OR within a facet and preserves other facets / plain tags', () => {
    const onLabelsChange = vi.fn()
    render(
      <FacetDropdowns
        facets={SAMPLE_FACETS}
        selectedLabels={['aws', 'io2-csi', 'scheduled']}
        onLabelsChange={onLabelsChange}
      />,
    )

    // Single-tag AWS=[aws] selected does NOT spuriously select multi-tag Cloud=[aws,gcp]
    expect(selectedOptionLabelsForFacet(SAMPLE_FACETS[0], ['aws'])).toEqual(['AWS'])

    fireEvent.click(screen.getByRole('combobox', { name: 'Add Platform' }))
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Bare metal (5)' }))

    expect(onLabelsChange).toHaveBeenCalledWith(['aws', 'io2-csi', 'scheduled', 'bare-metal'])

    onLabelsChange.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Remove AWS' }))
    expect(onLabelsChange).toHaveBeenCalledWith(['io2-csi', 'scheduled'])
  })

  it('preserves shared tags still needed by another selected facet when clearing one facet', () => {
    const overlappingFacets: FilterFacet[] = [
      {
        id: 'primary',
        name: 'Primary',
        options: [{ label: 'AWS', tags: ['aws'], count: 5, missing_tags: [] }],
      },
      {
        id: 'cloud',
        name: 'Cloud',
        options: [{ label: 'AWS Mirror', tags: ['aws'], count: 5, missing_tags: [] }],
      },
    ]
    const next = applyFacetOptionChange(overlappingFacets[0], ['aws'], [], overlappingFacets)
    expect(next).toEqual(['aws'])
  })

  it('still shows a disappeared option if it is already selected in the URL so the user can clear it', () => {
    const onLabelsChange = vi.fn()
    render(
      <FacetDropdowns
        facets={SAMPLE_FACETS}
        selectedLabels={['old-platform']}
        onLabelsChange={onLabelsChange}
      />,
    )

    fireEvent.click(screen.getByRole('combobox', { name: 'Add Platform' }))
    expect(screen.getByRole('option', { name: 'Legacy (0)' })).toBeInTheDocument()
  })

  it('saves and restores facet selections via FilterPresetsMenu and resets via Clear filters', async () => {
    const user = userEvent.setup()
    const presetsStore: FilterPreset[] = []
    apiGet.mockImplementation(() => Promise.resolve([...presetsStore]))
    apiPost.mockImplementation((_url: string, body: { name: string; page: 'dashboard'; scope: 'user'; filters: Record<string, string | string[]> }) => {
      const created: FilterPreset = {
        id: presetsStore.length + 1,
        name: body.name,
        page: body.page,
        scope: body.scope,
        filters: body.filters,
        created_at: '2026-01-01T00:00:00',
        updated_at: '2026-01-01T00:00:00',
      }
      presetsStore.push(created)
      return Promise.resolve(created)
    })

    render(<JobFilterBarHarness />)

    // Select AWS in Platform and io2 in Storage class
    fireEvent.click(screen.getByRole('combobox', { name: 'Add Platform' }))
    fireEvent.mouseDown(screen.getByRole('option', { name: 'AWS (12)' }))
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Search Platform' }), { key: 'Escape' })
    fireEvent.click(screen.getByRole('combobox', { name: 'Add Storage class' }))
    fireEvent.mouseDown(screen.getByRole('option', { name: 'io2 (3)' }))
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Search Storage class' }), { key: 'Escape' })

    expect(screen.getByTestId('url-state')).toHaveTextContent('label=aws&label=io2-csi')

    // Save current facet selection as a preset through FilterPresetsMenu
    await user.click(screen.getByTestId('filter-presets-trigger'))
    await user.click(await screen.findByText(/Save current filters as/i))
    const nameInput = await screen.findByPlaceholderText(/Preset name/i)
    await user.type(nameInput, 'AWS io2')
    await user.click(screen.getByTestId('filter-preset-save-confirm'))

    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1))
    expect(apiPost.mock.calls[0][1].filters).toEqual({ label: ['aws', 'io2-csi'] })

    // Click the real Clear filters button
    await user.click(screen.getByRole('button', { name: /Clear filters/i }))
    expect(screen.getByTestId('url-state')).toHaveTextContent('')
    expect(screen.queryByRole('button', { name: 'Remove AWS' })).toBeNull()

    // Reapply the saved preset through FilterPresetsMenu
    await user.click(screen.getByTestId('filter-presets-trigger'))
    await user.click(await screen.findByText('AWS io2'))

    await waitFor(() => {
      expect(screen.getByTestId('url-state')).toHaveTextContent('label=aws&label=io2-csi')
    })
    expect(screen.getByRole('button', { name: 'Remove AWS' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove io2' })).toBeInTheDocument()
    // Ensure JOB_FILTER_KEYS stays unchanged (no new URL key introduced)
    expect(JOB_FILTER_KEYS).toContain('label')
  })
})
