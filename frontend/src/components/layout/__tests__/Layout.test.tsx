import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { Layout } from '../Layout'

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

import { useSSE } from '@/lib/useSSE'

const mockUseSSE = useSSE as unknown as ReturnType<typeof vi.fn>

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({}),
    post: vi.fn(),
  },
}))

vi.mock('../UserBadge', () => ({
  UserBadge: () => <div data-testid="user-badge">UserBadge</div>,
}))

// Mock useSSE — no-op in tests (SSEProvider not mounted)
vi.mock('@/lib/useSSE', () => ({
  useSSE: vi.fn(),
}))

beforeEach(() => {
  localStorage.clear()
  mockAuth.isAdmin = false
  mockUseSSE.mockClear()
})

function renderLayout(pathname = '/') {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div data-testid="page-content">Dashboard Content</div>} />
          <Route path="/history" element={<div data-testid="page-content">History Content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('Layout', () => {
  it('renders header, sidebar, and main content', () => {
    renderLayout()
    expect(screen.getByTestId('app-header')).toBeDefined()
    expect(screen.getByTestId('app-sidebar')).toBeDefined()
    expect(screen.getByTestId('page-content')).toBeDefined()
  })

  it('renders child route content via Outlet', () => {
    renderLayout()
    expect(screen.getByText('Dashboard Content')).toBeDefined()
  })

  it('lets the scroll area shrink below its content width', () => {
    // The layout row is overflow-hidden, so without min-w-0 the scroll area
    // keeps its intrinsic width and wide filter rows (History) get clipped.
    const { container } = renderLayout()
    const wrapper = container.querySelector('main')?.parentElement
    expect(wrapper?.className).toContain('min-w-0')
  })

  it('renders the RootCoz logo in the header', () => {
    renderLayout()
    expect(screen.getByText('RootCoz')).toBeDefined()
  })

  it('renders navigation links in the sidebar', () => {
    renderLayout()
    expect(screen.getByText('Dashboard')).toBeDefined()
    expect(screen.getByText('History')).toBeDefined()
  })

  it('renders mobile menu toggle in header', () => {
    renderLayout()
    expect(screen.getByTestId('mobile-menu-toggle')).toBeDefined()
  })

  it('subscribes to the multiplexed navbar topic and handles pending-count', () => {
    // #227: the badge is driven by /api/stream?topics=navbar, which emits
    // topic-prefixed events (navbar:pending-count) that useSSE maps to the
    // unprefixed handler name.
    mockAuth.isAdmin = true
    renderLayout()
    const [topic, events] = mockUseSSE.mock.calls.at(-1)!
    expect(topic).toBe('navbar')
    expect(Object.keys(events)).toContain('pending-count')
  })

  it('updates the pending badge from the navbar pending-count SSE event', () => {
    // #227: the badge is driven by SSE, not a one-shot fetch on mount
    mockAuth.isAdmin = true
    renderLayout()
    expect(screen.queryByText('3')).toBeNull()

    const events = mockUseSSE.mock.calls.at(-1)![1]
    act(() => events['pending-count']('3'))
    expect(screen.getByText('3')).toBeDefined()

    act(() => events['pending-count']('0'))
    expect(screen.queryByText('3')).toBeNull()
  })

  it('hides admin navigation for a non-admin after an admin test', () => {
    // Guards the shared mockAuth: an admin test that leaked isAdmin=true
    // would keep the admin nav items mounted for every later test.
    renderLayout()
    expect(screen.queryByText('Users')).toBeNull()
  })
})
