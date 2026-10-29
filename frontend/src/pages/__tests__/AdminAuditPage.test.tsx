import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminAuditPage } from '@/pages/AdminAuditPage'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

const ACTIONS = [
  { action: 'settings.update', label: 'Settings updated', count: 2, last_seen: '2024-01-02 10:00:00' },
  { action: 'user.role.change', label: 'Role changed', count: 1, last_seen: '2024-01-02 10:00:00' },
]

function event(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    created_at: '2024-01-02 10:00:00',
    actor: 'alice',
    actor_role: 'admin',
    is_admin: true,
    action: 'settings.update',
    target_type: 'setting',
    target_id: 'ai_call_timeout',
    outcome: 'success',
    details: { key: 'ai_call_timeout', changed: true },
    request_ip: '10.0.0.7',
    ...overrides,
  }
}

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private listeners = new Map<string, Array<(e: MessageEvent) => void>>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (e: MessageEvent) => void) {
    const current = this.listeners.get(name) ?? []
    this.listeners.set(name, [...current, handler])
  }

  removeEventListener(name: string, handler: (e: MessageEvent) => void) {
    this.listeners.set(
      name,
      (this.listeners.get(name) ?? []).filter((h) => h !== handler),
    )
  }

  close() {
    this.closed = true
  }

  emit(name: string, data: unknown) {
    for (const handler of this.listeners.get(name) ?? []) {
      handler(new MessageEvent(name, { data: JSON.stringify(data) }))
    }
  }
}

/** Serve one page of events for whatever offset the page asks for. */
function mockAuditApi(total: number, build: (offset: number) => unknown[] = () => [event()]) {
  apiGet.mockImplementation((path: string) => {
    if (path.startsWith('/api/admin/audit/actions')) {
      return Promise.resolve({ actions: ACTIONS })
    }
    const offset = Number(new URL(path, 'http://x').searchParams.get('offset') ?? 0)
    const events = offset < total ? build(offset) : []
    return Promise.resolve({ events, total, limit: 50, offset })
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AdminAuditPage />
    </MemoryRouter>,
  )
}

describe('AdminAuditPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    MockEventSource.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('renders rows with actor, action label, target and outcome', async () => {
    mockAuditApi(1)
    renderPage()

    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(screen.getByText('Settings updated')).toBeInTheDocument()
    expect(screen.getByText('setting:ai_call_timeout')).toBeInTheDocument()
    expect(screen.getByText('success')).toBeInTheDocument()
    expect(screen.getByText('admin')).toBeInTheDocument()
  })

  it('shows a load error using the API detail', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new Error('500: audit table unavailable')),
    )
    renderPage()

    expect(await screen.findByText('500: audit table unavailable')).toBeInTheDocument()
  })

  it('shows the empty state', async () => {
    mockAuditApi(0, () => [])
    renderPage()

    expect(await screen.findByText('No audit events yet')).toBeInTheDocument()
    expect(screen.getByText('Showing 0–0 of 0')).toBeInTheDocument()
  })

  it('expands a row to show the details JSON and request_ip', async () => {
    mockAuditApi(1)
    renderPage()

    fireEvent.click(await screen.findByText('alice'))

    expect(await screen.findByText(/"ai_call_timeout"/)).toBeInTheDocument()
    expect(screen.getByText('request_ip: 10.0.0.7')).toBeInTheDocument()
  })

  it('debounces text filters and resets the offset', async () => {
    vi.useFakeTimers()
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // Move to page 2 first so the filter change has an offset to reset.
    fireEvent.click(screen.getByText('Next'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50')

    apiGet.mockClear()
    fireEvent.change(screen.getByLabelText('Actor filter'), { target: { value: 'alice' } })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(299)
    })
    expect(apiGet).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0&actor=alice')
  })

  it('pages with prev/next and reports the visible range', async () => {
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()

    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(await screen.findByText('Showing 51–51 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Previous'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0'),
    )
    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()
  })

  it('subscribes the live tail, prepends, and closes on toggle-off and unmount', async () => {
    mockAuditApi(1)
    const { unmount } = renderPage()
    await screen.findByText('alice')
    expect(MockEventSource.instances).toHaveLength(0)

    const liveSwitch = screen.getByRole('switch', { name: 'Live tail' })
    fireEvent.click(liveSwitch)
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe('/api/admin/audit/stream')

    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('audit:event', event({ id: 2, actor: 'bob', action: 'user.role.change' }))
    })

    expect(await screen.findByText('bob')).toBeInTheDocument()
    expect(screen.getByText('Role changed')).toBeInTheDocument()
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument()

    fireEvent.click(liveSwitch)
    expect(source.closed).toBe(true)

    fireEvent.click(liveSwitch)
    const second = MockEventSource.instances[1]
    unmount()
    expect(second.closed).toBe(true)
  })
})
