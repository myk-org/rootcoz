import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ProtectedRoute } from '../ProtectedRoute'

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

function renderWithRoute(element: React.ReactNode, path = '/reports') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<div>home</div>} />
        <Route path="/login" element={<div>login</div>} />
        <Route path={path} element={element} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ProtectedRoute', () => {
  beforeEach(() => {
    mockAuth.isAdmin = false
    mockAuth.canViewReports = false
    mockAuth.role = 'reviewer'
    mockAuth.loading = false
    mockAuth.authenticated = true
  })

  it('renders children when authenticated with no extra gates', () => {
    renderWithRoute(
      <ProtectedRoute><div>ok</div></ProtectedRoute>,
      '/history',
    )
    expect(screen.getByText('ok')).toBeDefined()
  })

  it('redirects unauthenticated users to login', () => {
    mockAuth.authenticated = false
    renderWithRoute(
      <ProtectedRoute><div>ok</div></ProtectedRoute>,
      '/history',
    )
    expect(screen.getByText('login')).toBeDefined()
  })

  it('allows reportsAccess when canViewReports is true', () => {
    mockAuth.canViewReports = true
    renderWithRoute(
      <ProtectedRoute reportsAccess><div>reports</div></ProtectedRoute>,
    )
    expect(screen.getByText('reports')).toBeDefined()
  })

  it('allows reportsAccess when isAdmin even if canViewReports is false', () => {
    // Stale auth: admin cookie/role true but canViewReports not yet refreshed
    mockAuth.isAdmin = true
    mockAuth.canViewReports = false
    renderWithRoute(
      <ProtectedRoute reportsAccess><div>reports</div></ProtectedRoute>,
    )
    expect(screen.getByText('reports')).toBeDefined()
    expect(screen.queryByText('home')).toBeNull()
  })

  it('blocks reportsAccess when canViewReports is false', () => {
    mockAuth.canViewReports = false
    mockAuth.isAdmin = false
    renderWithRoute(
      <ProtectedRoute reportsAccess><div>reports</div></ProtectedRoute>,
    )
    expect(screen.getByText('home')).toBeDefined()
    expect(screen.queryByText('reports')).toBeNull()
  })

  it('blocks adminOnly when not admin', () => {
    mockAuth.isAdmin = false
    renderWithRoute(
      <ProtectedRoute adminOnly><div>admin</div></ProtectedRoute>,
      '/admin/users',
    )
    expect(screen.getByText('home')).toBeDefined()
  })
})
