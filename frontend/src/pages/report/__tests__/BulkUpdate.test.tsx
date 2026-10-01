import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ReportProvider, useReportDispatch } from '../ReportContext'
import { FailureCard } from '../FailureCard'
import { GroupSelectAll } from '../GroupSelectAll'
import { BulkUpdateBar } from '../BulkUpdateBar'
import { groupFailures } from '@/lib/grouping'
import type { FailureAnalysis } from '@/types'

const { put, get, role } = vi.hoisted(() => ({ put: vi.fn(), get: vi.fn(), role: { current: 'reviewer' } }))
vi.mock('@/lib/api', () => ({ api: { get, put, post: vi.fn(), delete: vi.fn() }, extractApiDetail: () => null }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: role.current, isOperator: false, isAdmin: false, username: 'rev' }) }))

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
function resultPayload(failures: FailureAnalysis[] = FAILURES) {
  return {
    result: {
      job_id: 'job-1', job_name: 'test-job', build_number: 1, jenkins_url: null,
      status: 'completed' as const, summary: '', ai_provider: 'p', ai_model: 'm',
      failures, child_job_analyses: [],
    },
    createdAt: '', completedAt: '', analysisStartedAt: '',
  }
}

function SeedResult() {
  const dispatch = useReportDispatch()
  useEffect(() => { dispatch({ type: 'SET_RESULT', payload: resultPayload() }) }, [dispatch])
  return null
}

/** Stands in for the SSE status-changed refresh that re-dispatches SET_RESULT. */
function SseRefresh({ label = 'Simulate background refresh', failures }: { label?: string; failures?: FailureAnalysis[] }) {
  const dispatch = useReportDispatch()
  return <button onClick={() => dispatch({ type: 'SET_RESULT', payload: resultPayload(failures) })}>{label}</button>
}

function Harness({ seedGroupId, removedFailures }: { seedGroupId?: string; removedFailures?: FailureAnalysis[] }) {
  const groups = groupFailures(FAILURES)
  return (
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ReportProvider>
          <SeedResult />
          <SseRefresh />
          {removedFailures && (
            <SseRefresh label="Simulate refresh without removed failures" failures={removedFailures} />
          )}
          <GroupSelectAll groups={groups} scopeLabel="Failures" />
          {groups.map((g, i) => (
            <FailureCard key={g.id} group={g} jobId="job-1" index={i} />
          ))}
          {seedGroupId && <SeedSelection groupId={seedGroupId} groups={groups} />}
          <BulkUpdateBar />
        </ReportProvider>
      </TooltipProvider>
    </MemoryRouter>
  )
}

/** Pre-select a group without clicking — used to prove the bulk bar hides from viewers. */
function SeedSelection({ groupId, groups }: { groupId: string; groups: ReturnType<typeof groupFailures> }) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    const g = groups.find((x) => x.id === groupId)
    if (g) dispatch({ type: 'TOGGLE_GROUP_SELECTION', payload: { id: g.id, testNames: g.tests.map((t) => t.test_name), childJobName: '', childBuildNumber: 0 } })
  }, [dispatch, groupId, groups])
  return null
}

function renderHarness(props: { seedGroupId?: string; removedFailures?: FailureAnalysis[] } = {}) {
  render(<Harness {...props} />)
  return userEvent.setup()
}

async function confirmBulkAction(user: ReturnType<typeof userEvent.setup>, action: string, confirmLabel = action) {
  await user.click(screen.getByRole('button', { name: action }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: confirmLabel }))
}

/** The confirm dialog is modal, so the page behind it is aria-hidden while a run
 *  is in flight — query it with `hidden` to drive a selection change underneath. */
function behindDialog(name: string, role: 'checkbox' | 'button') {
  return screen.getByRole(role, { name, hidden: true })
}

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

