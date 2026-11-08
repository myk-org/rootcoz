import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import {
  AdminServerStatusPage,
  type ServerStatus,
} from '@/pages/AdminServerStatusPage'
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
    cpu: {
      available: true,
      process_percent: 12.34,
      quota_cores: 2,
      quota_state: 'set',
      period_us: 100000,
    },
    memory: {
      available: true,
      rss_bytes: 200 * MiB,
      current_bytes: 300 * MiB,
      working_set_bytes: 280 * MiB,
      limit_bytes: 1 * GiB,
      limit_state: 'set',
      percent_of_limit: 27.3,
    },
    process: {
      version: '1.2.3',
      git_sha: 'abc1234',
      uptime_seconds: 93784,
      pid: 42,
      restart_count: null,
      loop_lag_ms: 1.5,
    },
    database: {
      available: true,
      size_bytes: 4096,
      results: 12,
      failures: 3,
      test_entries: 40,
      compression_ratio: null,
    },
    sidecar: { reachable: true, models: 5, free_models: 2 },
    activity: {
      analyses_running: 1,
      analyses: [
        {
          job_id: 'j1',
          job_name: 'nightly',
          build_number: 7,
          status: 'running',
          elapsed_seconds: 3600,
        },
      ],
      chat_turns_in_flight: 1,
      sse_clients: 2,
    },
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
    expect(screen.getByText('27.3%')).toBeInTheDocument()
    expect(screen.getByText('1d 2h')).toBeInTheDocument()
    expect(screen.getByText('abc1234')).toBeInTheDocument()
    expect(screen.getByText('1.5 ms')).toBeInTheDocument()
    expect(screen.getByText('Reachable')).toBeInTheDocument()
    expect(screen.getByText('updated just now')).toBeInTheDocument()
  })

  it('renders an unknown quota as not visible, never as unlimited', async () => {
    const status = healthy()
    status.cpu = {
      available: true,
      process_percent: 5,
      quota_cores: null,
      quota_state: 'unknown',
      period_us: null,
    }
    status.memory = {
      available: true,
      current_bytes: 100,
      limit_bytes: null,
      limit_state: 'unknown',
      percent_of_limit: null,
    }
    apiGet.mockResolvedValue(status)
    renderPage()

    expect((await screen.findAllByText(/not visible in this container/)).length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(/no quota — unlimited/)).toBeNull()
    expect(screen.queryByText('no limit set')).toBeNull()
  })

  it('shows an explicit unlimited cgroup as unlimited', async () => {
    const status = healthy()
    status.cpu = { available: true, process_percent: 5, quota_cores: null, quota_state: 'unlimited', period_us: null }
    status.memory = { available: true, current_bytes: 100, limit_bytes: null, limit_state: 'unlimited' }
    apiGet.mockResolvedValue(status)
    renderPage()

    await screen.findByText('5.0%')
    expect(screen.getByText(/no quota — unlimited/)).toBeInTheDocument()
    expect(screen.getByText(/no limit set/)).toBeInTheDocument()
  })

  it('renders the activity card with per-analysis elapsed time', async () => {
    apiGet.mockResolvedValue(healthy())
    renderPage()

    expect(await screen.findByText('nightly #7')).toBeInTheDocument()
    expect(screen.getByText('1h 0m')).toBeInTheDocument()
    expect(screen.getByText('Chat turns in flight')).toBeInTheDocument()
  })

  it('shows the database ratio placeholder and unavailable process fields', async () => {
    apiGet.mockResolvedValue(healthy())
    renderPage()

    expect(await screen.findByText('Compression ratio')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('Restarts')).toBeInTheDocument()
  })

  it('keeps one request in flight at a time', async () => {
    vi.useFakeTimers()
    try {
      let resolveFirst: (status: ServerStatus) => void = () => {}
      apiGet.mockReturnValue(
        new Promise<ServerStatus>((resolve) => {
          resolveFirst = resolve
        }),
      )
      renderPage()
      // Two polling turns pass while the first request is still open.
      await vi.advanceTimersByTimeAsync(65_000)
      expect(apiGet).toHaveBeenCalledTimes(1)

      resolveFirst(healthy())
      await vi.advanceTimersByTimeAsync(31_000)
      expect(apiGet).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
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

  it('renders the health monitor card with per-condition state', async () => {
    const status = healthy()
    status.health_monitor = {
      available: true,
      enabled: true,
      interval_seconds: 60,
      last_sample_ago_seconds: 12,
      conditions: [
        {
          key: 'health_down',
          label: 'Health down',
          state: 'firing',
          held_seconds: 420,
          threshold_seconds: 300,
          age_seconds: null,
          age_known: true,
        },
        {
          key: 'stuck_analysis',
          label: 'Analysis stuck',
          state: 'firing',
          held_seconds: 0,
          threshold_seconds: 7200,
          age_seconds: 7500,
          age_known: true,
        },
        {
          key: 'error_rate',
          label: 'Error rate',
          state: 'ok',
          held_seconds: null,
          threshold_seconds: 600,
          age_seconds: null,
          age_known: true,
        },
      ],
    }
    apiGet.mockResolvedValue(status)
    renderPage()

    const card = (await screen.findByText('Health monitor')).closest(
      'div.rounded-lg',
    ) as HTMLElement
    expect(within(card).getByText('Enabled')).toBeInTheDocument()
    expect(within(card).getByText('12s ago')).toBeInTheDocument()
    // A firing condition carries its label and held / threshold.
    expect(within(card).getByText('Health down')).toBeInTheDocument()
    expect(within(card).getAllByText('Firing')).toHaveLength(2)
    expect(within(card).getByText('7m 0s / 5m 0s')).toBeInTheDocument()
    // The stuck row shows its age, never "held 0s".
    expect(within(card).getByText('Analysis stuck')).toBeInTheDocument()
    expect(within(card).getByText('2h 5m / 2h 0m')).toBeInTheDocument()
    expect(within(card).queryByText(/^0s/)).toBeNull()
    // An ok row that has not held yet is the page's muted dash, not "not visible".
    const errorRow = within(card).getByText('Error rate').closest('div') as HTMLElement
    expect(within(errorRow).getByText('—')).toBeInTheDocument()
    expect(within(card).queryByText(/not visible in this container/)).toBeNull()
  })

  it('distinguishes an idle analysis, an unreadable age, and a fresh monitor', async () => {
    const status = healthy()
    const condition = {
      label: 'Analysis stuck',
      state: 'ok' as const,
      held_seconds: null,
      threshold_seconds: 7200,
      age_known: true,
    }
    status.health_monitor = {
      available: true,
      enabled: true,
      interval_seconds: 60,
      last_sample_ago_seconds: null,
      conditions: [
        { ...condition, key: 'stuck_analysis', age_seconds: null },
        { ...condition, key: 'error_rate', label: 'Error rate', age_seconds: null },
      ],
    }
    apiGet.mockResolvedValue(status)
    const { unmount } = renderPage()

    expect(await screen.findByText('not running')).toBeInTheDocument()
    expect(screen.getByText('not sampled yet')).toBeInTheDocument()
    expect(screen.queryByText('unknown')).toBeNull()
    unmount()

    const unreadable = healthy()
    unreadable.health_monitor = {
      available: true,
      enabled: true,
      interval_seconds: 60,
      last_sample_ago_seconds: 5,
      conditions: [
        {
          ...condition,
          key: 'stuck_analysis',
          age_seconds: null,
          age_known: false,
        },
      ],
    }
    apiGet.mockResolvedValue(unreadable)
    renderPage()
    expect(await screen.findByText('unknown')).toBeInTheDocument()
    expect(screen.queryByText('not running')).toBeNull()
  })

  it('renders a disabled monitor without condition rows, and no card when absent', async () => {
    const status = healthy()
    status.health_monitor = {
      available: true,
      enabled: false,
      interval_seconds: 60,
      last_sample_ago_seconds: null,
      conditions: [
        {
          key: 'health_down',
          label: 'Health down',
          state: 'ok',
          held_seconds: null,
          threshold_seconds: 300,
          age_seconds: null,
          age_known: true,
        },
      ],
    }
    apiGet.mockResolvedValue(status)
    const { unmount } = renderPage()

    expect(await screen.findByText('Disabled')).toBeInTheDocument()
    expect(screen.queryByText('Health down')).toBeNull()
    unmount()

    // The existing fixtures carry no block at all: no card, and no crash.
    apiGet.mockResolvedValue(healthy())
    renderPage()
    expect(await screen.findByRole('heading', { name: 'Server Status' })).toBeInTheDocument()
    expect(screen.queryByText('Health monitor')).toBeNull()
  })

  it('renders an unavailable monitor block with its reason', async () => {
    const status = healthy()
    status.health_monitor = {
      available: false,
      reason: 'probe timed out after 4s',
    }
    apiGet.mockResolvedValue(status)
    renderPage()

    expect(await screen.findByText(/probe timed out after 4s/)).toBeInTheDocument()
    expect(screen.queryByText('Disabled')).toBeNull()
  })
})
