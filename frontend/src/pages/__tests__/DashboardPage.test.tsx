import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from '@/pages/DashboardPage'
import { notifyPresetApplied, resetLastAppliedPresetForTests } from '@/lib/filterPresets'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPut = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => apiGet(...args),
      post: (...args: unknown[]) => apiPost(...args),
      put: (...args: unknown[]) => apiPut(...args),
    },
  }
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

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>
}

function NavControls() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate(-1)}>Go back</button>
      <button type="button" onClick={() => navigate('/?search=external&team=net')}>Nav external</button>
      <button type="button" onClick={() => navigate('/')}>Nav clean</button>
      <button
        type="button"
        onClick={() => {
          notifyPresetApplied(1, 'dashboard')
          navigate('/?team=storage')
        }}
      >
        Sidebar preset no search
      </button>
    </>
  )
}

function renderPage(route = '/', initialEntries?: string[]) {
  return render(
    <MemoryRouter initialEntries={initialEntries ?? [route]}>
      <Routes>
        <Route
          path="/"
          element={
            <>
              <DashboardPage />
              <LocationProbe />
              <NavControls />
            </>
          }
        />
        <Route path="/other" element={<LocationProbe />} />
      </Routes>
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

describe('DashboardPage test-name filter', () => {
  afterEach(() => {
    apiGet.mockReset()
  })

  it('filters by test_name and writes a non-default mode beside it', async () => {
    mockApi(5)
    renderPage('/?test_name=checkout')

    await waitFor(() => expect(requests.at(-1)?.get('test_name')).toBe('checkout'))
    // Absent mode is the default one: the clean URL carries the name alone.
    expect(requests.at(-1)?.get('test_name_mode')).toBeNull()

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'ends with' }))

    await waitFor(() => expect(requests.at(-1)?.get('test_name_mode')).toBe('ends_with'))
    expect(requests.at(-1)?.get('test_name')).toBe('checkout')
  })

  it('writes no test_name_mode while there is no name', async () => {
    mockApi(5)
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'exact' }))

    await waitFor(() => expect(requests.length).toBeGreaterThan(0))
    // A mode with nothing to match is not a filter: it stays out of the URL.
    expect(requests.at(-1)?.get('test_name_mode')).toBeNull()
    expect(requests.at(-1)?.get('test_name')).toBeNull()
  })

  it('sends the name picked from the suggestions', async () => {
    mockApi(5)
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/dashboard/test-names')
        ? Promise.resolve({ test_names: [{ test_name: 'test_checkout_fails', failure_count: 9 }] })
        : fallback(path),
    )
    renderPage()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    await userEvent.click(screen.getByRole('combobox', { name: 'Select Test name' }))
    await userEvent.type(screen.getByRole('combobox', { name: 'Search Test name' }), 'checkout')
    const list = await screen.findByRole('listbox')
    await userEvent.click(await within(list).findByText('test_checkout_fails'))

    await waitFor(() => expect(requests.at(-1)?.get('test_name')).toBe('test_checkout_fails'))
  })

  it('returns to page 1 when the test-name filter changes', async () => {
    mockApi(25)
    renderPage('/?test_name=checkout')
    await waitFor(() => expect(requests.at(-1)?.get('offset')).toBe('0'))

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(requests.at(-1)?.get('offset')).toBe('20'))

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'exact' }))

    // A new filter on page 3 would skip the first matches: it starts over.
    await waitFor(() => expect(requests.at(-1)?.get('test_name_mode')).toBe('exact'))
    expect(requests.at(-1)?.get('offset')).toBe('0')
  })

  it('applies a legacy from/to link and keeps the dates when the test name changes', async () => {
    mockApi(5)
    renderPage('/?from=2024-01-01&to=2024-01-05&test_name=checkout')

    // The aliases are read, so the link opens the range it was saved with.
    await waitFor(() => expect(requests.at(-1)?.get('date_from')).toBe('2024-01-01'))
    expect(requests.at(-1)?.get('date_to')).toBe('2024-01-05')
    expect(requests.at(-1)?.get('test_name')).toBe('checkout')

    await userEvent.click(screen.getByRole('button', { name: 'Clear test name' }))

    // Writing the pair drops the aliases, and writes what they said: no dates lost.
    await waitFor(() => expect(requests.at(-1)?.get('test_name')).toBeNull())
    expect(requests.at(-1)?.get('date_from')).toBe('2024-01-01')
    expect(requests.at(-1)?.get('date_to')).toBe('2024-01-05')
  })
})

