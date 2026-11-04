import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { AdminServerStatusPage, type ServerStatus } from '@/pages/AdminServerStatusPage'
import { ProtectedRoute } from '@/components/shared/ProtectedRoute'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: { get: (...args: unknown[]) => apiGet(...args) },
  }
})

const mockAuth = {
  username: 'admin',
  isAdmin: true,
  isOperator: true,
  canViewReports: true,
  role: 'admin',
  loading: false,
  authenticated: true,
  login: vi.fn(),
  logout: vi.fn(),
  refreshAuth: vi.fn(),
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

const GiB = 1024 ** 3
const MiB = 1024 ** 2

function healthy(): ServerStatus {
  return {
    disk: {
      available: true,
      used_bytes: 12 * GiB,
      capacity_bytes: 20 * GiB,
      used_ratio: 0.6,
      filesystem_note: 'Measured on the filesystem that holds the data directory.',
      database_bytes: 1024,
      transcripts_bytes: 2048,
      logs_bytes: 512,
    },
    cpu: { available: true, process_percent: 12.34, quota_cores: 2, period_us: 100000 },
    memory: {
      available: true,
      rss_bytes: 200 * MiB,
      current_bytes: 300 * MiB,
      limit_bytes: 1 * GiB,
      percent_of_limit: 29.3,
    },
    process: {
      version: '1.2.3',
      git_sha: 'abc1234',
      uptime_seconds: 93784,
      pid: 42,
      loop_lag_ms: 1.5,
    },
    database: { available: true, size_bytes: 4096, results: 12, failures: 3, test_entries: 40 },
    sidecar: { reachable: true, models: 5, free_models: 2 },
    activity: { analyses_running: 1, sse_clients: 2 },
    warnings: [],
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/admin/server-status']}>
      <AdminServerStatusPage />
    </MemoryRouter>,
  )
}

function renderGuarded() {
  return render(
    <MemoryRouter initialEntries={['/admin/server-status']}>
      <Routes>
        <Route path="/" element={<div>home</div>} />
        <Route
          path="/admin/server-status"
          element={
            <ProtectedRoute adminOnly>
              <AdminServerStatusPage />
            </ProtectedRoute>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AdminServerStatusPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    mockAuth.isAdmin = true
  })

  it('renders each warning as text beside its level', async () => {
    const status = healthy()
    status.warnings = [
      {
        level: 'critical',
        code: 'disk_critical',
        message: 'Data directory is 92% full — free space now, writes will start failing.',
      },
      {
        level: 'warn',
        code: 'sidecar_unreachable',
        message: 'AI sidecar is unreachable — analyses and AI chat will fail.',
      },
    ]
    apiGet.mockResolvedValue(status)
    renderPage()

    expect(await screen.findByText(/92% full/)).toBeInTheDocument()
    expect(screen.getByText('Critical')).toBeInTheDocument()
    expect(screen.getByText('AI sidecar is unreachable — analyses and AI chat will fail.')).toBeInTheDocument()
    expect(screen.getByText('Warning')).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(2)
  })

  it('renders unavailable blocks with their reason and no fake zeros', async () => {
    const status = healthy()
    status.disk = {
      available: false,
      reason: 'data directory usage not readable',
      database_bytes: null,
      transcripts_bytes: null,
      logs_bytes: null,
    }
    status.cpu = {
      available: false,
      reason: 'process CPU counters not readable at /proc/self/stat',
    }
    status.memory = { available: false, reason: 'memory accounting not readable' }
    status.database = { available: false, reason: 'database not readable: disk I/O error' }
    status.sidecar = { reachable: false, models: null, free_models: null, reason: 'connection refused' }
    apiGet.mockResolvedValue(status)
    renderPage()

    expect(await screen.findByText(/process CPU counters not readable/)).toBeInTheDocument()
    expect(screen.getByText(/data directory usage not readable/)).toBeInTheDocument()
    expect(screen.getByText(/memory accounting not readable/)).toBeInTheDocument()
    expect(screen.getByText(/database not readable: disk I\/O error/)).toBeInTheDocument()
    expect(screen.getByText(/connection refused/)).toBeInTheDocument()
    expect(screen.getByText('Unreachable')).toBeInTheDocument()
    // Six blocks are unavailable (disk, cpu, memory, database, sidecar, activity last row is fine).
    expect(screen.getAllByText(/not visible in this container/).length).toBeGreaterThanOrEqual(5)
    // A degraded probe must never read as a healthy zero.
    expect(screen.queryByText('0.0%')).toBeNull()
    expect(screen.queryByText(/^0 B$/)).toBeNull()
    expect(screen.queryByText('0 cores')).toBeNull()
  })

  it('renders the per-directory breakdown rows', async () => {
    apiGet.mockResolvedValue(healthy())
    renderPage()

    expect(await screen.findAllByText('Database')).toHaveLength(2) // card title + breakdown row
    expect(screen.getByText('Transcripts')).toBeInTheDocument()
    expect(screen.getByText('Logs')).toBeInTheDocument()
    expect(screen.getByText('1.0 KiB')).toBeInTheDocument()
    expect(screen.getByText('2.0 KiB')).toBeInTheDocument()
    expect(screen.getByText('512 B')).toBeInTheDocument()
    expect(screen.getByText('60.0%')).toBeInTheDocument()
    expect(screen.getByText('12.0 GiB of 20.0 GiB')).toBeInTheDocument()
  })

  it('renders the other blocks from the same payload', async () => {
    apiGet.mockResolvedValue(healthy())
    renderPage()

    expect(await screen.findByText('12.3%')).toBeInTheDocument()
    expect(screen.getByText('29.3%')).toBeInTheDocument()
    expect(screen.getByText('1d 2h')).toBeInTheDocument()
    expect(screen.getByText('abc1234')).toBeInTheDocument()
    expect(screen.getByText('1.5 ms')).toBeInTheDocument()
    expect(screen.getByText('Reachable')).toBeInTheDocument()
    expect(screen.getByText('updated just now')).toBeInTheDocument()
  })

  it('shows an error state instead of a blank page when the fetch fails', async () => {
    apiGet.mockRejectedValue(new Error('API error 500: Internal Server Error'))
    renderPage()

    expect(await screen.findByText('API error 500: Internal Server Error')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Server Status' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /refresh/i })).toBeInTheDocument()
  })

  it('refetches on the refresh button', async () => {
    apiGet.mockResolvedValue(healthy())
    renderPage()
    await screen.findByText('12.3%')
    expect(apiGet).toHaveBeenCalledTimes(1)

    await userEvent.setup().click(screen.getByRole('button', { name: /refresh/i }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(apiGet).toHaveBeenCalledWith('/api/admin/server-status')
  })

  it('is admin-only like the other admin routes', async () => {
    apiGet.mockResolvedValue(healthy())
    mockAuth.isAdmin = false
    renderGuarded()

    expect(await screen.findByText('home')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Server Status' })).toBeNull()
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('renders for an admin user', async () => {
    apiGet.mockResolvedValue(healthy())
    renderGuarded()

    expect(await screen.findByRole('heading', { name: 'Server Status' })).toBeInTheDocument()
  })
})
