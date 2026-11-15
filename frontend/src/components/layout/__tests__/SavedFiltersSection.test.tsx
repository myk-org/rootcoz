import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SavedFiltersSection } from '../SavedFiltersSection'
import {
  getLastAppliedPreset,
  notifyPresetApplied,
  PRESET_APPLIED_EVENT,
  PRESETS_CHANGED_EVENT,
  resetLastAppliedPresetForTests,
  type FilterPreset,
  type FilterPresetPage,
} from '@/lib/filterPresets'
import { DISCLOSURES, useSidebarSavedFilters, type Disclosure } from '@/lib/useSidebarSavedFilters'

// ─── Mocks ──────────────────────────────────────────────────────────

const mockAuth = {
  canViewReports: true,
  isAdmin: false,
  username: 'alice',
  authenticated: true,
  loading: false,
  role: 'reviewer',
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

const apiGet = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
  },
}))

// ─── Test Harness & Probes ──────────────────────────────────────────

function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="location-probe">
      {location.pathname}
      {location.search}
    </div>
  )
}

function Harness({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean
  onNavigate?: () => void
}) {
  const { loading, authenticated, canViewReports, isAdmin } = mockAuth
  const { groups, expanded, toggle } = useSidebarSavedFilters()
  const showReports = !loading && authenticated && (canViewReports || isAdmin)

  return (
    <nav aria-label="Test sidebar">
      <SavedFiltersSection
        page="dashboard"
        navLink={<Link to="/">Dashboard</Link>}
        groups={groups.dashboard}
        expanded={expanded}
        onToggle={toggle}
        collapsed={collapsed}
        onNavigate={onNavigate}
      />
      <Link to="/history">History</Link>
      {showReports && (
        <SavedFiltersSection
          page="reports"
          navLink={<Link to="/reports">Reports</Link>}
          groups={groups.reports}
          expanded={expanded}
          onToggle={toggle}
          collapsed={collapsed}
          onNavigate={onNavigate}
        />
      )}
    </nav>
  )
}

function renderSection(
  collapsed = false,
  onNavigate?: () => void,
  initialEntry = '/history',
) {
  const tree = (isCollapsed: boolean) => (
    <TooltipProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <Harness collapsed={isCollapsed} onNavigate={onNavigate} />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </TooltipProvider>
  )

  const utils = render(tree(collapsed))
  return {
    ...utils,
    rerenderHarness: (nextCollapsed = collapsed) => utils.rerender(tree(nextCollapsed)),
  }
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

// ─── Fixtures ───────────────────────────────────────────────────────

const PERSONAL: FilterPreset = {
  id: 1,
  name: 'Storage unreviewed',
  page: 'dashboard',
  scope: 'user',
  filters: { team: ['storage'], review_status: 'unreviewed' },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
}

const SERVER_DASH: FilterPreset = {
  id: 2,
  name: 'Team view',
  page: 'dashboard',
  scope: 'server',
  filters: { tier: ['2'], status: ['failed', 'aborted'] },
  created_at: '2026-01-02T00:00:00',
  updated_at: '2026-01-02T00:00:00',
}

const SERVER_REPORTS: FilterPreset = {
  id: 3,
  name: 'Upgrade reports',
  page: 'reports',
  scope: 'server',
  filters: { from: '2026-01-01', to: '2026-02-01' },
  created_at: '2026-01-03T00:00:00',
  updated_at: '2026-01-03T00:00:00',
}

const USER_REPORTS: FilterPreset = {
  id: 4,
  name: 'Weekly digest',
  page: 'reports',
  scope: 'user',
  filters: { team: ['compute'], status: ['completed'] },
  created_at: '2026-01-04T00:00:00',
  updated_at: '2026-01-04T00:00:00',
}

const DISCLOSURE_BUTTON_NAMES: Record<Disclosure, string> = {
  Dashboard: 'Dashboard saved filters',
  DashboardServer: 'Dashboard Server saved filters',
  DashboardUser: 'Dashboard User saved filters',
  Reports: 'Reports saved filters',
  ReportsServer: 'Reports Server saved filters',
  ReportsUser: 'Reports User saved filters',
}

function mockAllFourPresets() {
  apiGet.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes('page=dashboard')
        ? [PERSONAL, SERVER_DASH]
        : [USER_REPORTS, SERVER_REPORTS],
    ),
  )
}

