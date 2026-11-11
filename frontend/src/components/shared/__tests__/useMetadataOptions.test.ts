import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useMetadataOptions } from '@/components/shared/useMetadataOptions'
import type { JobMetadata } from '@/types'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

const row = (over: Partial<JobMetadata>): JobMetadata => ({
  job_name: 'job',
  team: null,
  tier: null,
  version: null,
  labels: [],
  ...over,
})

describe('useMetadataOptions', () => {
  afterEach(() => {
    apiGet.mockReset()
  })

  it('folds one metadata fetch into deduped, sorted options', async () => {
    apiGet.mockResolvedValue([
      row({ team: 'storage', tier: '10', version: 'v2', labels: ['b', 'a'] }),
      row({ team: 'compute', tier: '9', version: 'v1', labels: ['a'] }),
      row({ team: 'storage', tier: null, version: null }),
    ])

    const { result } = renderHook(() => useMetadataOptions())

    await waitFor(() => expect(result.current.options.teams).toEqual(['compute', 'storage']))
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(apiGet).toHaveBeenCalledWith('/api/jobs/metadata')
    // Tiers sort numerically, so "9" precedes "10" — the filter bar's order.
    expect(result.current.options.tiers).toEqual(['9', '10'])
    expect(result.current.options.versions).toEqual(['v1', 'v2'])
    expect(result.current.options.allLabels).toEqual(['a', 'b'])
    expect(result.current.loadError).toBe(false)
  })

  it('reports a failed fetch without pretending it has options', async () => {
    apiGet.mockRejectedValue(new Error('API error 500: boom'))

    const { result } = renderHook(() => useMetadataOptions())

    await waitFor(() => expect(result.current.loadError).toBe(true))
    expect(result.current.options).toEqual({
      teams: [],
      tiers: [],
      versions: [],
      allLabels: [],
    })
  })
})
