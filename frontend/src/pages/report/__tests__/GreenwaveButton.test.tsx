import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ApiError } from '@/lib/api'
import { GreenwaveButton } from '../GreenwaveButton'

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }))

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      post: (...args: unknown[]) => mockPost(...args),
    },
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  mockPost.mockResolvedValue({})
})

describe('GreenwaveButton', () => {
  it('renders the Push to Greenwave button; disabled when hasFailures is false', () => {
    const { rerender } = render(<GreenwaveButton jobId="job-1" hasFailures={false} />)
    let btn = screen.getByRole('button', { name: /Push to Greenwave/i })
    expect(btn).toBeDisabled()

    rerender(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    btn = screen.getByRole('button', { name: /Push to Greenwave/i })
    expect(btn).not.toBeDisabled()
  })

  it('clicking the button opens the dialog showing waiver justification and subject identifier', async () => {
    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    const btn = screen.getByRole('button', { name: /Push to Greenwave/i })
    fireEvent.click(btn)

    expect(await screen.findByText('Subject identifier (build NVR)')).toBeDefined()
    expect(screen.getByPlaceholderText('e.g. myproduct-bundle-registry-container-vX.Y.Z-BN')).toBeDefined()
    expect(screen.getByText('Waiver justification (optional)')).toBeDefined()
    expect(screen.getByPlaceholderText('Why is this being waived? (max 500 chars)')).toBeDefined()
  })

  it('updates the character counter when typing into the waiver textarea', async () => {
    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))

    const textarea = await screen.findByPlaceholderText('Why is this being waived? (max 500 chars)')
    expect(screen.getByText('0/500')).toBeDefined()

    fireEvent.change(textarea, { target: { value: 'Hello' } })
    expect(screen.getByText('5/500')).toBeDefined()
  })

  it('pushes values and shows success message', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 1, skipped: 0, waived: 1, errors: [],
      details: { resultsdb_ids: [1], waiver_ids: [1], group_uuid: 'g' },
      success: true, message: 'ok'
    })

    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))

    const subjectInput = await screen.findByPlaceholderText('e.g. myproduct-bundle-registry-container-vX.Y.Z-BN')
    const textarea = screen.getByPlaceholderText('Why is this being waived? (max 500 chars)')

    fireEvent.change(subjectInput, { target: { value: 'my-subject' } })
    fireEvent.change(textarea, { target: { value: 'my comment' } })

    fireEvent.click(screen.getByRole('button', { name: 'Push' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/results/job-1/push/greenwave',
        { subject_identifier: 'my-subject', waiver_comment: 'my comment' },
      )
    })

    expect(await screen.findByText('Pushed to Greenwave successfully.')).toBeDefined()
  })

  it('omits empty params from the push request', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 1, skipped: 0, waived: 1, errors: [],
      details: {}, success: true, message: 'ok'
    })

    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))

    const pushBtn = await screen.findByRole('button', { name: 'Push' })
    fireEvent.click(pushBtn)

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/greenwave', {})
    })
  })

  it('adds child scope through the shared exporter request options', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 1, skipped: 0, waived: 0, errors: [],
      details: {}, success: true, message: 'ok'
    })

    render(
      <GreenwaveButton
        jobId="job-1"
        childJobName="child/job"
        childBuildNumber={42}
        hasFailures={true}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Push' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/results/job-1/push/greenwave?child_job_name=child%2Fjob&child_build_number=42',
        {},
      )
    })
  })

  it('shows partial success when some ResultsDB writes land', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 1, skipped: 0, waived: 0, errors: ['test_b (WaiverDB): HTTP 503'],
      details: { resultsdb_ids: [1], waiver_ids: [], group_uuid: 'g' },
      success: true, message: 'Pushed 1 result(s) to ResultsDB, 0 waiver(s)'
    })

    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Push' }))

    expect(await screen.findByText('Pushed to Greenwave with errors.')).toBeDefined()
    expect(screen.getByText('test_b (WaiverDB): HTTP 503')).toBeDefined()
  })

  it('shows a neutral no-op state when every classification is skipped', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 0, skipped: 2, waived: 0, errors: [],
      details: { resultsdb_ids: [], waiver_ids: [], group_uuid: 'g' },
      success: false,
      message: 'No results pushed (2 skipped — no matching classifications)'
    })

    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Push' }))

    expect(await screen.findByText('No Greenwave results needed pushing.')).toBeDefined()
    expect(screen.getByText('No results pushed (2 skipped — no matching classifications)')).toBeDefined()
  })

  it('shows the sanitized API diagnostic when the push request is rejected', async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(502, 'Bad Gateway', { detail: 'Safe exporter error detail' }),
    )

    render(<GreenwaveButton jobId="job-1" hasFailures={true} />)
    fireEvent.click(screen.getByRole('button', { name: /Push to Greenwave/i }))

    const pushBtn = await screen.findByRole('button', { name: 'Push' })
    fireEvent.click(pushBtn)

    expect(await screen.findByText('Failed to push to Greenwave.')).toBeDefined()
    expect(screen.getByText('Safe exporter error detail')).toBeDefined()
  })
})
