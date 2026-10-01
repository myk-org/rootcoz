import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ReportProvider, useReportDispatch } from '../ReportContext'
import { FailureCard } from '../FailureCard'
import { GroupSelectAll } from '../GroupSelectAll'
import { BulkUpdateBar } from '../BulkUpdateBar'
import { groupFailures } from '@/lib/grouping'
import type { FailureAnalysis } from '@/types'

const { put, get } = vi.hoisted(() => ({ put: vi.fn(), get: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { get, put, post: vi.fn(), delete: vi.fn() }, extractApiDetail: () => null }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'reviewer', isOperator: false, isAdmin: false, username: 'rev' }) }))

function failure(testName: string, signature: string): FailureAnalysis {
  return {
    id: `id-${testName}`,
    test_name: testName,
    error: 'boom',
    error_signature: signature,
    analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: [], details: '', artifacts_evidence: '' },
  }
}

// Two single-test failures plus one group of two tests sharing a signature.
const FAILURES = [failure('test-a', 'sig-a'), failure('test-b', 'sig-b'), failure('test-c1', 'sig-c'), failure('test-c2', 'sig-c')]

/** The report page always has a result loaded; the bulk bar reads the job id from it. */
function SeedResult() {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_RESULT',
      payload: {
        result: {
          job_id: 'job-1', job_name: 'test-job', build_number: 1, jenkins_url: null,
          status: 'completed', summary: '', ai_provider: 'p', ai_model: 'm',
          failures: FAILURES, child_job_analyses: [],
        },
        createdAt: '', completedAt: '', analysisStartedAt: '',
      },
    })
  }, [dispatch])
  return null
}

function Harness() {
  const groups = groupFailures(FAILURES)
  return (
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ReportProvider>
          <SeedResult />
          <GroupSelectAll groups={groups} scopeLabel="Failures" />
          {groups.map((g, i) => (
            <FailureCard key={g.id} group={g} jobId="job-1" index={i} />
          ))}
          <BulkUpdateBar />
        </ReportProvider>
      </TooltipProvider>
    </MemoryRouter>
  )
}

function renderHarness() {
  render(<Harness />)
  return userEvent.setup()
}

async function confirmBulkAction(user: ReturnType<typeof userEvent.setup>, action: string, confirmLabel = action) {
  await user.click(screen.getByRole('button', { name: action }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: confirmLabel }))
}

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

beforeEach(() => {
  sessionStorage.clear()
  put.mockReset()
  get.mockReset()
  put.mockResolvedValue({ reviewed_by: 'rev' })
  get.mockResolvedValue({ tracked_in: {} })
})

describe('bulk failure selection', () => {
  it('bulk-marks individual selections as reviewed and clears the selection', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select test-b' }))

    // A selected group covers every test that shares its error signature.
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    // The confirmation states the scope of the bulk action.
    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Apply to 4 tests in 3 failures?')
    await user.click(within(dialog).getByRole('button', { name: 'Mark reviewed' }))

    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))
    for (const name of ['test-a', 'test-b', 'test-c1', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: name, reviewed: true, child_job_name: '', child_build_number: 0,
      })
    }
    // Every selected failure now shows the reviewed toggle state.
    await waitFor(() => expect(screen.queryByText(/selected in/)).not.toBeInTheDocument())
  })

  it('selects every listed failure with select-all and un-marks them when all are reviewed', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark reviewed' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))

    // Re-select: all are reviewed, so the bar offers the reverse action.
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await confirmBulkAction(user, 'Unmark reviewed')
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/results/job-1/reviewed', {
        test_name: 'test-c2', reviewed: false, child_job_name: '', child_build_number: 0,
      }),
    )
  })

  it('applies classification and pattern across the selection', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-a', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })

    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await user.click(screen.getByRole('combobox', { name: 'Bulk pattern' }))
    await user.click(await screen.findByRole('option', { name: 'FLAKY' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(8))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-pattern', {
      test_name: 'test-b', pattern: 'FLAKY', child_job_name: '', child_build_number: 0,
    })
  })

  it('links the whole selection to one issue and refreshes tracked-in links', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://github.com/org/repo/issues/7')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))

    const confirmDialog = await screen.findByRole('dialog')
    expect(confirmDialog).toHaveTextContent('https://github.com/org/repo/issues/7')
    expect(confirmDialog).toHaveTextContent('1 test in 1 failure')
    await user.click(within(confirmDialog).getByRole('button', { name: 'Link' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/results/job-1/tracked-in', {
        test_name: 'test-a', url: 'https://github.com/org/repo/issues/7', type: 'github', child_job_name: '', child_build_number: 0,
      }),
    )
    expect(get).toHaveBeenCalledWith('/results/job-1/tracked-in')
  })

  it('reports partial failures without losing the rest of the update', async () => {
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (body.test_name === 'test-b') throw new Error('boom')
      return { reviewed_by: 'rev', path }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await confirmBulkAction(user, 'Mark reviewed')

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to update 1 of 4: test-b'))
  })

  it('keeps the existing signature-scoped "Review all" behaviour', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('button', { name: /test-c1/ }))
    await user.click(screen.getByRole('button', { name: 'Review All (0/2)' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
      test_name: 'test-c2', reviewed: true, child_job_name: '', child_build_number: 0,
    })
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
  })
})