beforeEach(() => {
  sessionStorage.clear()
  role.current = 'reviewer'
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

  it('applies classification and pattern once per signature group, not once per test', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    // Three groups, one request each — the backend propagates to the whole
    // signature group, so test-c2 must not get its own request.
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-a', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-c1', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
    expect(put).not.toHaveBeenCalledWith('/results/job-1/override-classification', expect.objectContaining({ test_name: 'test-c2' }))

    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await user.click(screen.getByRole('combobox', { name: 'Bulk pattern' }))
    await user.click(await screen.findByRole('option', { name: 'FLAKY' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(6))
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

  it('reports partial failures and retries only the tests that failed', async () => {
    let failTestB = true
    put.mockImplementation(async (_path: string, body: { test_name: string }) => {
      if (body.test_name === 'test-b' && failTestB) throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await confirmBulkAction(user, 'Mark reviewed')

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to update 1 of 4: test-b'))
    // Only the failed test stays selected — the three that succeeded are gone.
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()

    failTestB = false
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(5))
    expect(put).toHaveBeenLastCalledWith('/results/job-1/reviewed', {
      test_name: 'test-b', reviewed: true, child_job_name: '', child_build_number: 0,
    })
    await waitFor(() => expect(screen.queryByText(/selected in/)).not.toBeInTheDocument())
  })

  it('keeps a selection made while the bulk run is in flight', async () => {
    let release: () => void = () => {}
    put.mockImplementation(() => new Promise((resolve) => { release = () => resolve({ reviewed_by: 'rev' }) }))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    await confirmBulkAction(user, 'Mark reviewed')

    fireEvent.click(behindDialog('Select test-b', 'checkbox'))
    release()
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // test-a is done, but the selection made during the run survives.
    await waitFor(() => expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument())
  })

  it('keeps the selection through a background refresh and drops a stale dialog', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    // A status-changed SSE refresh re-dispatches SET_RESULT.
    await user.click(screen.getByRole('button', { name: 'Simulate background refresh' }))
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()

    // An open dialog must not survive a cleared selection and act on stale scopes.
    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.click(behindDialog('Clear', 'button'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })

  it('rejects an unusable issue URL before the bulk confirm', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://')
    expect(await within(urlDialog).findByText('Please enter a valid URL (e.g., https://github.com/org/repo/issues/123)')).toBeInTheDocument()
    expect(within(urlDialog).getByRole('button', { name: 'Continue' })).toBeDisabled()
    // Only the URL prompt is open — no bulk confirmation.
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(put).not.toHaveBeenCalled()
  })

  it('reports saved links as applied when only the refresh fails', async () => {
    get.mockRejectedValue(new Error('boom'))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://jira.example.com/browse/PROJ-1')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Link' }))

    await waitFor(() => expect(put).toHaveBeenCalledWith('/results/job-1/tracked-in', expect.objectContaining({ test_name: 'test-a' })))
    // The write is not reported as a bulk failure — only the refresh is, and it
    // outlives the bar that the successful run removed.
    expect(await screen.findByRole('alert')).toHaveTextContent('Links saved, but refreshing the tracked links failed.')
    expect(screen.queryByRole('alert')).not.toHaveTextContent('Failed to update')
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
  })

  it('reports a failed link write even when the tracked-in refresh also fails', async () => {
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (path.endsWith('/tracked-in') && body.test_name === 'test-b') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    get.mockRejectedValue(new Error('boom'))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://jira.example.com/browse/PROJ-1')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Link' }))

    // Both problems are reported: the link that was never written and the refresh.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Failed to update 1 of 4: test-b')
    expect(alert).toHaveTextContent('Refreshing the tracked links also failed.')
    // Only the link that was not written stays selected for a retry.
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()
  })

  it('prunes a removed failure from the selection on a background refresh', async () => {
    // The refreshed report dropped test-b entirely and split the sig-c group.
    const user = renderHarness({ removedFailures: [FAILURES[0], FAILURES[3]] })
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Simulate refresh without removed failures' }))
    expect(screen.getByText(/2 tests selected in 2 failures/)).toBeInTheDocument()

    // The retained names are the live ones — a mutation must not send a dead name.
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    for (const name of ['test-a', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: name, reviewed: true, child_job_name: '', child_build_number: 0,
      })
    }
    expect(put).not.toHaveBeenCalledWith('/results/job-1/reviewed', expect.objectContaining({ test_name: 'test-b' }))
  })

  it('applies an override to the whole signature group after the selection was narrowed', async () => {
    // Narrow the sig-c group down to a single test with a failed review run.
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (path.endsWith('/reviewed') && body.test_name === 'test-c2') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument())

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'INFRASTRUCTURE' }))
    const dialog = await screen.findByRole('dialog')
    // The widened scope is stated, not silently applied.
    expect(dialog).toHaveTextContent('every test sharing the error signature — 2 tests in 1 failure')
    await user.click(within(dialog).getByRole('button', { name: 'Apply' }))

    // One request for the group (the backend propagates it to both tests), but
    // the optimistic patch now covers the unselected sibling.
    await waitFor(() => expect(put).toHaveBeenLastCalledWith('/results/job-1/override-classification', {
      test_name: 'test-c1', classification: 'INFRASTRUCTURE', child_job_name: '', child_build_number: 0,
    }))
    const card = within(document.getElementById('group-id-test-c1') as HTMLElement)
    expect(card.getByText('INFRASTRUCTURE')).toBeInTheDocument()
    expect(card.queryByText('CODE ISSUE')).toBeNull()
  })

  it('clears the previous bulk error when the selection is cleared', async () => {
    put.mockImplementation(async (_path: string, body: { test_name: string }) => {
      if (body.test_name === 'test-b') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-b' }))
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to update 1 of 1: test-b'))

    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('hides selection checkboxes and the bulk bar from viewers', async () => {
    role.current = 'viewer'
    const groups = groupFailures(FAILURES)
    renderHarness({ seedGroupId: groups[0].id })

    expect(screen.queryByRole('checkbox', { name: 'Select all failures in Failures' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Select test-a' })).not.toBeInTheDocument()
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark reviewed' })).not.toBeInTheDocument()
  })

  it('makes every review control read-only for viewers', async () => {
    role.current = 'viewer'
    const user = renderHarness()

    // The state stays readable, but nothing may start a review edit (403).
    for (const name of ['Review', 'Review 0/2']) {
      expect(screen.getAllByRole('button', { name }).length).toBeGreaterThan(0)
      for (const button of screen.getAllByRole('button', { name })) expect(button).toBeDisabled()
    }
    await user.click(screen.getByRole('button', { name: /test-c1/ }))
    expect(screen.getByRole('button', { name: 'Review All (0/2)' })).toBeDisabled()
    for (const button of screen.getAllByRole('button', { name: 'Review' })) expect(button).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Review 0/2' }))
    expect(put).not.toHaveBeenCalled()
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
