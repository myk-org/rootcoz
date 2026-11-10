import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ReportsPage } from '@/pages/ReportsPage'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    username: 'alice',
    role: 'admin',
    isAdmin: true,
    isOperator: true,
    isViewer: false,
    canViewReports: true,
  }),
}))

// Radix Select opens through pointer capture, which jsdom lacks.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

/** The reports requests this page made, in order. */
const reportRequests: URLSearchParams[] = []

function mockApi() {
  reportRequests.length = 0
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/jobs/metadata') return Promise.resolve([])
    if (path === '/api/dashboard/submitters') {
      return Promise.resolve({ submitters: ['alice', 'yuval'] })
    }
    if (path.startsWith('/api/user/filter-presets')) return Promise.resolve([])
    if (path.startsWith('/api/reports/totals')) {
      reportRequests.push(new URLSearchParams(path.split('?')[1] ?? ''))
      return Promise.resolve({
        total_jobs: 0,
        total_failures: 0,
        total_reviewed: 0,
        total_details: 0,
        jobs: [],
      })
    }
    return Promise.resolve([])
  })
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>
}

function renderReports(route = '/reports') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route
          path="/reports"
          element={
            <>
              <ReportsPage />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ReportsPage filters', () => {
  beforeEach(() => {
    mockApi()
  })
  afterEach(() => {
    apiGet.mockReset()
  })

  it('renders the submitter and analysis-state filters next to the others', async () => {
    renderReports()

    expect(await screen.findByRole('combobox', { name: 'Add Submitted by' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Mode filter' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'All statuses' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Date range filter' })).toBeInTheDocument()
  })

  it('writes the canonical date keys and sends them', async () => {
    // An old bookmark still carries from/to; the page reads them and then
    // writes the canonical pair the dashboard uses.
    renderReports('/reports?from=2026-01-01&to=2026-02-01')

    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/reports?date_from=2026-01-01&date_to=2026-02-01',
      ),
    )
    await waitFor(() =>
      expect(reportRequests.at(-1)?.get('date_from')).toBe('2026-01-01'),
    )
    expect(reportRequests.at(-1)?.get('date_to')).toBe('2026-02-01')
    expect(reportRequests.at(-1)?.get('from')).toBeNull()
  })

  it('writes a picked submitter into the URL and the request', async () => {
    renderReports()
    await waitFor(() => expect(reportRequests.length).toBeGreaterThan(0))

    await userEvent.click(screen.getByRole('combobox', { name: 'Add Submitted by' }))
    await userEvent.click(await screen.findByRole('option', { name: 'yuval' }))

    await waitFor(() => expect(reportRequests.at(-1)?.getAll('submitted_by')).toEqual(['yuval']))
    expect(screen.getByTestId('location-probe').textContent).toContain('submitted_by=yuval')
  })

  it('writes the analysis state into the URL and the request', async () => {
    renderReports()
    await waitFor(() => expect(reportRequests.length).toBeGreaterThan(0))

    await userEvent.click(screen.getByRole('combobox', { name: 'Mode filter' }))
    await userEvent.click(await screen.findByRole('option', { name: 'Submitted' }))

    await waitFor(() => expect(reportRequests.at(-1)?.get('analysis_state')).toBe('submitted'))
    expect(screen.getByTestId('location-probe').textContent).toContain('analysis_state=submitted')
  })

  it('honours a link that already carries both new filters', async () => {
    renderReports('/reports?submitted_by=yuval&analysis_state=analyzed')

    await waitFor(() => expect(reportRequests.length).toBeGreaterThan(0))
    const sent = reportRequests.at(-1)
    expect(sent?.getAll('submitted_by')).toEqual(['yuval'])
    expect(sent?.get('analysis_state')).toBe('analyzed')
    // The picks are visible, not just applied behind the user's back.
    expect(screen.getByRole('button', { name: 'Remove yuval' })).toBeInTheDocument()
    expect(screen.getByText('Mode: Analyzed')).toBeInTheDocument()
  })
})
