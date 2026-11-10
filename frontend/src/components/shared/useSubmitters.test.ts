import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useSubmitters } from '@/components/shared/useSubmitters'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

describe('useSubmitters', () => {
  afterEach(() => {
    apiGet.mockReset()
  })

  it('returns the names the endpoint sends, exactly as sent', async () => {
    // The server folded with SQLite's ASCII-only lower(); folding again here in
    // full-Unicode JS would drop a name it kept, and the picker could then never
    // offer the dropped spelling.
    apiGet.mockResolvedValue({ submitters: ['Änne', 'änne', 'alice'] })

    const { result } = renderHook(() => useSubmitters())

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(apiGet).toHaveBeenCalledWith('/api/dashboard/submitters')
    expect(result.current.submitters).toEqual(['Änne', 'änne', 'alice'])
  })

  it('settles on an empty list for an empty or failed response', async () => {
    apiGet.mockResolvedValueOnce({ submitters: [] })
    const empty = renderHook(() => useSubmitters())
    await waitFor(() => expect(empty.result.current.loading).toBe(false))
    expect(empty.result.current.submitters).toEqual([])

    apiGet.mockRejectedValueOnce(new Error('API error 500: boom'))
    const failed = renderHook(() => useSubmitters())
    // No spinner loop: a failure ends the load with nothing to offer.
    await waitFor(() => expect(failed.result.current.loading).toBe(false))
    expect(failed.result.current.submitters).toEqual([])
  })
})
