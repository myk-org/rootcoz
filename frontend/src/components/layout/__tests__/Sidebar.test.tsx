import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { Sidebar } from '../Sidebar'
import { resetLastAppliedPresetForTests, type FilterPreset } from '@/lib/filterPresets'

// Mock useAuth to control role/admin state
const mockAuth = {
  username: 'testuser',
  isAdmin: false,
  isOperator: false,
  canViewReports: false,
  role: 'reviewer',
  loading: false,
  authenticated: true,
  login: vi.fn(),
  logout: vi.fn(),
  refreshAuth: vi.fn(),
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

import { api } from '@/lib/api'

const mockApiGet = api.get as unknown as ReturnType<typeof vi.fn>

const DASH_SERVER: FilterPreset = {
  id: 11,
  name: 'mtv-2.11',
  page: 'dashboard',
  scope: 'server',
  filters: { version: ['2.11'] },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
}

const DASH_USER: FilterPreset = {
  id: 12,
  name: 'My recent failures',
  page: 'dashboard',
  scope: 'user',
  filters: { status: ['failed'], submitted_by: 'testuser' },
  created_at: '2026-01-02T00:00:00',
  updated_at: '2026-01-02T00:00:00',
}

const REPORTS_SERVER: FilterPreset = {
  id: 21,
  name: 'cnv-5.0',
  page: 'reports',
  scope: 'server',
  filters: { version: ['5.0'] },
  created_at: '2026-01-03T00:00:00',
  updated_at: '2026-01-03T00:00:00',
}

const REPORTS_USER: FilterPreset = {
  id: 22,
  name: 'My weekly report',
  page: 'reports',
  scope: 'user',
  filters: { submitted_by: 'testuser' },
  created_at: '2026-01-04T00:00:00',
  updated_at: '2026-01-04T00:00:00',
}

function mockEndpoints(options?: {
  version?: { version: string; build?: { label?: string } } | Error
  dashboard?: FilterPreset[] | Promise<FilterPreset[]>
  reports?: FilterPreset[] | Promise<FilterPreset[]>
}) {
  mockApiGet.mockImplementation((path: string) => {
    if (path === '/api/version') {
      if (options?.version instanceof Error) return Promise.reject(options.version)
      return Promise.resolve(options?.version ?? { version: '4.5.0' })
    }
    if (path.includes('page=dashboard')) {
      return Promise.resolve(options?.dashboard ?? [])
    }
    if (path.includes('page=reports')) {
      return Promise.resolve(options?.reports ?? [])
    }
    return Promise.resolve([])
  })
}

function presetRequestCalls() {
  return mockApiGet.mock.calls.filter(([path]) =>
    String(path).startsWith('/api/user/filter-presets'),
  )
}

function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="sidebar-location">
      {location.pathname}
      {location.search}
    </div>
  )
}

const zeroBadges = { activeCount: 0, unreadCount: 0, pendingCount: 0 }
const defaultProps = { badges: zeroBadges, mobileOpen: false, onMobileClose: vi.fn() }

