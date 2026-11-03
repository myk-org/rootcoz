import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

const { sseHandlers } = vi.hoisted(() => ({
  sseHandlers: {} as Record<string, () => void | Promise<void>>,
}))

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({
  useSSE: (_topic: string | null, events: Record<string, () => void | Promise<void>>) => {
    Object.assign(sseHandlers, events)
  },
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: 'operator', isOperator: true, isViewer: false }),
}))

function makeResponse(allowUnreviewedPush: boolean): ResultResponse {
  return {
    job_id: 'job-1',
    jenkins_url: null,
    status: 'completed',
    created_at: '',
    base_url: null,
    result_url: null,
    result: {
      job_id: 'job-1',
      job_name: 'gate-job',
      build_number: 1,
      jenkins_url: null,
      status: 'completed',
      summary: '',
      ai_provider: '',
      ai_model: '',
      failures: [
        {
          id: 'f-a',
          test_name: 'test-a',
          error: 'error-a',
          analysis: { classification: 'PRODUCT BUG', pattern: '', affected_tests: ['test-a'], details: '', artifacts_evidence: '' },
          error_signature: 'sig-a',
        },
      ],
      child_job_analyses: [],
    },
    capabilities: {
      github_issues_enabled: false,
      jira_issues_enabled: false,
      datadog: true,
      exporters: [{ name: 'datadog', display_name: 'Datadog', enabled: true, allow_unreviewed_push: allowUnreviewedPush }],
    },
  }
}

/** Renders the report with the given hatch state and returns a switch that
 *  flips it for subsequent result fetches. */
function renderReport(allowUnreviewedPush: boolean) {
  let hatchOn = allowUnreviewedPush
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === '/results/job-1') return makeResponse(hatchOn)
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.mocked(api.post).mockResolvedValue({ enrichments: {} })
  const view = render(
    <MemoryRouter initialEntries={['/report/job-1']}>
      <Routes>
        <Route path="/report/:jobId" element={<ReportPage />} />
      </Routes>
    </MemoryRouter>,
  )
  return {
    flipHatch: () => {
      hatchOn = true
    },
    ...view,
  }
}

describe('report push-gate flag freshness', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    for (const key of Object.keys(sseHandlers)) delete sseHandlers[key]
  })

  it('disables the push button while the hatch is off and unreviewed failures exist', async () => {
    renderReport(false)
    const button = await screen.findByRole('button', { name: /push to datadog/i })
    expect(button).toBeDisabled()
  })

  it('updates the push button when a later fetch returns changed gate flags', async () => {
    const { flipHatch } = renderReport(false)
    const button = await screen.findByRole('button', { name: /push to datadog/i })
    expect(button).toBeDisabled()

    // Admin flips the escape hatch while the report is open; the SSE
    // status-changed event triggers a result refetch carrying new flags.
    flipHatch()
    await sseHandlers['status-changed']()

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /push to datadog/i })).toBeEnabled()
    })
  })
})
