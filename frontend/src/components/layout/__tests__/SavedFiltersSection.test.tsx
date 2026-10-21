import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SavedFiltersSection } from '../SavedFiltersSection'

// ─── Mocks ──────────────────────────────────────────────────────────

const apiGet = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
  },
}))

// Displays the current location so tests can assert navigation targets.
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>
}

function renderSection(collapsed = false) {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={['/history']}>
        <Routes>
          <Route
            path="/history"
            element={
              <>
                <SavedFiltersSection collapsed={collapsed} />
                <LocationProbe />
              </>
            }
          />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </TooltipProvider>,
  )
}

// ─── Fixtures ───────────────────────────────────────────────────────

const PERSONAL = {
  id: 1,
  name: 'Storage unreviewed',
  page: 'dashboard',
  scope: 'user' as const,
  filters: { team: ['storage'], review_status: 'unreviewed' },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
}

const SERVER_DASH = {
  id: 2,
  name: 'Team view',
  page: 'dashboard',
  scope: 'server' as const,
  filters: { tier: ['2'], status: ['failed', 'aborted'] },
  created_at: '2026-01-02T00:00:00',
  updated_at: '2026-01-02T00:00:00',
}

const SERVER_REPORTS = {
  id: 3,
  name: 'Upgrade reports',
  page: 'reports',
  scope: 'server' as const,
  filters: { from: '2026-01-01', to: '2026-02-01' },
  created_at: '2026-01-03T00:00:00',
  updated_at: '2026-01-03T00:00:00',
}

// ─── Tests ──────────────────────────────────────────────────────────

describe('SavedFiltersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders nothing before presets load or when the list is empty', () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const { container } = renderSection()
    expect(container.querySelector('[data-testid="sidebar-saved-filters"]')).toBeNull()
  })

  it('renders nothing when the user has no presets', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [] : []),
    )
    const { container } = renderSection()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(container.querySelector('[data-testid="sidebar-saved-filters"]')).toBeNull()
  })

  it('groups Yours and Server presets from both pages', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes('page=dashboard') ? [PERSONAL, SERVER_DASH] : [SERVER_REPORTS],
      ),
    )
    renderSection()
    expect(await screen.findByText('Saved filters')).toBeDefined()
    expect(screen.getByText('Yours')).toBeDefined()
    expect(screen.getByText('Server')).toBeDefined()
    expect(screen.getByText('Storage unreviewed')).toBeDefined()
    expect(screen.getByText('Team view')).toBeDefined()
    expect(screen.getByText('Upgrade reports')).toBeDefined()
  })

  it('navigates to the preset page with its filters applied', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : []),
    )
    renderSection()
    fireEvent.click(await screen.findByTestId('sidebar-filter-preset-1'))
    const probe = await screen.findByTestId('location-probe')
    expect(probe.textContent).toBe(
      '/?team=storage&review_status=unreviewed',
    )
  })

  it('navigates to reports presets with the reports route', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [] : [SERVER_REPORTS]),
    )
    renderSection()
    fireEvent.click(await screen.findByTestId('sidebar-filter-preset-3'))
    const probe = await screen.findByTestId('location-probe')
    expect(probe.textContent).toBe('/reports?from=2026-01-01&to=2026-02-01')
  })

  it('renders collapsed without group titles', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : []),
    )
    renderSection(true)
    // Names hidden when collapsed; tooltips carry them instead
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Saved filters')).toBeNull()
    expect(screen.queryByText('Storage unreviewed')).toBeNull()
  })
})
