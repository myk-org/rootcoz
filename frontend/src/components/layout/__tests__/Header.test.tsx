import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Header } from '../Header'

const mockAuth = {
  username: 'testuser',
  isAdmin: false,
  isOperator: false,
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

// Mock UserBadge to avoid its internal routing/auth dependencies
vi.mock('../UserBadge', () => ({
  UserBadge: () => <div data-testid="user-badge">UserBadge</div>,
}))

const defaultProps = { mobileOpen: false, onMobileToggle: vi.fn() }

function renderHeader(pathname = '/', props = defaultProps) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Header {...props} />
    </MemoryRouter>,
  )
}

describe('Header', () => {
  beforeEach(() => {
    mockAuth.isOperator = false
    mockAuth.role = 'reviewer'
  })

  it('renders the header element', () => {
    renderHeader()
    expect(screen.getByTestId('app-header')).toBeDefined()
  })

  it('renders the RootCoz logo link', () => {
    renderHeader()
    const logo = screen.getByText('RootCoz')
    expect(logo.closest('a')?.getAttribute('href')).toBe('/')
  })

  it('renders the User Guide link', () => {
    renderHeader()
    expect(screen.getByText('Guide')).toBeDefined()
  })

  it('renders the user badge', () => {
    renderHeader()
    expect(screen.getByTestId('user-badge')).toBeDefined()
  })

  it('shows New Analysis button for operators', () => {
    mockAuth.isOperator = true
    renderHeader()
    expect(screen.getByText('New Analysis')).toBeDefined()
  })

  it('hides New Analysis button for reviewers', () => {
    mockAuth.isOperator = false
    mockAuth.role = 'reviewer'
    renderHeader()
    expect(screen.queryByText('New Analysis')).toBeNull()
  })

  it('hides New Analysis button for viewers', () => {
    mockAuth.isOperator = false
    mockAuth.role = 'viewer'
    renderHeader()
    expect(screen.queryByText('New Analysis')).toBeNull()
  })

  it('does not render navigation links (moved to sidebar)', () => {
    renderHeader()
    expect(screen.queryByText('Dashboard')).toBeNull()
    expect(screen.queryByText('History')).toBeNull()
    expect(screen.queryByText('Mentions')).toBeNull()
  })

  it('always renders the Feedback button', () => {
    renderHeader()
    expect(screen.getByText('Feedback')).toBeDefined()
  })

  it('renders mobile menu toggle button', () => {
    renderHeader()
    expect(screen.getByTestId('mobile-menu-toggle')).toBeDefined()
  })

  it('shows hamburger icon when mobile menu is closed', () => {
    renderHeader('/', { mobileOpen: false, onMobileToggle: vi.fn() })
    expect(screen.getByLabelText('Open menu')).toBeDefined()
  })

  it('shows X icon when mobile menu is open', () => {
    renderHeader('/', { mobileOpen: true, onMobileToggle: vi.fn() })
    expect(screen.getByLabelText('Close menu')).toBeDefined()
  })

  it('calls onMobileToggle when hamburger is clicked', async () => {
    const onToggle = vi.fn()
    renderHeader('/', { mobileOpen: false, onMobileToggle: onToggle })
    await userEvent.click(screen.getByTestId('mobile-menu-toggle'))
    expect(onToggle).toHaveBeenCalledOnce()
  })
})
