import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserTokenPresence } from '@/lib/useUserTokens'

const apiMock = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: apiMock }))

describe('useUserTokenPresence', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
  })

  it('reports the row as pending while a reopened dialog re-reads it', async () => {
    apiMock.get
      .mockResolvedValueOnce({ github_token: 'ghp_old' })
      .mockReturnValueOnce(new Promise(() => {})) // the reopen's read never settles
    const { result, rerender } = renderHook(
      ({ open }: { open: boolean }) => useUserTokenPresence(open),
      { initialProps: { open: true } },
    )
    await waitFor(() =>
      expect(result.current).toEqual({ github: true, jira: false, loaded: true }),
    )

    rerender({ open: false })
    rerender({ open: true })

    // The previous answer belongs to the last opening: until this read lands the
    // row is unknown, not "no token" and not the stale one.
    expect(result.current).toEqual({ github: false, jira: false, loaded: false })
  })
})
