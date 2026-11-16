import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ReportProvider } from '../ReportContext'
import { useReportDispatch } from '../reportState'
import { ReviewToggle } from '../ReviewToggle'
import { useEffect } from 'react'

const auth = { role: 'reviewer', isOperator: false, isAdmin: false, username: 'tester' }
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }))
vi.mock('@/lib/cookies', () => ({ getUsername: () => 'tester' }))

const mockPut = vi.fn()
vi.mock('@/lib/api', () => ({
  api: { put: (...args: unknown[]) => mockPut(...args) },
}))

const REVIEWED_AT = '2026-01-02T03:04:05.000006Z'

/** The rendered timestamp, derived the same way the component derives it. */
function formatted(ts: string) {
  return new Date(ts).toLocaleString()
}

function Harness({ seed }: { seed?: { reviewed: boolean; username: string; reviewed_at?: string | null } }) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    if (seed) {
      dispatch({ type: 'SET_REVIEW', payload: { key: 'test-a', state: { ...seed, updated_at: 'x' } } })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <ReviewToggle jobId="job-1" testName="test-a" />
}

function show(seed?: Parameters<typeof Harness>[0]['seed']) {
  render(
    <TooltipProvider delayDuration={0}>
      <ReportProvider>
        <Harness seed={seed} />
      </ReportProvider>
    </TooltipProvider>,
  )
}

describe('ReviewToggle attribution', () => {
  beforeEach(() => {
    sessionStorage.clear()
    mockPut.mockReset()
  })

  it('shows who reviewed it and when it was first reviewed', async () => {
    show({ reviewed: true, username: 'alice', reviewed_at: REVIEWED_AT })
    expect(screen.getByText('Reviewed')).toBeTruthy()
    expect(screen.getByText(`by alice on ${formatted(REVIEWED_AT)}`)).toBeTruthy()

    await userEvent.hover(screen.getByRole('button', { name: /Reviewed/i }))
    expect(
      (await screen.findAllByText(`Reviewed by alice on ${formatted(REVIEWED_AT)} — click to unmark`)).length,
    ).toBeGreaterThan(0)
  })

  it('omits the timestamp for a review recorded before #566', () => {
    show({ reviewed: true, username: 'alice', reviewed_at: null })
    expect(screen.getByText('by alice')).toBeTruthy()
    expect(screen.queryByText(/on \u2014/)).toBeNull()
    expect(screen.queryByText(/ on /)).toBeNull()
  })

  it('uses the timestamp the API returned when marking', async () => {
    mockPut.mockResolvedValue({ status: 'ok', reviewed_by: 'alice', reviewed_at: REVIEWED_AT })
    show()
    await userEvent.click(screen.getByRole('button', { name: /^Review$/i }))

    await waitFor(() => expect(screen.getByText(`by alice on ${formatted(REVIEWED_AT)}`)).toBeTruthy())
    expect(mockPut).toHaveBeenCalledWith('/results/job-1/reviewed', {
      test_name: 'test-a',
      reviewed: true,
      child_job_name: '',
      child_build_number: 0,
    })
  })

  it('unmarks back to Review and re-marking keeps the original timestamp', async () => {
    show({ reviewed: true, username: 'alice', reviewed_at: REVIEWED_AT })

    mockPut.mockResolvedValue({ status: 'ok', reviewed_by: '', reviewed_at: null })
    await userEvent.click(screen.getByRole('button', { name: /Reviewed/i }))
    await waitFor(() => expect(screen.getByText('Review')).toBeTruthy())

    // The server never rewrites first_reviewed_at, so the re-mark returns it.
    mockPut.mockResolvedValue({ status: 'ok', reviewed_by: 'bob', reviewed_at: REVIEWED_AT })
    await userEvent.click(screen.getByRole('button', { name: /^Review$/i }))
    await waitFor(() => expect(screen.getByText(`by bob on ${formatted(REVIEWED_AT)}`)).toBeTruthy())
  })
})
