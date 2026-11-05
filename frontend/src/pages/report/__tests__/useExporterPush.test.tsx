import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { api, ApiError } from '@/lib/api'
import { pushToExporter, useExporterPush } from '../useExporterPush'

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

const postSpy = vi.spyOn(api, 'post')

function HookHarness(props: Parameters<typeof useExporterPush>[0] & {
  extra?: { subject_identifier?: string; waiver_comment?: string }
}) {
  const { extra, ...options } = props
  const { push, pushing, result, error, reset } = useExporterPush(options)
  return (
    <>
      <span data-testid="pushing">{String(pushing)}</span>
      <span data-testid="result">{result ? JSON.stringify(result) : ''}</span>
      <span data-testid="error">{error ?? ''}</span>
      <button data-testid="push" onClick={() => void push(extra)}>push</button>
      <button data-testid="reset" onClick={reset}>reset</button>
    </>
  )
}

beforeEach(() => {
  postSpy.mockReset()
})

describe('pushToExporter', () => {
  it('builds the generic URL for any plugin and scope, never the legacy one', async () => {
    postSpy.mockResolvedValue({ pushed: 1, unmatched: [], errors: [], launch_id: 4 } as never)
    await pushToExporter('job-9', 'reportportal', { childJobName: 'c', childBuildNumber: 3 })

    expect(postSpy).toHaveBeenCalledWith(
      '/results/job-9/push/reportportal?child_job_name=c&child_build_number=3',
    )
    // Guard: reintroducing a direct legacy `push-reportportal` call must fail
    expect(postSpy.mock.calls.some((c) => String(c[0]).includes('push-reportportal'))).toBe(false)
  })
})

describe('useExporterPush', () => {
  it('builds the generic plugin URL with only the non-empty params', async () => {
    postSpy.mockResolvedValue({ success: true, message: 'ok' } as never)
    render(
      <HookHarness
        jobId="job-1"
        plugin="greenwave"
        childJobName="child-a"
        childBuildNumber={7}
        extra={{ subject_identifier: '   ', waiver_comment: 'known flake' }}
      />,
    )
    fireEvent.click(screen.getByTestId('push'))

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        '/results/job-1/push/greenwave?child_job_name=child-a&child_build_number=7&waiver_comment=known+flake',
      )
    })
  })

  it('sends no query string when every param is empty', async () => {
    postSpy.mockResolvedValue({ success: true, message: 'ok' } as never)
    render(<HookHarness jobId="job-2" plugin="reportportal" />)
    fireEvent.click(screen.getByTestId('push'))

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith('/results/job-2/push/reportportal')
    })
  })

  it('stores the result and clears it on reset', async () => {
    postSpy.mockResolvedValue({ success: true, message: 'pushed', pushed: 3 } as never)
    render(<HookHarness jobId="job-1" plugin="greenwave" />)
    fireEvent.click(screen.getByTestId('push'))

    await waitFor(() => {
      expect(screen.getByTestId('result').textContent).toContain('"pushed":3')
    })

    fireEvent.click(screen.getByTestId('reset'))
    expect(screen.getByTestId('result').textContent).toBe('')
  })

  it('surfaces the server detail message on error', async () => {
    postSpy.mockRejectedValueOnce(
      new ApiError(422, 'Unprocessable Entity', { detail: 'subject_identifier is required for waivers' }),
    )
    render(<HookHarness jobId="job-1" plugin="greenwave" />)
    fireEvent.click(screen.getByTestId('push'))

    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('subject_identifier is required for waivers')
    })
    expect(screen.getByTestId('result').textContent).toBe('')
  })
})
