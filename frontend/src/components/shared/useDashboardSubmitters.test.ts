import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useDashboardSubmitters } from '@/components/shared/useDashboardSubmitters'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

describe('useDashboardSubmitters', () => {
  afterEach(() => {
    apiGet.mockReset()
  })

  it('returns the names the endpoint sends, once each', async () => {
    apiGet.mockResolvedValue({ submitters: ['Yuval', 'yuval', 'alice'] })

    const { result } = renderHook(() => useDashboardSubmitters())

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(apiGet).toHaveBeenCalledWith('/api/dashboard/submitters')
    expect(result.current.submitters).toEqual(['Yuval', 'alice'])
  })

  it('settles on an empty list for an empty or failed response', async () => {
    apiGet.mockResolvedValueOnce({ submitters: [] })
    const empty = renderHook(() => useDashboardSubmitters())
    await waitFor(() => expect(empty.result.current.loading).toBe(false))
    expect(empty.result.current.submitters).toEqual([])

    apiGet.mockRejectedValueOnce(new Error('API error 500: boom'))
    const failed = renderHook(() => useDashboardSubmitters())
    // No spinner loop: a failure ends the load with nothing to offer.
    await waitFor(() => expect(failed.result.current.loading).toBe(false))
    expect(failed.result.current.submitters).toEqual([])
  })
})