// ─── Tests ──────────────────────────────────────────────────────────

describe('SavedFiltersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockAuth.canViewReports = true
    mockAuth.isAdmin = false
    mockAuth.username = 'alice'
    mockAuth.authenticated = true
    mockAuth.loading = false
    mockAuth.role = 'reviewer'
    resetLastAppliedPresetForTests()
  })

  it('renders only page links without disclosure chevrons before presets load', () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const { container } = renderSection()
    expect(container.querySelector('[data-testid="sidebar-saved-filters"]')).toBeNull()
    expect(screen.queryByTestId('sidebar-preset-group-dashboard')).toBeNull()
    expect(screen.queryByTestId('sidebar-preset-group-reports')).toBeNull()
    expect(screen.queryByRole('button', { name: /saved filters/i })).toBeNull()
    expect(screen.getByRole('link', { name: 'Dashboard' })).toBeDefined()
    expect(screen.getByRole('link', { name: 'Reports' })).toBeDefined()
  })

  it('renders working page links without disclosure controls when the user has no presets', async () => {
    apiGet.mockResolvedValue([])
    const { container } = renderSection()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(container.querySelector('[data-testid="sidebar-saved-filters"]')).toBeNull()
    expect(screen.queryByTestId('sidebar-preset-group-dashboard')).toBeNull()
    expect(screen.queryByTestId('sidebar-preset-group-reports')).toBeNull()
    expect(screen.queryByRole('button', { name: /saved filters/i })).toBeNull()
    fireEvent.click(screen.getByRole('link', { name: 'Dashboard' }))
    expect(screen.getByTestId('location-probe').textContent).toBe('/')
  })

  it('places all four fixtures into their exact page and scope with Server preceding User and no legacy headings', async () => {
    mockAllFourPresets()
    renderSection()
    await screen.findByText('Weekly digest')

    expect(screen.queryByText('Saved filters')).toBeNull()
    expect(screen.queryByText('Yours')).toBeNull()
    expect(screen.getAllByText('Reports')).toHaveLength(1)

    const dashboard = screen.getByTestId('sidebar-preset-group-dashboard')
    const dashServer = within(dashboard).getByRole('button', { name: 'Dashboard Server saved filters' })
    const dashUser = within(dashboard).getByRole('button', { name: 'Dashboard User saved filters' })
    const dashServerItem = within(dashboard).getByTestId('sidebar-filter-preset-2')
    const dashUserItem = within(dashboard).getByTestId('sidebar-filter-preset-1')
    expect(within(dashboard).getByRole('link', { name: 'Dashboard' })).toBeDefined()
    expect(within(dashboard).queryByText('Upgrade reports')).toBeNull()
    expect(within(dashboard).queryByText('Weekly digest')).toBeNull()
    expect(
      dashServer.compareDocumentPosition(dashServerItem) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      dashServerItem.compareDocumentPosition(dashUser) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      dashUser.compareDocumentPosition(dashUserItem) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    const reports = screen.getByTestId('sidebar-preset-group-reports')
    const repServer = within(reports).getByRole('button', { name: 'Reports Server saved filters' })
    const repUser = within(reports).getByRole('button', { name: 'Reports User saved filters' })
    const repServerItem = within(reports).getByTestId('sidebar-filter-preset-3')
    const repUserItem = within(reports).getByTestId('sidebar-filter-preset-4')
    expect(within(reports).getByRole('link', { name: 'Reports' })).toBeDefined()
    expect(within(reports).queryByText('Team view')).toBeNull()
    expect(within(reports).queryByText('Storage unreviewed')).toBeNull()
    expect(
      repServer.compareDocumentPosition(repServerItem) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      repServerItem.compareDocumentPosition(repUser) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      repUser.compareDocumentPosition(repUserItem) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it.each([
    {
      label: 'empty',
      dash: [] as FilterPreset[],
      rep: [] as FilterPreset[],
      dashScopes: [] as string[],
      repScopes: [] as string[],
    },
    {
      label: 'Server only on Dashboard, User only on Reports',
      dash: [SERVER_DASH],
      rep: [USER_REPORTS],
      dashScopes: ['Dashboard Server saved filters'],
      repScopes: ['Reports User saved filters'],
    },
    {
      label: 'User only on Dashboard, Server only on Reports',
      dash: [PERSONAL],
      rep: [SERVER_REPORTS],
      dashScopes: ['Dashboard User saved filters'],
      repScopes: ['Reports Server saved filters'],
    },
    {
      label: 'both scopes on both pages',
      dash: [PERSONAL, SERVER_DASH],
      rep: [USER_REPORTS, SERVER_REPORTS],
      dashScopes: ['Dashboard Server saved filters', 'Dashboard User saved filters'],
      repScopes: ['Reports Server saved filters', 'Reports User saved filters'],
    },
  ])('renders only nonempty scope headers/toggles/lists ($label)', async ({ dash, rep, dashScopes, repScopes }) => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? dash : rep),
    )
    renderSection()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))

    expect(screen.getByRole('link', { name: 'Dashboard' })).toBeDefined()
    expect(screen.getByRole('link', { name: 'Reports' })).toBeDefined()

    if (dashScopes.length === 0) {
      expect(screen.queryByRole('button', { name: 'Dashboard saved filters' })).toBeNull()
      expect(screen.queryByTestId('sidebar-preset-group-dashboard')).toBeNull()
    } else {
      expect(screen.getByRole('button', { name: 'Dashboard saved filters' })).toBeDefined()
      for (const name of ['Dashboard Server saved filters', 'Dashboard User saved filters']) {
        if (dashScopes.includes(name)) {
          expect(screen.getByRole('button', { name })).toBeDefined()
        } else {
          expect(screen.queryByRole('button', { name })).toBeNull()
        }
      }
    }

    if (repScopes.length === 0) {
      expect(screen.queryByRole('button', { name: 'Reports saved filters' })).toBeNull()
      expect(screen.queryByTestId('sidebar-preset-group-reports')).toBeNull()
    } else {
      expect(screen.getByRole('button', { name: 'Reports saved filters' })).toBeDefined()
      for (const name of ['Reports Server saved filters', 'Reports User saved filters']) {
        if (repScopes.includes(name)) {
          expect(screen.getByRole('button', { name })).toBeDefined()
        } else {
          expect(screen.queryByRole('button', { name })).toBeNull()
        }
      }
    }
  })

  it.each(DISCLOSURES)(
    'tracks %s independently, persists under encoded user key, and restores across remount',
    async (choice) => {
      mockAuth.username = 'alice/dev@example.com'
      mockAllFourPresets()

      const first = renderSection()
      await screen.findByText('Weekly digest')

      // All 6 start expanded
      for (const key of DISCLOSURES) {
        expect(
          screen.getByRole('button', { name: DISCLOSURE_BUTTON_NAMES[key] }).getAttribute('aria-expanded'),
        ).toBe('true')
      }

      // Toggle target choice
      fireEvent.click(screen.getByRole('button', { name: DISCLOSURE_BUTTON_NAMES[choice] }))
      expect(
        screen.getByRole('button', { name: DISCLOSURE_BUTTON_NAMES[choice] }).getAttribute('aria-expanded'),
      ).toBe('false')
      expect(
        localStorage.getItem(`rootcoz_sidebar_filters:alice%2Fdev%40example.com:${choice}`),
      ).toBe('false')

      // Other 5 keys remain untouched in storage
      for (const other of DISCLOSURES) {
        if (other !== choice) {
          expect(
            localStorage.getItem(`rootcoz_sidebar_filters:alice%2Fdev%40example.com:${other}`),
          ).toBeNull()
        }
      }

      first.unmount()

      // Remount restores exact state
      renderSection()
      await screen.findByText('Weekly digest')
      expect(
        screen.getByRole('button', { name: DISCLOSURE_BUTTON_NAMES[choice] }).getAttribute('aria-expanded'),
      ).toBe('false')

      for (const other of DISCLOSURES) {
        if (other === choice) continue
        // Child buttons of a collapsed page are hidden until parent reopens; top-level & sibling buttons are visible
        const maybeVisible = screen.queryByRole('button', { name: DISCLOSURE_BUTTON_NAMES[other] })
        if (maybeVisible) {
          expect(maybeVisible.getAttribute('aria-expanded')).toBe('true')
        }
      }
    },
  )

  it('preserves child collapsed choices when parent closes and reopens, and never auto-expands on active preset', async () => {
    mockAllFourPresets()
    renderSection(false, undefined, '/?team=storage&review_status=unreviewed')
    await screen.findByText('Weekly digest')

    // Collapse DashboardUser and ReportsServer, then collapse both parents
    fireEvent.click(screen.getByRole('button', { name: 'Dashboard User saved filters' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reports Server saved filters' }))
    fireEvent.click(screen.getByRole('button', { name: 'Dashboard saved filters' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reports saved filters' }))

    // Simulate applying / restoring active presets while collapsed
    act(() => {
      notifyPresetApplied(PERSONAL.id, 'dashboard')
      notifyPresetApplied(SERVER_REPORTS.id, 'reports')
    })

    expect(screen.getByRole('button', { name: 'Dashboard saved filters' }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByRole('button', { name: 'Reports saved filters' }).getAttribute('aria-expanded')).toBe('false')

    // Reopen parents -> child false choices are preserved
    fireEvent.click(screen.getByRole('button', { name: 'Dashboard saved filters' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reports saved filters' }))
    expect(
      screen.getByRole('button', { name: 'Dashboard User saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(
      screen.getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('true')
    expect(
      screen.getByRole('button', { name: 'Reports Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(
      screen.getByRole('button', { name: 'Reports User saved filters' }).getAttribute('aria-expanded'),
    ).toBe('true')
  })

  it('collapses only on literal "false" and falls back expanded for absent, "true", and malformed values', async () => {
    mockAllFourPresets()
    for (const value of [null, 'true', 'FALSE', '0', '{"expanded":false}', 'invalid', '']) {
      localStorage.clear()
      if (value !== null) {
        for (const choice of DISCLOSURES) {
          localStorage.setItem(`rootcoz_sidebar_filters:alice:${choice}`, value)
        }
      }
      const { unmount } = renderSection()
      await screen.findByText('Weekly digest')
      for (const choice of DISCLOSURES) {
        expect(
          screen.getByRole('button', { name: DISCLOSURE_BUTTON_NAMES[choice] }).getAttribute('aria-expanded'),
        ).toBe('true')
      }
      unmount()
    }

    // Literal 'false' collapses
    localStorage.setItem('rootcoz_sidebar_filters:alice:DashboardServer', 'false')
    renderSection()
    await screen.findByText('Weekly digest')
    expect(
      screen.getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
  })

  it('tolerates throwing getItem, setItem, and window.localStorage getter with expanded fallback and in-memory toggling', async () => {
    mockAllFourPresets()

    // 1. Throwing getItem
    const getSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('read blocked')
    })
    try {
      const view = renderSection()
      await screen.findByText('Weekly digest')
      const trigger = screen.getByRole('button', { name: 'Dashboard saved filters' })
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(screen.getByTestId('sidebar-filter-preset-1'))
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/?team=storage&review_status=unreviewed',
      )
      view.unmount()
    } finally {
      getSpy.mockRestore()
    }

    // 2. Throwing setItem -> repeated open/close still works in memory
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    try {
      const view = renderSection()
      await screen.findByText('Weekly digest')
      const trigger = screen.getByRole('button', { name: 'Dashboard saved filters' })
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(trigger)
      expect(trigger.getAttribute('aria-expanded')).toBe('false')
      fireEvent.click(trigger)
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(screen.getByTestId('sidebar-filter-preset-3'))
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/reports?from=2026-01-01&to=2026-02-01',
      )
      view.unmount()
    } finally {
      setSpy.mockRestore()
    }

    // 3. Throwing window.localStorage getter
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage')
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('Blocked storage', 'SecurityError')
      },
    })
    try {
      const view = renderSection()
      await screen.findByText('Weekly digest')
      const trigger = screen.getByRole('button', { name: 'Reports saved filters' })
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(trigger)
      expect(trigger.getAttribute('aria-expanded')).toBe('false')
      fireEvent.click(trigger)
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(screen.getByTestId('sidebar-filter-preset-4'))
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/reports?team=compute&status=completed',
      )
      view.unmount()
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'localStorage', originalDescriptor)
      }
    }
  })

  it('isolates identities synchronously across loading, logout, A -> B, and A -> B -> A races', async () => {
    const aliceFirstDash = createDeferred<FilterPreset[]>()
    const bobDash = createDeferred<FilterPreset[]>()
    const aliceSecondDash = createDeferred<FilterPreset[]>()

    const ALICE_STALE: FilterPreset = { ...PERSONAL, id: 101, name: 'Alice stale preset' }
    const ALICE_FRESH: FilterPreset = { ...PERSONAL, id: 102, name: 'Alice fresh preset' }
    const BOB_PRESET: FilterPreset = { ...PERSONAL, id: 201, name: 'Bob preset' }

    localStorage.setItem('rootcoz_sidebar_filters:alice:DashboardServer', 'false')
    localStorage.setItem('rootcoz_sidebar_filters:bob:DashboardUser', 'false')

    let aliceLoadCount = 0
    apiGet.mockImplementation((path: string) => {
      if (!path.includes('page=dashboard')) return Promise.resolve([SERVER_REPORTS])
      if (mockAuth.username === 'alice') {
        aliceLoadCount += 1
        return aliceLoadCount === 1 ? aliceFirstDash.promise : aliceSecondDash.promise
      }
      return bobDash.promise
    })

    const { rerenderHarness } = renderSection()
    expect(apiGet).toHaveBeenCalledTimes(2)

    // Switch Alice -> Bob BEFORE resolving Alice's first fetch
    mockAuth.username = 'bob'
    rerenderHarness()

    // No Alice writes leaked under Bob's keys
    expect(localStorage.getItem('rootcoz_sidebar_filters:bob:DashboardServer')).toBeNull()
    expect(localStorage.getItem('rootcoz_sidebar_filters:bob:DashboardUser')).toBe('false')

    // Resolve Bob's presets -> Bob sees his presets and his preferences
    await act(async () => {
      bobDash.resolve([BOB_PRESET, SERVER_DASH])
    })
    expect(await screen.findByText('Bob preset')).toBeDefined()
    expect(
      screen.getByRole('button', { name: 'Dashboard User saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(
      screen.getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('true')

    // Rerender to loading -> Bob's presets synchronously disappear and zero new requests fire
    const callsBeforeLoading = apiGet.mock.calls.length
    mockAuth.loading = true
    rerenderHarness()
    expect(screen.queryByText('Bob preset')).toBeNull()
    expect(apiGet.mock.calls.length).toBe(callsBeforeLoading)

    // Rerender to logged out -> still empty and zero new requests fire
    mockAuth.loading = false
    mockAuth.authenticated = false
    mockAuth.username = ''
    rerenderHarness()
    expect(screen.queryByText('Bob preset')).toBeNull()
    expect(apiGet.mock.calls.length).toBe(callsBeforeLoading)

    // Return to Alice (A -> B -> A): second Alice request starts; resolve second Alice BEFORE first Alice
    mockAuth.authenticated = true
    mockAuth.username = 'alice'
    rerenderHarness()

    await act(async () => {
      aliceSecondDash.resolve([ALICE_FRESH, SERVER_DASH])
    })
    expect(await screen.findByText('Alice fresh preset')).toBeDefined()
    expect(
      screen.getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')

    // Now resolve the very first Alice promise last -> stale A1 data is ignored
    await act(async () => {
      aliceFirstDash.resolve([ALICE_STALE])
    })
    expect(screen.getByText('Alice fresh preset')).toBeDefined()
    expect(screen.queryByText('Alice stale preset')).toBeNull()
  })

  it('commits only the newest PRESETS_CHANGED_EVENT refresh when responses finish out of order', async () => {
    apiGet.mockResolvedValue([])
    renderSection()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))

    const refreshOne = createDeferred<FilterPreset[]>()
    const refreshTwo = createDeferred<FilterPreset[]>()
    let call = 0
    apiGet.mockImplementation((path: string) => {
      if (!path.includes('page=dashboard')) return Promise.resolve([])
      call += 1
      return call === 1 ? refreshOne.promise : refreshTwo.promise
    })

    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })

    await act(async () => {
      refreshTwo.resolve([{ ...PERSONAL, id: 302, name: 'Newest preset' }])
    })
    expect(await screen.findByText('Newest preset')).toBeDefined()

    await act(async () => {
      refreshOne.resolve([{ ...PERSONAL, id: 301, name: 'Older preset' }])
    })
    expect(screen.getByText('Newest preset')).toBeDefined()
    expect(screen.queryByText('Older preset')).toBeNull()
  })

  it('enforces Reports effective grant (canViewReports || isAdmin), masks immediately on revoke, and preserves preferences on restore', async () => {
    // 1. Forbidden non-admin -> no Reports link/request/presets
    mockAuth.canViewReports = false
    mockAuth.isAdmin = false
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : [SERVER_REPORTS]),
    )
    const { rerenderHarness } = renderSection()
    expect(await screen.findByText('Storage unreviewed')).toBeDefined()
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(apiGet.mock.calls.every((call) => String(call[0]).includes('page=dashboard'))).toBe(true)
    expect(screen.queryByRole('link', { name: 'Reports' })).toBeNull()
    expect(screen.queryByText('Upgrade reports')).toBeNull()

    // 2. Admin-only grant (canViewReports false, isAdmin true) -> fetches and shows Reports
    mockAuth.isAdmin = true
    rerenderHarness()
    expect(await screen.findByText('Upgrade reports')).toBeDefined()
    expect(screen.getByRole('link', { name: 'Reports' })).toBeDefined()

    // Collapse ReportsServer preference
    fireEvent.click(screen.getByRole('button', { name: 'Reports Server saved filters' }))
    expect(localStorage.getItem('rootcoz_sidebar_filters:alice:ReportsServer')).toBe('false')

    // 3. Revoke while a refresh is in flight -> Reports masked immediately and late response ignored
    const pendingReports = createDeferred<FilterPreset[]>()
    apiGet.mockImplementation((path: string) =>
      path.includes('page=dashboard') ? Promise.resolve([PERSONAL]) : pendingReports.promise,
    )
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })

    mockAuth.isAdmin = false
    mockAuth.canViewReports = false
    rerenderHarness()
    expect(screen.queryByRole('link', { name: 'Reports' })).toBeNull()
    expect(screen.queryByText('Upgrade reports')).toBeNull()

    await act(async () => {
      pendingReports.resolve([{ ...SERVER_REPORTS, id: 99, name: 'Late forbidden report' }])
    })
    expect(screen.queryByText('Late forbidden report')).toBeNull()

    // 4. Restore grant -> refetches Reports and retains the saved false ReportsServer preference
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : [SERVER_REPORTS, USER_REPORTS]),
    )
    mockAuth.canViewReports = true
    rerenderHarness()
    expect(await screen.findByText('Weekly digest')).toBeDefined()
    expect(
      screen.getByRole('button', { name: 'Reports Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
  })

  it('keeps presets from the succeeding page when another rejects or returns malformed data, and clears stale failed pages', async () => {
    // Initial load: both succeed
    mockAllFourPresets()
    renderSection()
    await screen.findByText('Storage unreviewed')
    expect(screen.getByText('Upgrade reports')).toBeDefined()

    // Refresh 1: dashboard rejects, reports returns malformed non-array -> both become empty (not stale!)
    apiGet.mockImplementation((path: string) =>
      path.includes('page=dashboard')
        ? Promise.reject(new Error('boom'))
        : Promise.resolve({ malformed: true }),
    )
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })
    await waitFor(() => {
      expect(screen.queryByText('Storage unreviewed')).toBeNull()
      expect(screen.queryByText('Upgrade reports')).toBeNull()
    })

    // Refresh 2: dashboard rejects, reports succeeds -> reports renders
    apiGet.mockImplementation((path: string) =>
      path.includes('page=dashboard')
        ? Promise.reject(new Error('boom'))
        : Promise.resolve([SERVER_REPORTS]),
    )
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })
    expect(await screen.findByText('Upgrade reports')).toBeDefined()
    expect(screen.queryByText('Storage unreviewed')).toBeNull()
  })

  it('applies all four shortcuts with exact URLs and notifies active-preset listeners before navigation and onNavigate', async () => {
    mockAllFourPresets()
    const events: string[] = []
    const appliedDetails: Array<{ id: number; page: FilterPresetPage }> = []
    const onApplied = (e: Event) => {
      const detail = (e as CustomEvent<{ id: number; page: FilterPresetPage }>).detail
      appliedDetails.push(detail)
      events.push(`notify:${detail.id}`)
    }
    window.addEventListener(PRESET_APPLIED_EVENT, onApplied)

    try {
      const onNavigate = vi.fn(() => {
        events.push('callback')
      })
      renderSection(false, onNavigate, '/history')
      await screen.findByText('Weekly digest')

      const cases: Array<{ preset: FilterPreset; expectedUrl: string }> = [
        { preset: SERVER_DASH, expectedUrl: '/?tier=2&status=failed&status=aborted' },
        { preset: PERSONAL, expectedUrl: '/?team=storage&review_status=unreviewed' },
        { preset: SERVER_REPORTS, expectedUrl: '/reports?from=2026-01-01&to=2026-02-01' },
        { preset: USER_REPORTS, expectedUrl: '/reports?team=compute&status=completed' },
      ]

      let previousUrl = '/history'
      for (const { preset, expectedUrl } of cases) {
        events.length = 0
        onNavigate.mockClear()

        let routeAtNotify = ''
        let callbackCalledBeforeNotify = false
        const checkOrder = () => {
          routeAtNotify = screen.getByTestId('location-probe').textContent ?? ''
          callbackCalledBeforeNotify = onNavigate.mock.calls.length > 0
        }
        window.addEventListener(PRESET_APPLIED_EVENT, checkOrder, { once: true })

        fireEvent.click(screen.getByTestId(`sidebar-filter-preset-${preset.id}`))

        expect(routeAtNotify).toBe(previousUrl)
        expect(callbackCalledBeforeNotify).toBe(false)
        expect(screen.getByTestId('location-probe').textContent).toBe(expectedUrl)
        expect(getLastAppliedPreset()).toEqual({ id: preset.id, page: preset.page })
        expect(onNavigate).toHaveBeenCalledTimes(1)
        expect(events).toEqual([`notify:${preset.id}`, 'callback'])
        previousUrl = expectedUrl
      }
      expect(appliedDetails).toHaveLength(4)
    } finally {
      window.removeEventListener(PRESET_APPLIED_EVENT, onApplied)
    }
  })

  it('handles same-path query preset application on / and /reports without altering disclosure preferences', async () => {
    mockAllFourPresets()
    localStorage.setItem('rootcoz_sidebar_filters:alice:DashboardUser', 'false')
    localStorage.setItem('rootcoz_sidebar_filters:alice:ReportsUser', 'false')

    const onNavigate = vi.fn()
    const applied: Array<{ id: number; page: FilterPresetPage }> = []
    const listener = (e: Event) =>
      applied.push((e as CustomEvent<{ id: number; page: FilterPresetPage }>).detail)
    window.addEventListener(PRESET_APPLIED_EVENT, listener)

    try {
      // Start on /?tier=1 (same pathname '/')
      const first = renderSection(false, onNavigate, '/?tier=1')
      await screen.findByText('Team view')

      fireEvent.click(screen.getByTestId('sidebar-filter-preset-2'))
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/?tier=2&status=failed&status=aborted',
      )
      expect(onNavigate).toHaveBeenCalledTimes(1)
      expect(applied.at(-1)).toEqual({ id: 2, page: 'dashboard' })
      expect(localStorage.getItem('rootcoz_sidebar_filters:alice:DashboardUser')).toBe('false')
      first.unmount()

      // Start on /reports?from=2025-01-01 (same pathname '/reports')
      onNavigate.mockClear()
      renderSection(false, onNavigate, '/reports?from=2025-01-01')
      await screen.findByText('Upgrade reports')

      fireEvent.click(screen.getByTestId('sidebar-filter-preset-3'))
      expect(screen.getByTestId('location-probe').textContent).toBe(
        '/reports?from=2026-01-01&to=2026-02-01',
      )
      expect(onNavigate).toHaveBeenCalledTimes(1)
      expect(applied.at(-1)).toEqual({ id: 3, page: 'reports' })
      expect(localStorage.getItem('rootcoz_sidebar_filters:alice:ReportsUser')).toBe('false')
    } finally {
      window.removeEventListener(PRESET_APPLIED_EVENT, listener)
    }
  })

  it('reflects create, update, rename, and delete mutations without duplicates or stale controls', async () => {
    // Start empty
    apiGet.mockResolvedValue([])
    renderSection()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('sidebar-preset-group-dashboard')).toBeNull()

    // Create PERSONAL
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : []),
    )
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })
    expect(await screen.findByText('Storage unreviewed')).toBeDefined()
    expect(screen.getAllByTestId('sidebar-filter-preset-1')).toHaveLength(1)

    // Update filters + rename PERSONAL
    const renamed: FilterPreset = {
      ...PERSONAL,
      name: 'Storage blockers',
      filters: { team: ['storage'], status: ['failed'] },
    }
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [renamed] : []),
    )
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })
    expect(await screen.findByText('Storage blockers')).toBeDefined()
    expect(screen.queryByText('Storage unreviewed')).toBeNull()
    expect(screen.getAllByTestId('sidebar-filter-preset-1')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('sidebar-filter-preset-1'))
    expect(screen.getByTestId('location-probe').textContent).toBe('/?team=storage&status=failed')

    // Delete last preset -> page chevron and group wrapper disappear cleanly
    apiGet.mockResolvedValue([])
    act(() => {
      window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
    })
    await waitFor(() => {
      expect(screen.queryByText('Storage blockers')).toBeNull()
      expect(screen.queryByTestId('sidebar-preset-group-dashboard')).toBeNull()
      expect(screen.queryByRole('button', { name: 'Dashboard saved filters' })).toBeNull()
    })
  })

  it('renders collapsed icon-only presentation with tooltips and without group titles or chevrons', async () => {
    const user = userEvent.setup()
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(path.includes('page=dashboard') ? [PERSONAL] : []),
    )
    renderSection(true)
    const button = await screen.findByRole('button', { name: 'Storage unreviewed' })
    expect(screen.queryByText('Saved filters')).toBeNull()
    expect(screen.queryByText('User')).toBeNull()
    expect(screen.queryByText('Storage unreviewed')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Dashboard saved filters' })).toBeNull()

    await user.hover(button)
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('Storage unreviewed')
    })
  })
})
