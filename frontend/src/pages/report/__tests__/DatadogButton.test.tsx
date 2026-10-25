import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { DatadogButton } from '@/pages/report/DatadogButton'
import type { DatadogPushResult } from '@/types'

const mockPost = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    post: (...args: unknown[]) => mockPost(...args),
  },
}))

function makeResult(overrides: Partial<DatadogPushResult> = {}): DatadogPushResult {
  return {
    success: true,
    message: 'Pushed 2 Datadog event(s)',
    pushed: 2,
    total: 2,
    errors: [],
    skipped: 0,
    ...overrides,
  }
}

function renderButton(hasFailures = true) {
  return render(
    <DatadogButton
      jobId="job-1"
      jobName="test-job"
      buildNumber={42}
      hasFailures={hasFailures}
    />,
  )
}

async function confirmPush() {
  fireEvent.click(screen.getByRole('button', { name: /push to datadog/i }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: /^push$/i })).toBeDefined()
  })
  fireEvent.click(screen.getByRole('button', { name: /^push$/i }))
}

describe('DatadogButton', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPost.mockResolvedValue(makeResult())
  })

  it('renders the push button when failures exist', () => {
    renderButton(true)
    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeDefined()
  })

  it('disables the button when there are no failures', () => {
    renderButton(false)
    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeDisabled()
  })

  it('calls the datadog push endpoint on confirm', async () => {
    renderButton(true)
    await confirmPush()

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/datadog')
    })
  })

  it('shows a success result dialog with the pushed count', async () => {
    renderButton(true)
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Pushed 2 events to Datadog.')).toBeDefined()
    })
  })

  it('shows a partial-failure result dialog with errors and skipped counts', async () => {
    mockPost.mockResolvedValue(
      makeResult({
        success: false,
        pushed: 1,
        total: 3,
        errors: ['event 1 failed'],
        skipped: 1,
      }),
    )

    renderButton(true)
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Some events could not be pushed.')).toBeDefined()
    })
    expect(screen.getByText('event 1 failed')).toBeDefined()
    expect(screen.getByText('1 event skipped')).toBeDefined()
  })

  it('shows a full-failure result dialog when the request fails', async () => {
    mockPost.mockRejectedValue(new Error('Network error'))

    renderButton(true)
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Failed to push events to Datadog.')).toBeDefined()
    })
  })
})
