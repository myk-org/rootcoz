import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Sidebar } from '../Sidebar'

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
    get: vi.fn().mockResolvedValue({ version: '4.5.0' }),
  },
}))

const zeroBadges = { activeCount: 0, unreadCount: 0, pendingCount: 0 }
const defaultProps = { badges: zeroBadges, mobileOpen: false, onMobileClose: vi.fn() }

function renderSidebar(pathname = '/', props = defaultProps) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Sidebar {...props} />
    </MemoryRouter>,
  )
}

describe('Sidebar', () => {
  beforeEach(async () => {
    localStorage.clear()
    mockAuth.isAdmin = false
    mockAuth.canViewReports = false
    mockAuth.role = 'reviewer'
    mockAuth.username = 'testuser'
    mockAuth.loading = false
    vi.clearAllMocks()
    const { api } = vi.mocked(await import('@/lib/api'))
    api.get.mockResolvedValue({ version: '4.5.0' })
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
      expect(screen.getByTestId('sidebar-version')).toHaveTextContent('v4.5.0')
    })
  })

  it('shows server version in mobile sidebar', async () => {
    renderSidebar('/', { ...defaultProps, mobileOpen: true })
    await waitFor(() => {
      const footers = screen.getAllByTestId('sidebar-version')
      expect(footers.length).toBe(2)
      for (const footer of footers) {
        expect(footer).toHaveTextContent('v4.5.0')
      }
    })
  })

  it('hides version footer when health endpoint fails', async () => {
    const { api } = vi.mocked(await import('@/lib/api'))
    api.get.mockRejectedValue(new Error('unavailable'))
    renderSidebar()
    // Give effect time to settle; footer should stay absent
    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith('/api/version', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    })
    expect(screen.queryByTestId('sidebar-version')).toBeNull()
  })

  it('shows abbreviated version with tooltip when sidebar is collapsed', async () => {
    localStorage.setItem('rootcoz_sidebar_collapsed', 'true')
    renderSidebar()
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-version')).toBeDefined()
    })
    // Trigger span shows exactly "v" (not the full version)
    const trigger = screen.getByTestId('sidebar-version').querySelector('span')
    expect(trigger?.textContent).toBe('v')
    // Full version is in the tooltip content (portaled; opens on hover)
    await userEvent.hover(trigger!)
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('v4.5.0')
    })
  })
})
