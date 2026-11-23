import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { FacetDropdowns } from '../FacetDropdowns'
import { applyJobFiltersToParams, EMPTY_JOB_FILTERS, jobFiltersFromParams, JOB_FILTER_KEYS } from '@/lib/jobFilters'
import { applyFacetOptionChange, selectedOptionLabelsForFacet } from '@/lib/tagFacets'
import type { FilterFacet } from '@/types'

const SAMPLE_FACETS: FilterFacet[] = [
  {
    id: 'platform',
    name: 'Platform',
    options: [
      { label: 'AWS', tags: ['aws'], count: 12, missing_tags: [] },
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

describe('FacetDropdowns (#658)', () => {
  it('renders facets with per-option counts and hides fully missing options unless selected', () => {
    const onLabelsChange = vi.fn()
    render(
      <FacetDropdowns
        facets={SAMPLE_FACETS}
        selectedLabels={[]}
        onLabelsChange={onLabelsChange}
      />,
    )

    const platformTrigger = screen.getByRole('combobox', { name: 'Add Platform' })
    fireEvent.click(platformTrigger)

    expect(screen.getByRole('option', { name: 'AWS (12)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Bare metal (5)' })).toBeInTheDocument()
    // 0-count live tag stays visible so admin/user sees it matches nothing
    expect(screen.getByRole('option', { name: 'vSphere (0)' })).toBeInTheDocument()
    // Disappeared tag option is hidden when unselected
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

    // Platform shows AWS selected; open it and also select Bare metal
    fireEvent.click(screen.getByRole('combobox', { name: 'Add Platform' }))
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Bare metal (5)' }))

    expect(onLabelsChange).toHaveBeenCalledWith(['aws', 'io2-csi', 'scheduled', 'bare-metal'])

    // Removing AWS chip removes only aws, keeping io2-csi and scheduled intact
    onLabelsChange.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Remove AWS' }))
    expect(onLabelsChange).toHaveBeenCalledWith(['io2-csi', 'scheduled'])
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

  it('round-trips facet selections through flat label= URL params, presets, and Clear filters', () => {
    const [platform, storage] = SAMPLE_FACETS
    let labels = applyFacetOptionChange(platform, [], ['AWS', 'Bare metal'])
    labels = applyFacetOptionChange(storage, labels, ['io2'])
    expect(labels).toEqual(['aws', 'bare-metal', 'io2-csi'])

    const params = applyJobFiltersToParams(
      new URLSearchParams(),
      { ...EMPTY_JOB_FILTERS, label: labels },
      JOB_FILTER_KEYS,
    )
    expect(params.getAll('label')).toEqual(['aws', 'bare-metal', 'io2-csi'])
    expect([...new Set(params.keys())]).toEqual(['label'])

    // Round-trip from URL restores selections on both facets
    const restored = jobFiltersFromParams(params, JOB_FILTER_KEYS)
    expect(selectedOptionLabelsForFacet(platform, restored.label)).toEqual(['AWS', 'Bare metal'])
    expect(selectedOptionLabelsForFacet(storage, restored.label)).toEqual(['io2'])

    // Clear filters wipes label= and clears all facet dropdowns
    const cleared = applyJobFiltersToParams(params, EMPTY_JOB_FILTERS, JOB_FILTER_KEYS)
    expect(cleared.getAll('label')).toEqual([])
    expect(selectedOptionLabelsForFacet(platform, cleared.getAll('label'))).toEqual([])
  })
})
