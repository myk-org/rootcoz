import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { JobMetadata, ResultResponse } from '@/types'

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
  useAuth: () => ({ role: 'viewer', isOperator: false }),
}))

function makeResponse({
  metadata = null,
  tags,
}: {
  metadata?: JobMetadata | null
  tags?: string[]
} = {}): ResultResponse {
  return {
    job_id: 'job-670',
    jenkins_url: null,
    status: 'completed',
    created_at: '2026-01-15T10:00:00Z',
    base_url: null,
    result_url: null,
    metadata,
    result: {
      job_id: 'job-670',
      job_name: 'mtv-2.12-ocp-4.22-test-release-gate',
      build_number: 123,
      jenkins_url: null,
      status: 'completed',
      summary: 'All passed.',
      ai_provider: '',
      ai_model: '',
      failures: [],
      child_job_analyses: [],
      ...(tags !== undefined ? { tags } : {}),
    },
  }
}

function renderReport(getResponse: () => ResultResponse) {
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === '/results/job-670') return getResponse()
    if (path === '/results/job-670/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
    if (path === '/results/job-670/enrich-comments') return { enrichments: {} }
    throw new Error(`Unexpected request: ${path}`)
  })
  return render(
    <MemoryRouter initialEntries={['/results/job-670']}>
      <Routes>
        <Route path="/results/:jobId" element={<ReportPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ReportPage metadata labels and job tags (#670)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    for (const key of Object.keys(sseHandlers)) delete sseHandlers[key]
  })

  it('displays metadata badges (team, tier, version, labels) and per-job tags in the header', async () => {
    renderReport(() =>
      makeResponse({
        metadata: {
          job_name: 'mtv-2.12-ocp-4.22-test-release-gate',
          team: 'mtv',
          tier: 'critical',
          version: '2.12',
          labels: ['gating', 'vsphere8'],
        },
        tags: ['re-analyze', 'rnetser'],
      }),
    )

    const row = await screen.findByTestId('job-metadata-tags')
    expect(within(row).getByText('mtv')).toBeInTheDocument()
    expect(within(row).getByText('critical')).toBeInTheDocument()
    expect(within(row).getByText('2.12')).toBeInTheDocument()
    expect(within(row).getByText('gating')).toBeInTheDocument()
    expect(within(row).getByText('vsphere8')).toBeInTheDocument()
    expect(within(row).getByText('re-analyze')).toBeInTheDocument()
    expect(within(row).getByText('rnetser')).toBeInTheDocument()
  })

  it('displays only metadata badges when a job has labels but no tags', async () => {
    renderReport(() =>
      makeResponse({
        metadata: {
          job_name: 'mtv-2.12-ocp-4.22-test-release-gate',
          team: 'mtv',
          tier: null,
          version: '2.12',
          labels: ['gating'],
        },
        tags: [],
      }),
    )

    const row = await screen.findByTestId('job-metadata-tags')
    expect(within(row).getByText('mtv')).toBeInTheDocument()
    expect(within(row).getByText('2.12')).toBeInTheDocument()
    expect(within(row).getByText('gating')).toBeInTheDocument()
  })

  it('displays only tag pills when a job has tags but no metadata', async () => {
    renderReport(() =>
      makeResponse({
        metadata: null,
        tags: ['regression', 'rnetser'],
      }),
    )

    const row = await screen.findByTestId('job-metadata-tags')
    expect(within(row).getByText('regression')).toBeInTheDocument()
    expect(within(row).getByText('rnetser')).toBeInTheDocument()
  })

  it('omits the metadata/tags container cleanly when a job has neither metadata nor tags', async () => {
    renderReport(() => makeResponse({ metadata: null, tags: [] }))

    await screen.findByRole('heading', { name: 'mtv-2.12-ocp-4.22-test-release-gate' })
    expect(screen.queryByTestId('job-metadata-tags')).not.toBeInTheDocument()
  })

  it('updates displayed metadata and tags after an SSE status-changed refresh', async () => {
    let current = makeResponse({ metadata: null, tags: [] })
    renderReport(() => current)

    await screen.findByRole('heading', { name: 'mtv-2.12-ocp-4.22-test-release-gate' })
    expect(screen.queryByTestId('job-metadata-tags')).not.toBeInTheDocument()

    current = makeResponse({
      metadata: {
        job_name: 'mtv-2.12-ocp-4.22-test-release-gate',
        team: 'mtv',
        tier: null,
        version: '2.12',
        labels: ['gating'],
      },
      tags: ['verified'],
    })
    await sseHandlers['status-changed']()

    await waitFor(() => {
      const row = screen.getByTestId('job-metadata-tags')
      expect(within(row).getByText('gating')).toBeInTheDocument()
      expect(within(row).getByText('verified')).toBeInTheDocument()
    })
  })
})
