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

  it('folds a shared link onto the server spelling, so one name is one pick', async () => {
    // The link was written with a different case; the endpoint matches
    // case-insensitively, so two chips here would be one filter twice.
    renderReports('/reports?submitted_by=ALICE')

    await waitFor(() => expect(reportRequests.at(-1)?.getAll('submitted_by')).toEqual(['alice']))
    expect(screen.getByRole('button', { name: 'Remove alice' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove ALICE' })).toBeNull()
  })

  it('drops a submitter the endpoint would reject instead of failing the request', async () => {
    // Over the 64-character cap the reports endpoint answers 400: the shared
    // normalizer bounds the value here instead of shipping a broken request.
    const tooLong = 'a'.repeat(65)
    renderReports(`/reports?submitted_by=${tooLong}`)

    await waitFor(() => expect(reportRequests.length).toBeGreaterThan(0))
    expect(reportRequests.at(-1)?.getAll('submitted_by')).toEqual([])
  })

  it('narrows the report by failing test name, like the dashboard', async () => {
    renderReports('/reports?test_name=needle&test_name_mode=exact')

    await waitFor(() => expect(reportRequests.at(-1)?.get('test_name')).toBe('needle'))
    expect(reportRequests.at(-1)?.get('test_name_mode')).toBe('exact')
    // The pick is visible, not applied behind the user's back.
    expect(screen.getByRole('combobox', { name: 'Select Test name' })).toHaveTextContent('needle')
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

  it('keeps search in URL and presets without sending search to the reports API', async () => {
    const presetWithoutSearch = {
      id: 1,
      name: 'Storage only',
      page: 'reports',
      scope: 'user',
      filters: { team: ['storage'] },
      created_at: '2026-01-01T00:00:00',
      updated_at: '2026-01-01T00:00:00',
    }
    const presetWithSearch = {
      id: 2,
      name: 'Virt search',
      page: 'reports',
      scope: 'user',
      filters: { search: 'virt', team: ['storage'] },
      created_at: '2026-01-02T00:00:00',
      updated_at: '2026-01-02T00:00:00',
    }
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/user/filter-presets')
        ? Promise.resolve([presetWithoutSearch, presetWithSearch])
        : fallback(path),
    )

    renderReports('/reports?search=kubevirt')
    const input = screen.getByPlaceholderText('Filter reports...')
    expect(input).toHaveValue('kubevirt')
    await waitFor(() => expect(reportRequests.length).toBeGreaterThan(0))
    expect(reportRequests.at(-1)?.get('search')).toBeNull()
    expect(screen.getByTestId('location-probe').textContent).toBe('/reports?search=kubevirt')

    // Applying a preset without search clears search from input and URL
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-1'))
    await waitFor(() => expect(input).toHaveValue(''))
    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/reports?team=storage'),
    )
    expect(reportRequests.at(-1)?.get('search')).toBeNull()

    // Applying a preset with search restores search in input and URL, still omitted from API
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-2'))
    await waitFor(() => expect(input).toHaveValue('virt'))
    await waitFor(() => {
      const sp = new URLSearchParams(screen.getByTestId('location-probe').textContent?.split('?')[1] ?? '')
      expect(sp.get('search')).toBe('virt')
      expect(sp.getAll('team')).toEqual(['storage'])
    })
    expect(reportRequests.at(-1)?.get('search')).toBeNull()
  })

  it('shows Clear filters and inline clear for search-only state on reports and clears both', async () => {
    renderReports('/reports?search=kubevirt')
    const input = screen.getByPlaceholderText('Filter reports...')
    expect(input).toHaveValue('kubevirt')

    expect(screen.getByRole('button', { name: 'Clear search' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Clear filters' })).toBeInTheDocument()

    // Inline clear resets search and hides both buttons
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(input).toHaveValue('')
    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/reports'),
    )
    expect(screen.queryByRole('button', { name: 'Clear search' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()

    // Typing again shows Clear filters; clicking Clear filters resets input and URL
    await userEvent.type(input, 'storage')
    expect(screen.getByRole('button', { name: 'Clear filters' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(input).toHaveValue('')
    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/reports'),
    )
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
  })

  it('shows Clear filters for date-only state on reports and clears date range and request', async () => {
    renderReports('/reports?date_from=2026-11-01&date_to=2026-11-10')
    await waitFor(() =>
      expect(reportRequests.at(-1)?.get('date_from')).toBe('2026-11-01'),
    )
    expect(reportRequests.at(-1)?.get('date_to')).toBe('2026-11-10')

    const clearAllBtn = screen.getByRole('button', { name: 'Clear filters' })
    expect(clearAllBtn).toBeInTheDocument()

    await userEvent.click(clearAllBtn)

    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/reports'),
    )
    await waitFor(() => {
      const last = reportRequests.at(-1)
      expect(last?.get('date_from')).toBeNull()
      expect(last?.get('date_to')).toBeNull()
    })
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
  })

  it('clears all filters on reports while preserving the active report tab', async () => {
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/reports/classification-overrides')) {
        reportRequests.push(new URLSearchParams(path.split('?')[1] ?? ''))
        return Promise.resolve({
          total: 0,
          total_reviewed: 0,
          ai_correct: 0,
          ai_accuracy_pct: 100,
          groups: [],
          details: [],
        })
      }
      return fallback(path)
    })

    renderReports('/reports?report=classification-overrides&search=virt&date_from=2026-11-01&team=storage&test_name=test_x')
    await waitFor(() => expect(reportRequests.at(-1)?.get('date_from')).toBe('2026-11-01'))

    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))

    expect(screen.getByPlaceholderText('Filter reports...')).toHaveValue('')
    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/reports?report=classification-overrides'),
    )
    await waitFor(() => {
      const last = reportRequests.at(-1)
      expect(last?.get('date_from')).toBeNull()
      expect(last?.get('team')).toBeNull()
      expect(last?.get('test_name')).toBeNull()
    })
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
  })
})
