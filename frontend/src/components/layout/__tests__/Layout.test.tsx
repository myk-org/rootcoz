import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { Layout } from '../Layout'

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    username: 'testuser',
    isAdmin: false,
    isOperator: false,
    role: 'reviewer',
    loading: false,
    authenticated: true,
    login: vi.fn(),
    logout: vi.fn(),
    refreshAuth: vi.fn(),
  }),
}))

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
vi.mock('@/lib/SSEProvider', () => ({
  useSSE: vi.fn(),
}))

beforeEach(() => {
  localStorage.clear()
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
})