describe('DashboardPage search URL contract', () => {
  beforeEach(() => {
    resetLastAppliedPresetForTests()
  })

  afterEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiPut.mockReset()
  })

  it('seeds input and first API request from ?search= with zero unfiltered flash', async () => {
    mockApi(5)
    renderPage('/?search=kubevirt')

    expect(screen.getByPlaceholderText('Filter jobs...')).toHaveValue('kubevirt')
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))
    expect(requests[0]?.get('search')).toBe('kubevirt')
    expect(screen.getByTestId('location-probe').textContent).toBe('/?search=kubevirt')
  })

  it('debounces typing into URL using replace: true', async () => {
    mockApi(5)
    renderPage('/', ['/other', '/'])
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    const input = screen.getByPlaceholderText('Filter jobs...')
    await userEvent.type(input, 'kubevirt')

    await waitFor(() =>
      expect(screen.getByTestId('location-probe').textContent).toBe('/?search=kubevirt'),
    )
    await waitFor(() => expect(requests.at(-1)?.get('search')).toBe('kubevirt'))

    // replace: true means going back returns to /other instead of '/'
    await userEvent.click(screen.getByRole('button', { name: 'Go back' }))
    await waitFor(() => expect(screen.getByTestId('location-probe').textContent).toBe('/other'))
  })

  it('preserves search in URL when clicking status and label filters, without clobbering mid-debounce typing', async () => {
    mockApi(5)
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) =>
      path === '/api/jobs/metadata'
        ? Promise.resolve([{ job_id: 'job-1', labels: ['nightly'], team: 'storage' }])
        : fallback(path),
    )

    renderPage('/?search=kube')
    await waitFor(() => expect(requests[0]?.get('search')).toBe('kube'))
    const labelChip = await screen.findByRole('button', { name: /nightly/i })

    // Start typing mid-debounce, then click tag & status before 300ms elapses
    const input = screen.getByPlaceholderText('Filter jobs...')
    fireEvent.change(input, { target: { value: 'kubevirt' } })
    expect(input).toHaveValue('kubevirt')

    await userEvent.click(labelChip)
    await userEvent.click(screen.getByRole('button', { name: 'All statuses' }))
    await userEvent.click(await screen.findByText('failed'))
    await userEvent.keyboard('{Escape}')

    // Mid-debounce typing survived the other filter clicks
    expect(input).toHaveValue('kubevirt')

    await waitFor(() => {
      const sp = new URLSearchParams(screen.getByTestId('location-probe').textContent?.split('?')[1] ?? '')
      expect(sp.get('search')).toBe('kubevirt')
      expect(sp.getAll('status')).toEqual(['failed'])
      expect(sp.getAll('label')).toEqual(['nightly'])
    })
  })

  it('saves and restores search in filter presets and clears search when applying a preset without search', async () => {
    const presetWithoutSearch = {
      id: 1,
      name: 'Storage only',
      page: 'dashboard',
      scope: 'user',
      filters: { team: ['storage'] },
      created_at: '2026-01-01T00:00:00',
      updated_at: '2026-01-01T00:00:00',
    }
    const presetWithSearch = {
      id: 2,
      name: 'Kubevirt failed',
      page: 'dashboard',
      scope: 'user',
      filters: { search: 'kubevirt', status: ['failed'] },
      created_at: '2026-01-02T00:00:00',
      updated_at: '2026-01-02T00:00:00',
    }
    const savedPreset = {
      id: 3,
      name: 'Saved search',
      page: 'dashboard',
      scope: 'user',
      filters: { search: 'kubevirt', status: 'failed' },
      created_at: '2026-01-03T00:00:00',
      updated_at: '2026-01-03T00:00:00',
    }
    mockApi(5)
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/user/filter-presets')
        ? Promise.resolve([presetWithoutSearch, presetWithSearch])
        : fallback(path),
    )
    apiPost.mockResolvedValue(savedPreset)

    renderPage('/?search=kubevirt&status=failed')
    await waitFor(() => expect(requests[0]?.get('search')).toBe('kubevirt'))

    // Save preset containing search
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-save-as'))
    fireEvent.change(screen.getByTestId('filter-preset-save-name'), {
      target: { value: 'Saved search' },
    })
    await userEvent.click(screen.getByTestId('filter-preset-save-confirm'))

    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/user/filter-presets', {
        name: 'Saved search',
        page: 'dashboard',
        scope: 'user',
        filters: { search: 'kubevirt', status: 'failed' },
      }),
    )

    // Apply preset without search -> replaces all FILTER_PRESET_KEYS, clearing search
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-1'))

    await waitFor(() => expect(screen.getByPlaceholderText('Filter jobs...')).toHaveValue(''))
    expect(screen.getByTestId('location-probe').textContent).toBe('/?team=storage')
    await waitFor(() => {
      const last = requests.at(-1)
      expect(last?.get('search')).toBeNull()
      expect(last?.getAll('team')).toEqual(['storage'])
    })

    // Apply preset with search -> restores search in URL, input, and API query
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-2'))

    await waitFor(() =>
      expect(screen.getByPlaceholderText('Filter jobs...')).toHaveValue('kubevirt'),
    )
    const sp = new URLSearchParams(screen.getByTestId('location-probe').textContent?.split('?')[1] ?? '')
    expect(sp.get('search')).toBe('kubevirt')
    expect(sp.getAll('status')).toEqual(['failed'])
    await waitFor(() => {
      const last = requests.at(-1)
      expect(last?.get('search')).toBe('kubevirt')
      expect(last?.getAll('status')).toEqual(['failed'])
    })
  })

  it('syncs external URL changes into input and cancels any pending debounce timer', async () => {
    mockApi(5)
    renderPage('/?search=initial')
    await waitFor(() => expect(requests[0]?.get('search')).toBe('initial'))

    // Start typing (schedules 300ms debounce)
    const input = screen.getByPlaceholderText('Filter jobs...')
    fireEvent.change(input, { target: { value: 'initialtyping' } })
    expect(input).toHaveValue('initialtyping')

    // External URL change before debounce settles — single fetch with new search+team (no stale request)
    const countBeforeNav = requests.length
    await userEvent.click(screen.getByRole('button', { name: 'Nav external' }))

    await waitFor(() => expect(input).toHaveValue('external'))
    await waitFor(() => {
      const last = requests.at(-1)
      expect(last?.get('search')).toBe('external')
      expect(last?.getAll('team')).toEqual(['net'])
    })
    const navRequests = requests.slice(countBeforeNav)
    expect(navRequests).toHaveLength(1)
    expect(navRequests[0]?.get('search')).toBe('external')
    expect(navRequests[0]?.getAll('team')).toEqual(['net'])

    // Wait past the 300ms debounce window to ensure the cancelled timer never overwrites URL
    await new Promise((r) => setTimeout(r, 350))
    expect(input).toHaveValue('external')
    expect(screen.getByTestId('location-probe').textContent).toBe('/?search=external&team=net')

    // External navigation to clean URL clears search
    await userEvent.click(screen.getByRole('button', { name: 'Nav clean' }))
    await waitFor(() => expect(input).toHaveValue(''))
    await waitFor(() => expect(requests.at(-1)?.get('search')).toBeNull())
  })

  it('uses immediate typed search for preset modified-state and quick Update before debounce settles', async () => {
    const activePreset = {
      id: 2,
      name: 'Kubevirt failed',
      page: 'dashboard',
      scope: 'user',
      filters: { search: 'kubevirt', status: ['failed'] },
      created_at: '2026-01-02T00:00:00',
      updated_at: '2026-01-02T00:00:00',
    }
    mockApi(5)
    const fallback = apiGet.getMockImplementation()!
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/user/filter-presets')
        ? Promise.resolve([activePreset])
        : fallback(path),
    )
    apiPut.mockResolvedValue({
      ...activePreset,
      filters: { search: 'kubevirt-v2', status: 'failed' },
    })

    renderPage('/?search=kubevirt&status=failed')
    await waitFor(() => expect(requests[0]?.get('search')).toBe('kubevirt'))

    // Apply preset so it becomes active
    await userEvent.click(screen.getByTestId('filter-presets-trigger'))
    await userEvent.click(screen.getByTestId('filter-preset-2'))
    await waitFor(() =>
      expect(screen.getByTestId('filter-presets-trigger').textContent).toContain('Kubevirt failed'),
    )

    // Edit search and immediately click Update before 300ms debounce completes
    const input = screen.getByPlaceholderText('Filter jobs...')
    fireEvent.change(input, { target: { value: 'kubevirt-v2' } })

    const updateBtn = await screen.findByTestId('filter-preset-update')
    await userEvent.click(updateBtn)

    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith('/api/user/filter-presets/2', {
        filters: { search: 'kubevirt-v2', status: 'failed' },
      }),
    )
  })

  it('cancels pending search typing on sidebar preset navigation even when URL search stays empty', async () => {
    mockApi(5)
    renderPage('/')
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))

    // Start typing mid-debounce while URL search is empty
    const input = screen.getByPlaceholderText('Filter jobs...')
    fireEvent.change(input, { target: { value: 'draft' } })
    expect(input).toHaveValue('draft')

    // Click a sidebar preset that omits `search` (URL search remains empty)
    await userEvent.click(screen.getByRole('button', { name: 'Sidebar preset no search' }))

    await waitFor(() => expect(input).toHaveValue(''))
    expect(screen.getByTestId('location-probe').textContent).toBe('/?team=storage')

    // Wait past the 300ms debounce window — pending 'draft' must not overwrite the preset
    await new Promise((r) => setTimeout(r, 350))
    expect(input).toHaveValue('')
    expect(screen.getByTestId('location-probe').textContent).toBe('/?team=storage')
    expect(requests.at(-1)?.get('search')).toBeNull()
    expect(requests.at(-1)?.getAll('team')).toEqual(['storage'])
  })
})
