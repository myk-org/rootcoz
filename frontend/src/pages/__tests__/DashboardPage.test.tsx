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
    submitted_by: 'alice',
    metadata: { labels: [] },
    ...overrides,
  }
}

/** Serve the requested page of jobs; records the limit/offset the page asked for. */
const requests: URLSearchParams[] = []

/** Odd rows belong to yuval, even rows to alice, so a submitter filter narrows. */
const submitted = (index: number) => (index % 2 === 1 ? 'yuval' : 'alice')

function mockApi(total: number, submitters = ['alice', 'yuval']) {
  requests.length = 0
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/dashboard/submitters') {
      return Promise.resolve({ submitters })
    }
    if (path.startsWith('/api/dashboard/filtered')) {
      const params = new URLSearchParams(path.split('?')[1] ?? '')
      requests.push(params)
      const wanted = params.getAll('submitted_by').map((v) => v.toLowerCase()).filter(Boolean)
      const jobs = Array.from({ length: total }, (_, i) =>
        job({ job_id: `job-${i + 1}`, job_name: `job-${i + 1}`, submitted_by: submitted(i) }),
      ).filter((j) => wanted.length === 0 || wanted.some((w) => j.submitted_by.toLowerCase().includes(w)))
      const limit = Number(params.get('limit') ?? 20)
      const offset = Number(params.get('offset') ?? 0)
      return Promise.resolve({ jobs: jobs.slice(offset, offset + limit), total: jobs.length })
    }
    return Promise.resolve([])
  })
}

function renderPage(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <DashboardPage />
    </MemoryRouter>,
  )
}

/** Open the submitter picker and type a term. */
async function searchSubmitter(value: string) {
  await userEvent.click(screen.getByRole('combobox', { name: 'Add Submitted by' }))
  await userEvent.type(screen.getByRole('combobox', { name: 'Search Submitted by' }), value)
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

describe('DashboardPage submitter filter', () => {
  afterEach(() => {
    apiGet.mockReset()
  })

  it('lists every endpoint name as an option, and picks one into the request', async () => {
    mockApi(5, ['Yuval', 'alice'])
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await searchSubmitter('yuva')
    expect(screen.getAllByRole('option')).toHaveLength(2) // Yuval + Add "yuva"
    await userEvent.click(screen.getByRole('option', { name: 'Yuval' }))

    await waitFor(() => expect(requests.at(-1)?.getAll('submitted_by')).toEqual(['Yuval']))
    expect(await screen.findByText('2 analysis runs')).toBeInTheDocument()
    expect(screen.queryByText('job-1')).not.toBeInTheDocument()
  })

  it('keeps a comma-bearing name as one filter', async () => {
    mockApi(5, ['Doe, Jane', 'alice'])
    renderPage('/?submitted_by=Doe%2C%20Jane')

    // One URL value stays one SQL filter: splitting on the comma would send
    // `Doe` and ` Jane` and mix their jobs into the result.
    await waitFor(() =>
      expect(requests.at(-1)?.getAll('submitted_by')).toEqual(['Doe, Jane']),
    )
  })

  it('folds a differently-cased shared link onto the enumerated name', async () => {
    mockApi(5)
    renderPage('/?submitted_by=ALICE')

    // Matching is case-insensitive server-side, so ALICE and alice are one pick.
    await waitFor(() => expect(requests.at(-1)?.getAll('submitted_by')).toEqual(['alice']))

    await searchSubmitter('alice')
    await userEvent.click(screen.getByRole('option', { name: 'alice' }))
    // Toggling the option off, not adding a second chip beside the booking.
    await waitFor(() => expect(requests.at(-1)?.get('submitted_by')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Remove alice' })).not.toBeInTheDocument()
  })

  it('counts a name in characters, so emoji names survive', async () => {
    // 33 emoji are 66 UTF-16 units but 33 characters — the server accepts them.
    const emoji = '\u{1F600}'.repeat(33)
    mockApi(5, [emoji])
    renderPage(`/?submitted_by=${encodeURIComponent(emoji)}`)

    await waitFor(() => expect(requests.length).toBeGreaterThan(0))
    expect(requests.at(-1)?.getAll('submitted_by')).toEqual([emoji])
    await rows()
  })

  it('drops values the server rejects instead of failing the whole load', async () => {
    const long = 'x'.repeat(65)
    const many = Array.from({ length: 51 }, (_, i) => `user-${i}`)
    const query = [...many, long]
      .map((value) => `submitted_by=${encodeURIComponent(value)}`)
      .join('&')
    mockApi(5)
    renderPage(`/?${query}`)

    // 50 values is the server's cap and 64 chars its per-value limit; sending
    // them as-is would 400 the dashboard with a load error.
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))
    const sent = requests.at(-1)?.getAll('submitted_by') ?? []
    expect(sent).toHaveLength(50)
    expect(sent).not.toContain(long)
    await rows()
  })

  it('sends a typed partial that no option offers', async () => {
    mockApi(5)
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await searchSubmitter('yuva')
    // Not an option and not an exact name: the free-entry row is the only way.
    await userEvent.click(screen.getByRole('option', { name: 'Add "yuva"' }))

    await waitFor(() => expect(requests.at(-1)?.getAll('submitted_by')).toEqual(['yuva']))
    expect(await screen.findByText('2 analysis runs')).toBeInTheDocument()
  })

  it('combines with another filter and resets the offset', async () => {
    mockApi(25)
    renderPage('/?status=completed')
    await waitFor(() => expect(requests[0]?.get('offset')).toBe('0'))

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(requests.at(-1)?.get('offset')).toBe('20'))

    await searchSubmitter('yuval')
    await userEvent.click(screen.getByRole('option', { name: 'yuval' }))

    await waitFor(() => expect(requests.at(-1)?.get('submitted_by')).toBe('yuval'))
    const last = requests.at(-1)
    expect(last?.getAll('status')).toEqual(['completed'])
    expect(last?.get('offset')).toBe('0')
  })

  it('clears the filter and hides the Clear button again', async () => {
    mockApi(5)
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await searchSubmitter('yuval')
    await userEvent.click(screen.getByRole('option', { name: 'yuval' }))
    await waitFor(() => expect(requests.at(-1)?.get('submitted_by')).toBe('yuval'))

    // A submitter alone is a filter: the Clear button appears for it.
    await userEvent.click(await screen.findByRole('button', { name: /Clear filters/ }))

    await waitFor(() => expect(requests.at(-1)?.get('submitted_by')).toBeNull())
    expect(screen.queryByRole('button', { name: /Clear filters/ })).not.toBeInTheDocument()
  })

  it('honours a bookmarkable ?submitted_by= link', async () => {
    mockApi(5)
    renderPage('/?submitted_by=yuval')

    await waitFor(() => expect(requests[0]?.getAll('submitted_by')).toEqual(['yuval']))
    expect(await screen.findByText('2 analysis runs')).toBeInTheDocument()
  })

  it('stays usable when the endpoint returns nothing', async () => {
    mockApi(5, [])
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await searchSubmitter('yuva')
    await userEvent.click(screen.getByRole('option', { name: 'Add "yuva"' }))

    await waitFor(() => expect(requests.at(-1)?.get('submitted_by')).toBe('yuva'))
  })
})