function renderSidebar(pathname = '/', props = defaultProps) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <Sidebar {...props} />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Sidebar', () => {
  beforeEach(async () => {
    localStorage.clear()
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    mockAuth.isAdmin = false
    mockAuth.canViewReports = false
    mockAuth.role = 'reviewer'
    mockAuth.username = 'testuser'
    mockAuth.loading = false
    mockAuth.authenticated = true
    resetLastAppliedPresetForTests()
    vi.clearAllMocks()
    mockEndpoints()
  })

  it('renders the sidebar element', () => {
    renderSidebar()
    expect(screen.getByTestId('app-sidebar')).toBeDefined()
  })

  it('renders Dashboard and History links for all users', () => {
    renderSidebar()
    expect(screen.getByText('Dashboard')).toBeDefined()
    expect(screen.getByText('History')).toBeDefined()
  })

  it('renders Mentions link for non-viewer users', () => {
    mockAuth.role = 'reviewer'
    renderSidebar()
    expect(screen.getByText('Mentions')).toBeDefined()
  })

  it('hides Mentions link for viewer users', () => {
    mockAuth.role = 'viewer'
    renderSidebar()
    expect(screen.queryByText('Mentions')).toBeNull()
  })

  it('shows Reports and admin items when admin and canViewReports', () => {
    mockAuth.isAdmin = true
    mockAuth.canViewReports = true
    mockAuth.role = 'admin'
    renderSidebar()
    expect(screen.getByText('Reports')).toBeDefined()
    expect(screen.getByText('Users')).toBeDefined()
    expect(screen.getByText('Tokens')).toBeDefined()
    expect(screen.getByText('Settings')).toBeDefined()
    expect(screen.getByText('Chat')).toBeDefined()
    expect(screen.getByText('Logs')).toBeDefined()
  })

  it('shows Reports and admin items when isAdmin even if canViewReports is false', () => {
    // Stale auth: isAdmin true but canViewReports not refreshed — match backend grant
    mockAuth.isAdmin = true
    mockAuth.canViewReports = false
    mockAuth.role = 'admin'
    mockAuth.loading = false
    renderSidebar()
    expect(screen.getByText('Reports')).toBeDefined()
    expect(screen.getByText('Users')).toBeDefined()
    expect(screen.getByText('Tokens')).toBeDefined()
    expect(screen.getByText('Settings')).toBeDefined()
    expect(screen.getByText('Chat')).toBeDefined()
    expect(screen.getByText('Logs')).toBeDefined()
  })

  it('shows Reports only for non-admin with canViewReports', () => {
    mockAuth.isAdmin = false
    mockAuth.canViewReports = true
    mockAuth.role = 'reviewer'
    renderSidebar()
    expect(screen.getByText('Reports')).toBeDefined()
    expect(screen.queryByText('Users')).toBeNull()
    expect(screen.queryByText('Tokens')).toBeNull()
    expect(screen.queryByText('Settings')).toBeNull()
    expect(screen.queryByText('Chat')).toBeNull()
    expect(screen.queryByText('Logs')).toBeNull()
  })

  it('hides Reports and admin section for non-admin without canViewReports', () => {
    mockAuth.isAdmin = false
    mockAuth.canViewReports = false
    renderSidebar()
    expect(screen.queryByText('Reports')).toBeNull()
    expect(screen.queryByText('Users')).toBeNull()
    expect(screen.queryByText('Tokens')).toBeNull()
    expect(screen.queryByText('Settings')).toBeNull()
    expect(screen.queryByText('Chat')).toBeNull()
  })

  it('hides Reports and admin section while auth is loading', () => {
    mockAuth.loading = true
    mockAuth.isAdmin = true
    mockAuth.canViewReports = true
    mockAuth.role = 'admin'
    renderSidebar()
    expect(screen.queryByText('Reports')).toBeNull()
    expect(screen.queryByText('Users')).toBeNull()
    expect(screen.queryByText('Tokens')).toBeNull()
    expect(screen.queryByText('Settings')).toBeNull()
    expect(screen.queryByText('Chat')).toBeNull()
    expect(screen.queryByText('Logs')).toBeNull()
  })

  it('highlights the active route', () => {
    renderSidebar('/history')
    const historyLink = screen.getByText('History').closest('a')
    expect(historyLink?.className).toContain('bg-surface-elevated')
  })

  it('does not highlight inactive routes', () => {
    renderSidebar('/history')
    const dashboardLink = screen.getByText('Dashboard').closest('a')
    expect(dashboardLink?.className).not.toContain('bg-surface-elevated')
  })

  it('renders the resize handle', () => {
    renderSidebar()
    expect(screen.getByTestId('sidebar-resize-handle')).toBeDefined()
  })

  it('restores default width when localStorage is empty', () => {
    renderSidebar()
    const sidebar = screen.getByTestId('app-sidebar')
    expect(sidebar.style.width).toBe('200px') // default width
  })

  it('restores width from localStorage', () => {
    localStorage.setItem('rootcoz_sidebar_width', '250')
    renderSidebar()
    const sidebar = screen.getByTestId('app-sidebar')
    expect(sidebar.style.width).toBe('250px')
  })

  it('clamps restored width to valid range', () => {
    localStorage.setItem('rootcoz_sidebar_width', '999')
    renderSidebar()
    const sidebar = screen.getByTestId('app-sidebar')
    expect(sidebar.style.width).toBe('320px') // MAX_WIDTH
  })

  it('navigates to correct routes', () => {
    mockAuth.isAdmin = true
    mockAuth.canViewReports = true
    mockAuth.role = 'admin'
    renderSidebar()

    const dashboardLink = screen.getByText('Dashboard').closest('a')
    expect(dashboardLink?.getAttribute('href')).toBe('/')

    const historyLink = screen.getByText('History').closest('a')
    expect(historyLink?.getAttribute('href')).toBe('/history')

    const usersLink = screen.getByText('Users').closest('a')
    expect(usersLink?.getAttribute('href')).toBe('/admin/users')

    const reportsLink = screen.getByText('Reports').closest('a')
    expect(reportsLink?.getAttribute('href')).toBe('/reports')
  })

  // ─── Mobile behavior ─────────────────────────────────────────────

  it('does not show mobile sidebar when mobileOpen is false', () => {
    renderSidebar('/', { ...defaultProps, mobileOpen: false })
    expect(screen.queryByTestId('mobile-sidebar')).toBeNull()
    expect(screen.queryByTestId('mobile-sidebar-backdrop')).toBeNull()
  })

  it('shows mobile sidebar and backdrop when mobileOpen is true', () => {
    renderSidebar('/', { ...defaultProps, mobileOpen: true })
    expect(screen.getByTestId('mobile-sidebar')).toBeDefined()
    expect(screen.getByTestId('mobile-sidebar-backdrop')).toBeDefined()
  })

  it('calls onMobileClose when backdrop is clicked', async () => {
    const onClose = vi.fn()
    renderSidebar('/', { ...defaultProps, mobileOpen: true, onMobileClose: onClose })
    await userEvent.click(screen.getByTestId('mobile-sidebar-backdrop'))
    expect(onClose).toHaveBeenCalledOnce()
  })

  // ─── Version footer ──────────────────────────────────────────────

  it('shows server version in desktop sidebar', async () => {
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-version')).toHaveTextContent('RootCoz v4.5.0')
    })
    const link = screen.getByTestId('sidebar-version').querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://github.com/myk-org/rootcoz/releases/tag/v4.5.0')
    expect(link?.getAttribute('target')).toBe('_blank')
  })

  it('shows server version in mobile sidebar', async () => {
    renderSidebar('/', { ...defaultProps, mobileOpen: true })
    await waitFor(() => {
      const footers = screen.getAllByTestId('sidebar-version')
      expect(footers.length).toBe(2)
      for (const footer of footers) {
        expect(footer).toHaveTextContent('RootCoz v4.5.0')
      }
    })
  })

  it('shows the build label beside the version when the image carries one', async () => {
    // #520: a floating-tag build must never read as the release it reports.
    mockEndpoints({ version: { version: '4.5.0', build: { label: 'latest@d6a1f3c' } } })
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-version')).toHaveTextContent('RootCoz v4.5.0 · latest@d6a1f3c')
    })
    // D7: the link is the release the version names; the build is not that release,
    // so the suffix sits beside the anchor and never inside it.
    const link = screen.getByTestId('sidebar-version').querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://github.com/myk-org/rootcoz/releases/tag/v4.5.0')
    expect(link?.textContent).toBe('RootCoz v4.5.0')
    expect(screen.getByTestId('sidebar-build')).toHaveTextContent('latest@d6a1f3c')
  })

  it('renders the build label in the mobile sidebar too', async () => {
    mockEndpoints({ version: { version: '4.5.0', build: { label: 'latest@d6a1f3c' } } })
    renderSidebar('/', { ...defaultProps, mobileOpen: true })
    await waitFor(() => {
      const footers = screen.getAllByTestId('sidebar-version')
      expect(footers.length).toBe(2)
      for (const footer of footers) {
        expect(footer).toHaveTextContent('RootCoz v4.5.0 · latest@d6a1f3c')
      }
    })
  })

  it('carries the build label in the collapsed tooltip', async () => {
    localStorage.setItem('rootcoz_sidebar_collapsed', 'true')
    mockEndpoints({ version: { version: '4.5.0', build: { label: 'latest@d6a1f3c' } } })
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-version')).toBeDefined()
    })
    const trigger = screen.getByTestId('sidebar-version').querySelector('a')
    expect(trigger?.textContent).toBe('v')
    await userEvent.hover(trigger!)
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('rootcoz v4.5.0 · latest@d6a1f3c')
    })
  })

  it('re-reads the build identity when the tab regains focus', async () => {
    mockEndpoints({ version: { version: '4.5.0', build: { label: 'latest@aaaaaaa' } } })
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-build')).toHaveTextContent('latest@aaaaaaa')
    })
    // A deploy replaced the build under the open page; focus picks up the new one.
    mockEndpoints({ version: { version: '4.5.0', build: { label: 'latest@bbbbbbb' } } })
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-build')).toHaveTextContent('latest@bbbbbbb')
    })
  })

  it('hides version footer when health endpoint fails', async () => {
    mockEndpoints({ version: new Error('unavailable') })
    renderSidebar()
    // Give effect time to settle; footer should stay absent
    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledWith('/api/version', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    })
    expect(screen.queryByTestId('sidebar-version')).toBeNull()
  })

  it('does not fetch version when not authenticated', async () => {
    mockAuth.authenticated = false
    renderSidebar()
    expect(mockApiGet).not.toHaveBeenCalled()
    expect(screen.queryByTestId('sidebar-version')).toBeNull()
  })

  it('shows abbreviated version with tooltip when sidebar is collapsed', async () => {
    localStorage.setItem('rootcoz_sidebar_collapsed', 'true')
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-version')).toBeDefined()
    })
    // Trigger link shows exactly "v" (not the full version)
    const trigger = screen.getByTestId('sidebar-version').querySelector('a')
    expect(trigger?.textContent).toBe('v')
    expect(trigger?.getAttribute('href')).toBe('https://github.com/myk-org/rootcoz/releases/tag/v4.5.0')
    // Full version is in the tooltip content (portaled; opens on hover)
    await userEvent.hover(trigger!)
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('rootcoz v4.5.0')
    })
  })

  // ─── Saved-filter placement, viewports, keyboard & resize ────────

  it('places Server then User saved filters directly under Dashboard and Reports links', async () => {
    mockAuth.isAdmin = true
    mockAuth.canViewReports = true
    mockAuth.role = 'admin'
    mockEndpoints({
      dashboard: [DASH_USER, DASH_SERVER],
      reports: [REPORTS_USER, REPORTS_SERVER],
    })

    renderSidebar('/history', {
      badges: { activeCount: 2, unreadCount: 3, pendingCount: 1 },
      mobileOpen: false,
      onMobileClose: vi.fn(),
    })

    const desktop = screen.getByTestId('app-sidebar')
    await within(desktop).findByText('mtv-2.11')

    expect(within(desktop).queryByText('Saved filters')).toBeNull()
    expect(within(desktop).queryByText('Yours')).toBeNull()

    const dashboardLink = within(desktop).getByRole('link', { name: /Dashboard/ })
    const dashServerBtn = within(desktop).getByRole('button', { name: 'Dashboard Server saved filters' })
    const dashServerPreset = within(desktop).getByTestId('sidebar-filter-preset-11')
    const dashUserBtn = within(desktop).getByRole('button', { name: 'Dashboard User saved filters' })
    const dashUserPreset = within(desktop).getByTestId('sidebar-filter-preset-12')
    const historyLink = within(desktop).getByRole('link', { name: 'History' })
    const mentionsLink = within(desktop).getByRole('link', { name: /Mentions/ })
    const reportsLink = within(desktop).getByRole('link', { name: 'Reports' })
    const repServerBtn = within(desktop).getByRole('button', { name: 'Reports Server saved filters' })
    const repServerPreset = within(desktop).getByTestId('sidebar-filter-preset-21')
    const repUserBtn = within(desktop).getByRole('button', { name: 'Reports User saved filters' })
    const repUserPreset = within(desktop).getByTestId('sidebar-filter-preset-22')
    const usersLink = within(desktop).getByRole('link', { name: /Users/ })

    const orderedNodes = [
      dashboardLink,
      dashServerBtn,
      dashServerPreset,
      dashUserBtn,
      dashUserPreset,
      historyLink,
      mentionsLink,
      reportsLink,
      repServerBtn,
      repServerPreset,
      repUserBtn,
      repUserPreset,
      usersLink,
    ]
    for (let i = 0; i < orderedNodes.length - 1; i += 1) {
      expect(
        orderedNodes[i].compareDocumentPosition(orderedNodes[i + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()
    }

    // Badges remain rendered accurately alongside saved-filter trees
    expect(within(dashboardLink).getByText('2')).toBeDefined()
    expect(within(mentionsLink).getByText('3')).toBeDefined()
    expect(within(usersLink).getByText('1')).toBeDefined()
  })

  it('fetches presets once for admin even when canViewReports is false', async () => {
    mockAuth.isAdmin = true
    mockAuth.canViewReports = false
    mockAuth.role = 'admin'
    mockEndpoints({
      dashboard: [DASH_SERVER],
      reports: [REPORTS_SERVER],
    })

    renderSidebar()
    const desktop = screen.getByTestId('app-sidebar')
    expect(await within(desktop).findByText('cnv-5.0')).toBeDefined()
    expect(presetRequestCalls().map(([p]) => p)).toEqual([
      '/api/user/filter-presets?page=dashboard',
      '/api/user/filter-presets?page=reports',
    ])
  })

  it('shares one preset loader and mirrors disclosure state across desktop and mobile with unique local ARIA IDs', async () => {
    mockAuth.canViewReports = true
    mockEndpoints({
      dashboard: [DASH_SERVER, DASH_USER],
      reports: [REPORTS_SERVER, REPORTS_USER],
    })

    const onClose = vi.fn()
    const { container, rerender } = render(
      <MemoryRouter initialEntries={['/']}>
        <Sidebar badges={zeroBadges} mobileOpen={true} onMobileClose={onClose} />
      </MemoryRouter>,
    )

    const desktop = screen.getByTestId('app-sidebar')
    const mobile = screen.getByTestId('mobile-sidebar')
    await within(desktop).findByText('mtv-2.11')
    await within(mobile).findByText('mtv-2.11')

    // Exactly one request per permitted page despite two mounted sidebars; version call excluded.
    expect(presetRequestCalls()).toHaveLength(2)

    // No nested interactive controls anywhere in desktop or mobile.
    expect(container.querySelectorAll('a button, button a, button button, a a')).toHaveLength(0)

    // Toggle on desktop -> mobile mirrors immediately without refetching.
    await userEvent.click(within(desktop).getByRole('button', { name: 'Dashboard Server saved filters' }))
    expect(
      within(desktop).getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(
      within(mobile).getByRole('button', { name: 'Dashboard Server saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')

    // Toggle on mobile -> desktop mirrors immediately without refetching.
    await userEvent.click(within(mobile).getByRole('button', { name: 'Reports saved filters' }))
    expect(
      within(desktop).getByRole('button', { name: 'Reports saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(
      within(mobile).getByRole('button', { name: 'Reports saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')

    // Every aria-controls resolves inside its own aside, even for closed lists, and all IDs are unique.
    const allIds = Array.from(container.querySelectorAll('[id]')).map((el) => el.id)
    expect(new Set(allIds).size).toBe(allIds.length)

    for (const root of [desktop, mobile]) {
      const triggers = root.querySelectorAll<HTMLElement>('[aria-controls]')
      expect(triggers.length).toBe(6)
      for (const trigger of triggers) {
        const targetId = trigger.getAttribute('aria-controls')!
        const target = document.getElementById(targetId)
        expect(target).not.toBeNull()
        expect(root.contains(target)).toBe(true)
      }
    }

    // Closing and reopening mobile adds zero preset loads.
    rerender(
      <MemoryRouter initialEntries={['/']}>
        <Sidebar badges={zeroBadges} mobileOpen={false} onMobileClose={onClose} />
      </MemoryRouter>,
    )
    rerender(
      <MemoryRouter initialEntries={['/']}>
        <Sidebar badges={zeroBadges} mobileOpen={true} onMobileClose={onClose} />
      </MemoryRouter>,
    )
    expect(presetRequestCalls()).toHaveLength(2)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('supports keyboard navigation, separates link vs chevron actions, and skips hidden descendants on Tab', async () => {
    const user = userEvent.setup()
    mockAuth.canViewReports = true
    mockEndpoints({
      dashboard: [DASH_SERVER, DASH_USER],
      reports: [REPORTS_SERVER],
    })
    const onClose = vi.fn()
    renderSidebar('/history', { badges: zeroBadges, mobileOpen: false, onMobileClose: onClose })

    const desktop = screen.getByTestId('app-sidebar')
    await within(desktop).findByText('mtv-2.11')

    // Tab into sidebar: first stop is Dashboard link; Enter navigates without changing disclosure.
    await user.tab()
    const dashboardLink = within(desktop).getByRole('link', { name: 'Dashboard' })
    expect(document.activeElement).toBe(dashboardLink)
    await user.keyboard('{Enter}')
    expect(screen.getByTestId('sidebar-location').textContent).toBe('/')
    const dashboardChevron = within(desktop).getByRole('button', { name: 'Dashboard saved filters' })
    expect(dashboardChevron.getAttribute('aria-expanded')).toBe('true')

    // Tab to Dashboard chevron; Enter collapses page tree without navigating or closing mobile.
    await user.tab()
    expect(document.activeElement).toBe(dashboardChevron)
    await user.keyboard('{Enter}')
    expect(dashboardChevron.getAttribute('aria-expanded')).toBe('false')
    expect(onClose).not.toHaveBeenCalled()

    // Next Tab must skip hidden Dashboard Server/User triggers and shortcuts, landing on History link!
    await user.tab()
    expect(document.activeElement).toBe(within(desktop).getByRole('link', { name: 'History' }))

    // Shift+Tab back to Dashboard chevron and press Space to reopen.
    await user.tab({ shift: true })
    expect(document.activeElement).toBe(dashboardChevron)
    await user.keyboard(' ')
    expect(dashboardChevron.getAttribute('aria-expanded')).toBe('true')

    // Tab into Dashboard Server trigger and collapse with Space; Tab skips its hidden shortcut and lands on Dashboard User trigger.
    await user.tab()
    const dashServerTrigger = within(desktop).getByRole('button', { name: 'Dashboard Server saved filters' })
    expect(document.activeElement).toBe(dashServerTrigger)
    await user.keyboard(' ')
    expect(dashServerTrigger.getAttribute('aria-expanded')).toBe('false')
    await user.tab()
    expect(document.activeElement).toBe(
      within(desktop).getByRole('button', { name: 'Dashboard User saved filters' }),
    )
  })

  it('temporarily exposes accessible preset icons with tooltips in the narrow rail without mutating saved disclosures, while mobile stays full text', async () => {
    const user = userEvent.setup()
    mockAuth.canViewReports = true
    localStorage.setItem('rootcoz_sidebar_filters:testuser:Dashboard', 'false')
    localStorage.setItem('rootcoz_sidebar_filters:testuser:DashboardUser', 'false')
    localStorage.setItem('rootcoz_sidebar_collapsed', 'true')
    mockEndpoints({
      dashboard: [DASH_SERVER, DASH_USER],
      reports: [REPORTS_SERVER],
    })

    renderSidebar('/history', { badges: zeroBadges, mobileOpen: true, onMobileClose: vi.fn() })
    const desktop = screen.getByTestId('app-sidebar')
    const mobile = screen.getByTestId('mobile-sidebar')

    // Desktop icon rail hides disclosure buttons, gives icon links & preset shortcuts accessible names,
    // and temporarily displays shortcuts even though Dashboard and DashboardUser are saved as false.
    const railPreset = await within(desktop).findByRole('button', { name: 'My recent failures' })
    expect(within(desktop).getByRole('link', { name: 'Dashboard' })).toBeDefined()
    expect(within(desktop).getByRole('link', { name: 'Reports' })).toBeDefined()
    expect(within(desktop).queryByRole('button', { name: 'Dashboard saved filters' })).toBeNull()

    await user.hover(railPreset)
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('My recent failures')
    })

    // Mobile overlay simultaneously renders the full text tree honoring the saved false states.
    expect(
      within(mobile).getByRole('button', { name: 'Dashboard saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(localStorage.getItem('rootcoz_sidebar_filters:testuser:Dashboard')).toBe('false')
    expect(localStorage.getItem('rootcoz_sidebar_filters:testuser:DashboardUser')).toBe('false')

    // Dragging the desktop rail wide restores the exact saved disclosure states with zero extra preset fetches.
    const handle = screen.getByTestId('sidebar-resize-handle')
    fireEvent.mouseDown(handle, { clientX: 48 })
    fireEvent.mouseMove(document, { clientX: 220 })
    fireEvent.mouseUp(document)

    expect(
      within(desktop).getByRole('button', { name: 'Dashboard saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    await user.click(within(desktop).getByRole('button', { name: 'Dashboard saved filters' }))
    expect(
      within(desktop).getByRole('button', { name: 'Dashboard User saved filters' }).getAttribute('aria-expanded'),
    ).toBe('false')
    expect(presetRequestCalls()).toHaveLength(2)
  })

  it('persists width only on mouseup, clamps/collapses at threshold, leaves disclosure keys untouched, and cleans up mid-drag unmount', async () => {
    mockAuth.canViewReports = true
    localStorage.setItem('rootcoz_sidebar_filters:testuser:Dashboard', 'false')
    mockEndpoints({ dashboard: [DASH_SERVER], reports: [] })

    const { unmount } = renderSidebar('/')
    const desktop = screen.getByTestId('app-sidebar')
    await within(desktop).findByTestId('sidebar-filter-preset-11')
    const handle = screen.getByTestId('sidebar-resize-handle')

    // Drag from 200px to 260px: live width updates, localStorage written only at mouseup.
    fireEvent.mouseDown(handle, { clientX: 200 })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    fireEvent.mouseMove(document, { clientX: 260 })
    expect(desktop.style.width).toBe('260px')
    expect(localStorage.getItem('rootcoz_sidebar_width')).toBeNull()

    // Clamping at MAX_WIDTH (320px)
    fireEvent.mouseMove(document, { clientX: 500 })
    expect(desktop.style.width).toBe('320px')

    fireEvent.mouseUp(document)
    expect(localStorage.getItem('rootcoz_sidebar_width')).toBe('320')
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')

    // Drag below COLLAPSE_THRESHOLD (100px): collapses to 48px rail while remembering last expanded width (320).
    fireEvent.mouseDown(handle, { clientX: 320 })
    fireEvent.mouseMove(document, { clientX: 60 })
    expect(desktop.style.width).toBe('48px')
    fireEvent.mouseUp(document)
    expect(localStorage.getItem('rootcoz_sidebar_collapsed')).toBe('true')
    expect(localStorage.getItem('rootcoz_sidebar_width')).toBe('320')
    expect(localStorage.getItem('rootcoz_sidebar_filters:testuser:Dashboard')).toBe('false')

    // Repeat mouseup is a safe no-op.
    fireEvent.mouseUp(document)
    expect(document.body.style.cursor).toBe('')

    // Mid-drag unmount cleans up body styles and document listeners.
    fireEvent.mouseDown(handle, { clientX: 48 })
    fireEvent.mouseMove(document, { clientX: 210 })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    localStorage.removeItem('rootcoz_sidebar_width')
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')

    fireEvent.mouseMove(document, { clientX: 280 })
    fireEvent.mouseUp(document)
    expect(localStorage.getItem('rootcoz_sidebar_width')).toBeNull()
  })

  it('closes mobile sidebar on same-path query preset selection and isolates account switches', async () => {
    mockAuth.canViewReports = true
    const onClose = vi.fn()
    mockEndpoints({
      dashboard: [DASH_SERVER],
      reports: [REPORTS_SERVER],
    })

    const { rerender } = render(
      <MemoryRouter initialEntries={['/?version=2.10']}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <Sidebar badges={zeroBadges} mobileOpen={true} onMobileClose={onClose} />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    )

    const mobile = screen.getByTestId('mobile-sidebar')
    await within(mobile).findByText('mtv-2.11')

    await userEvent.click(within(mobile).getByTestId('sidebar-filter-preset-11'))
    expect(screen.getByTestId('sidebar-location').textContent).toBe('/?version=2.11')
    expect(onClose).toHaveBeenCalledTimes(1)

    // Switch account from testuser to bob: testuser presets synchronously cleared before bob resolves
    let resolveBob!: (presets: FilterPreset[]) => void
    const bobPending = new Promise<FilterPreset[]>((res) => {
      resolveBob = res
    })
    localStorage.setItem('rootcoz_sidebar_filters:bob:Dashboard', 'false')
    mockEndpoints({ dashboard: bobPending, reports: [] })
    mockAuth.username = 'bob'

    rerender(
      <MemoryRouter initialEntries={['/?version=2.11']}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <Sidebar badges={zeroBadges} mobileOpen={true} onMobileClose={onClose} />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    )

    expect(within(screen.getByTestId('mobile-sidebar')).queryByText('mtv-2.11')).toBeNull()

    await act(async () => {
      resolveBob([{ ...DASH_USER, id: 99, name: 'Bob mobile preset' }])
    })
    expect(
      within(screen.getByTestId('mobile-sidebar'))
        .getByRole('button', { name: 'Dashboard saved filters' })
        .getAttribute('aria-expanded'),
    ).toBe('false')
  })
})
