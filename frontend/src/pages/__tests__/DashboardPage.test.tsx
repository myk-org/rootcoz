import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from '@/pages/DashboardPage'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    username: 'alice',
    isAdmin: true,
    isOperator: true,
    canViewReports: true,
    canUseServerProviders: false,
    canUseFreeProviders: false,
    role: 'admin',
    loading: false,
    authenticated: true,
    login: vi.fn(),
    logout: vi.fn(),
    refreshAuth: vi.fn(),
  }),
}))

// Radix Select opens its listbox through pointer capture, which jsdom lacks.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

function job(overrides: Record<string, unknown> = {}) {
  return {
    job_id: 'job-1',
    job_name: 'my-test-job',
    status: 'completed',
    created_at: '2024-01-02 10:00:00',
    failure_count: 2,
    passed_count: 5,
    skipped_count: 0,
    reviewed_count: 0,
    comment_count: 0,
    child_job_count: 0,
    analysis_state: 'analyzed',
    metadata: { labels: [] },
    ...overrides,
  }
}

/** Serve the requested page of jobs; records the limit/offset the page asked for. */
const requests: URLSearchParams[] = []

function mockApi(total: number) {
  requests.length = 0
  apiGet.mockImplementation((path: string) => {
    if (path.startsWith('/api/dashboard/filtered')) {
      const params = new URLSearchParams(path.split('?')[1] ?? '')
      requests.push(params)
      const limit = Number(params.get('limit') ?? 20)
      const offset = Number(params.get('offset') ?? 0)
      const jobs = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) =>
        job({ job_id: `job-${offset + i + 1}`, job_name: `job-${offset + i + 1}` }),
      )
      return Promise.resolve({ jobs, total })
    }
    return Promise.resolve([])
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

/** The filter row must hold filters only; presentation controls live in the summary row. */
async function rows() {
  await waitFor(() => expect(screen.getByTestId('dashboard-summary')).toBeInTheDocument())
  return {
    filters: within(screen.getByTestId('dashboard-filters')),
    summary: within(screen.getByTestId('dashboard-summary')),
  }
}

describe('DashboardPage presentation controls', () => {
  beforeEach(() => {
    mockApi(5)
  })

  afterEach(() => {
    apiGet.mockReset()
  })

  it('renders page size, view toggle and count in the summary row', async () => {
    renderPage()
    const { filters, summary } = await rows()

    expect(summary.getByText(/analysis runs?/)).toBeInTheDocument()
    expect(summary.getByLabelText('Rows per page')).toBeInTheDocument()
    expect(summary.getByLabelText('Flat list view')).toBeInTheDocument()
    expect(summary.getByLabelText('Grouped by job view')).toBeInTheDocument()

    expect(filters.queryByLabelText('Rows per page')).not.toBeInTheDocument()
    expect(filters.queryByLabelText('Flat list view')).not.toBeInTheDocument()
    expect(filters.queryByLabelText('Grouped by job view')).not.toBeInTheDocument()
    expect(filters.queryByText(/analysis runs?/)).not.toBeInTheDocument()
  })

  it('keeps search in the filter row', async () => {
    renderPage()
    const { filters, summary } = await rows()

    expect(filters.getByPlaceholderText('Filter jobs...')).toBeInTheDocument()
    expect(summary.queryByPlaceholderText('Filter jobs...')).not.toBeInTheDocument()
  })

  it('changing page size refetches with the new limit and resets to page 1', async () => {
    mockApi(25)
    renderPage()
    const { summary } = await rows()
    await waitFor(() => expect(requests[0]?.get('limit')).toBe('20'))

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(requests.at(-1)?.get('offset')).toBe('20'))

    await userEvent.click(summary.getByLabelText('Rows per page'))
    await userEvent.click(await screen.findByRole('option', { name: '50' }))

    await waitFor(() => expect(requests.at(-1)?.get('limit')).toBe('50'))
    expect(requests.at(-1)?.get('offset')).toBe('0')
  })

  it('shows expand/collapse only in grouped mode', async () => {
    renderPage()
    const { summary } = await rows()

    expect(summary.queryByRole('button', { name: 'Expand All' })).not.toBeInTheDocument()

    await userEvent.click(summary.getByLabelText('Grouped by job view'))

    await waitFor(() =>
      expect(summary.getByRole('button', { name: 'Expand All' })).toBeInTheDocument(),
    )
    expect(summary.getByRole('button', { name: 'Collapse All' })).toBeInTheDocument()
    // Still in the summary row, never in the filter row.
    const { filters } = await rows()
    expect(filters.queryByRole('button', { name: 'Expand All' })).not.toBeInTheDocument()
  })
})
