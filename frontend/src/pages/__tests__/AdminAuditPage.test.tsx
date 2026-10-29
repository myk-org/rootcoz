import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { MemoryRouter, RouterProvider, createMemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminAuditPage } from '@/pages/AdminAuditPage'
import { ApiError } from '@/lib/api'
import { clearTokens, clearUsername, getGithubToken, getUsername, setGithubToken, setUsername } from '@/lib/cookies'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

const logout = vi.hoisted(() => vi.fn<() => Promise<void>>())
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ logout }) }))

// Radix Select opens its listbox through pointer capture, which jsdom lacks.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

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

/** Serve one page of events for whatever limit/offset the page asks for. */
function mockAuditApi(
  total: number,
  build: (offset: number, limit: number) => unknown[] = () => [event()],
) {
  apiGet.mockImplementation((path: string) => {
    if (path.startsWith('/api/admin/audit/actions')) {
      return Promise.resolve({ actions: ACTIONS })
    }
    const params = new URL(path, 'http://x').searchParams
    const offset = Number(params.get('offset') ?? 0)
    const limit = Number(params.get('limit') ?? 50)
    const events = offset < total ? build(offset, limit) : []
    return Promise.resolve({ events, total, limit, offset })
  })
}

/** One event per row, so the range text reflects the requested page size. */
function pageOfEvents(total: number) {
  return (offset: number, limit: number) =>
    Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) =>
      event({ id: offset + i + 1 }),
    )
}

function renderPage(initialEntry = '/admin/audit') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AdminAuditPage />
      <LocationProbe />
    </MemoryRouter>,
  )
}

/** A router whose history stack the test can walk, like a real Back button. */
function renderRouted(initialEntries: string[], initialIndex?: number) {
  const router = createMemoryRouter(
    [
      {
        path: '/admin/audit',
        element: (
          <>
            <AdminAuditPage />
            <LocationProbe />
          </>
        ),
      },
      // The layout has this route; the tests only need somewhere to land.
      { path: '/login', element: <LocationProbe /> },
    ],
    { initialEntries, initialIndex },
  )
  const view = render(<RouterProvider router={router} />)
  return { router, ...view }
}

/** Every location the page has rendered, to catch stray history entries. */
let locationKeys: string[] = []

/** The page owns the query string, so the URL assertions read it from the router. */
function LocationProbe() {
  const location = useLocation()
  useEffect(() => {
    locationKeys.push(location.key)
  }, [location.key])
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

function liveSwitch() {
  return screen.getByRole('switch', { name: /Live tail/ })
}

function liveDot() {
  return screen.getByTestId('live-dot')
}

/** Tooltip text for the current live phase (Radix renders it on hover). */
async function liveTooltip(): Promise<string> {
  await userEvent.hover(screen.getByTestId('live-status'))
  return (await screen.findByRole('tooltip')).textContent ?? ''
}

/** Type in the actor box and wait for the debounced request it produces. */
async function setActor(value: string, offset = 0) {
  fireEvent.change(screen.getByLabelText('Actor filter'), { target: { value } })
  const suffix = value ? `&actor=${value}` : ''
  await waitFor(() =>
    expect(apiGet).toHaveBeenLastCalledWith(`/api/admin/audit?limit=50&offset=${offset}${suffix}`),
  )
}

function enableLive() {
  const liveSwitch = screen.getByRole('switch', { name: /Live tail/ })
  fireEvent.click(liveSwitch)
  return { liveSwitch, source: MockEventSource.instances[MockEventSource.instances.length - 1] }
}
describe('AdminAuditPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    logout.mockReset().mockResolvedValue(undefined)
    MockEventSource.instances = []
    locationKeys = []
    clearUsername()
    clearTokens()
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
    const { container } = renderPage()

    expect(await screen.findByText('500: audit table unavailable')).toBeInTheDocument()
    // A 401-only affordance: nothing to sign in to for a plain server error.
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
    // A failed request is not an outstanding one: no skeleton, no "empty" claim.
    expect(container.querySelector('.animate-pulse')).toBeNull()
    expect(screen.queryByText('No audit events yet')).not.toBeInTheDocument()
  })

  it('offers a re-login path when the list request is rejected with 401', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    renderPage()

    expect(await screen.findByText(/session expired — sign in again/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('ends the session before showing the login form on a 401', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    const { router } = renderRouted(['/admin/audit'])

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))

    // Logging out first is what keeps RegisterPage from bouncing to the dashboard.
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
    expect(logout).toHaveBeenCalledTimes(1)
  })

  it('still clears the session and reaches the login form when logout rejects', async () => {
    logout.mockRejectedValueOnce(new Error('session already gone'))
    setUsername('alice')
    setGithubToken('gh-token')
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    const { router } = renderRouted(['/admin/audit'])

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
    expect(logout).toHaveBeenCalledTimes(1)
    expect(getUsername()).toBe('')
    expect(getGithubToken()).toBe('')
  })

  it('shows the plain error and no sign-in action for a 500', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(500, 'Internal Server Error', { detail: 'audit table unavailable' })),
    )
    renderPage()

    expect(await screen.findByText('audit table unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
    expect(screen.queryByText(/sign in again/i)).not.toBeInTheDocument()
  })

  it('labels the time column as UTC with the explanation in its tooltip', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const marker = screen.getByTestId('time-basis')
    expect(marker).toHaveTextContent('Time UTC')
    // The explanation is on the marker itself, not on every row.
    expect(marker.getAttribute('aria-label')).toContain('date filters are UTC')
    await userEvent.hover(marker)
    expect((await screen.findByRole('tooltip')).textContent).toContain('date filters are UTC')
  })

  it('restores filters from the URL and mirrors filter edits back into it', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?actor=alice&outcome=failure')

    // The shared link's filters reach the first request and the inputs.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        '/api/admin/audit?limit=50&offset=0&actor=alice&outcome=failure',
      ),
    )
    expect(screen.getByLabelText('Actor filter')).toHaveValue('alice')
    expect(screen.getByRole('combobox', { name: 'Outcome filter' })).toHaveTextContent('failure')

    fireEvent.change(screen.getByLabelText('Since date'), { target: { value: '2024-01-01' } })
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('since=2024-01-01'),
    )
    expect(screen.getByTestId('location')).toHaveTextContent('actor=alice')
    expect(screen.getByTestId('location')).toHaveTextContent('outcome=failure')

    // Clearing drops the filter params instead of storing empty values.
    fireEvent.click(screen.getByText('Clear'))
    await waitFor(() => expect(screen.getByTestId('location')).not.toHaveTextContent('actor='))
    expect(screen.getByTestId('location')).not.toHaveTextContent('outcome=')
    expect(screen.getByTestId('location')).not.toHaveTextContent('since=')
  })

  it('adopts a URL the browser navigates back to while the page stays mounted', async () => {
    mockAuditApi(120, pageOfEvents(120))
    // An earlier entry with other filters and a shared link on top of it.
    const { router } = renderRouted(
      ['/admin/audit?actor=bob&outcome=failure&limit=100&live=1', '/admin/audit?actor=alice'],
      1,
    )

    expect(await screen.findByText('Showing 1–50 of 120')).toBeInTheDocument()
    expect(screen.getByLabelText('Actor filter')).toHaveValue('alice')
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0&actor=alice'),
    )

    // Back: the component is not remounted, only the query changes.
    await act(async () => {
      await router.navigate(-1)
    })

    expect(screen.getByLabelText('Actor filter')).toHaveValue('bob')
    expect(screen.getByRole('combobox', { name: 'Outcome filter' })).toHaveTextContent('failure')
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('100')
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    // The next request asks for what the URL says, from the first page.
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith(
        '/api/admin/audit?limit=100&offset=0&actor=bob&outcome=failure',
      ),
    )
    // Adopting the URL rewrites nothing: no pushed entry and no replace either.
    expect(router.state.historyAction).toBe('POP')
    expect(locationKeys).toHaveLength(2)
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

  it('sends the default page size of 50 and shows it in the rows-per-page control', async () => {
    mockAuditApi(1)
    renderPage()

    await screen.findByText('alice')
    expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0')
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('50')
  })

  it('changes the page size to 100, resets the offset and persists ?limit=100', async () => {
    mockAuditApi(120, pageOfEvents(120))
    renderPage('/admin/audit?q=alice')

    expect(await screen.findByText('Showing 1–50 of 120')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50&q=alice'),
    )

    const user = userEvent.setup()
    await user.click(screen.getByRole('combobox', { name: 'Rows per page' }))
    await user.click(screen.getByRole('option', { name: '100' }))

    // Offset back to the first page, refetched with the new page size.
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=100&offset=0&q=alice'),
    )
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice&limit=100')
    expect(await screen.findByText('Showing 1–100 of 120')).toBeInTheDocument()
  })

  it('honours ?limit=250 on mount and ignores an invalid ?limit=9999', async () => {
    mockAuditApi(120, pageOfEvents(120))
    const { unmount } = renderPage('/admin/audit?limit=250')

    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=250&offset=0'))
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('250')
    unmount()

    apiGet.mockClear()
    renderPage('/admin/audit?limit=9999')

    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0'))
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('50')
  })

  it('subscribes the live tail, prepends, and closes on toggle-off and unmount', async () => {
    mockAuditApi(1)
    const { unmount } = renderPage()
    await screen.findByText('alice')
    expect(MockEventSource.instances).toHaveLength(0)

    const { liveSwitch, source } = enableLive()
    expect(MockEventSource.instances).toHaveLength(1)
    expect(source.url).toBe('/api/admin/audit/stream')
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

  it('clears the error banner when the same query succeeds after a failure', async () => {
    let failed = false
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/admin/audit/actions')) return Promise.resolve({ actions: ACTIONS })
      if (path.includes('actor=alice')) {
        return Promise.resolve({
          events: [event({ id: 2, actor: 'bob', action: 'user.role.change' })],
          total: 1,
          limit: 50,
          offset: 0,
        })
      }
      if (!failed) {
        failed = true
        return Promise.reject(new Error('500: audit table unavailable'))
      }
      return Promise.resolve({ events: [event()], total: 1, limit: 50, offset: 0 })
    })
    renderPage()

    expect(await screen.findByText('500: audit table unavailable')).toBeInTheDocument()

    await setActor('alice')
    expect(await screen.findByText('Role changed')).toBeInTheDocument()

    // Back to the query that failed before: its banner must not come back.
    await setActor('')
    expect(await screen.findByText('Settings updated')).toBeInTheDocument()
    expect(screen.queryByText('500: audit table unavailable')).not.toBeInTheDocument()
  })

  it('ignores a streamed event that does not match the active filters', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    await setActor('bob')
    const { source } = enableLive()

    act(() => {
      source.emit('audit:event', event({ id: 9, action: 'user.role.change' }))
    })

    expect(screen.queryByText('Role changed')).not.toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(screen.getByText('Showing 1–1 of 1')).toBeInTheDocument()
  })

  it('counts but does not insert a matching streamed event on a later page', async () => {
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()
    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(await screen.findByText('Showing 51–51 of 120')).toBeInTheDocument()

    const { source } = enableLive()
    act(() => {
      source.emit('audit:event', event({ id: 5000, actor: 'bob', action: 'user.role.change' }))
    })

    expect(screen.queryByText('Role changed')).not.toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(screen.getByText('Showing 51–51 of 121')).toBeInTheDocument()
  })

  it('restores the tail from ?live=1 and starts out connecting', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?live=1')

    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe('/api/admin/audit/stream')
    expect(liveDot()).toHaveClass('bg-signal-amber')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Connecting'))
    expect(await liveTooltip()).toContain('Connecting to the live audit stream')
  })

  it('turns green when the stream opens', async () => {
    mockAuditApi(1)
    renderPage()
    const { source } = enableLive()

    act(() => {
      source.onopen?.()
    })

    expect(liveDot()).toHaveClass('animate-pulse', 'bg-signal-green')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Receiving live audit events'))
    expect(await liveTooltip()).toContain('Receiving live audit events')
  })

  it('switches the tail off on error and leaves the failure visible without retrying', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice&live=1')
    const source = MockEventSource.instances[0]

    act(() => {
      source.onerror?.()
    })

    expect(source.closed).toBe(true)
    // Switch off, ?live=1 gone, other params kept — the admin re-enables it.
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice')
    expect(liveDot()).toHaveClass('bg-signal-red')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live stream failed'))
    expect(await liveTooltip()).toContain('switch Live on to try again')
    // Dot + label + tooltip are the only signal: nothing inline beside the switch.
    expect(screen.getByTestId('live-status').textContent).toBe('')
    // No automatic reconnect.
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('creates exactly one new stream when the admin enables it after a failure', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?live=1')

    act(() => {
      MockEventSource.instances[0].onerror?.()
    })
    expect(MockEventSource.instances).toHaveLength(1)

    fireEvent.click(liveSwitch())

    expect(MockEventSource.instances).toHaveLength(2)
    expect(MockEventSource.instances[1].url).toBe('/api/admin/audit/stream')
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Connecting'))
    expect(liveDot()).toHaveClass('bg-signal-amber')
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?live=1')
  })

  it('writes live=1 on and removes it off while preserving other params', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice')

    fireEvent.click(liveSwitch())
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice&live=1')

    fireEvent.click(liveSwitch())
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice')
    expect(screen.getByTestId('location')).not.toHaveTextContent('live=1')
  })

  it('does not offer expansion for a row with no details', async () => {
    mockAuditApi(1, () => [event({ details: {} })])
    renderPage()

    const row = (await screen.findByText('No details recorded')).closest('tr')
    expect(row).toBeTruthy()
    expect(row!).not.toHaveAttribute('aria-expanded')
    expect(row!).not.toHaveClass('cursor-pointer')
  })

  it('renders label, dot and switch tightly together and greys the dot when off', async () => {
    mockAuditApi(1)
    renderPage()
    const { source } = enableLive()

    // No text label: dot + tooltip carry the state, so nothing can shift.
    expect(screen.queryByTestId('live-label')).toBeNull()
    expect(liveDot().nextElementSibling).toHaveAttribute('role', 'switch')

    act(() => {
      source.onopen?.()
    })
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Receiving live audit events'))

    act(() => {
      source.onerror?.()
    })
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live stream failed'))

    fireEvent.click(liveSwitch())
    fireEvent.click(liveSwitch())
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live tail is off'))
    expect(liveDot()).toHaveClass('bg-border-default')
    expect(await liveTooltip()).toContain('Live tail is off')
  })

  it('shows how many events the stream dropped when audit-gap arrives', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })

    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()
    // The stream is still healthy — nothing else about the page changes.
    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
  })

  it('clears the gap notice and refetches the current view when Refresh is clicked', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice&offset=0')
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    apiGet.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    // Same filters, same offset: it refreshes the view instead of resetting it.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0&q=alice'),
    )
    await waitFor(() => expect(screen.queryByText(/Missed 3 live events/)).toBeNull())
  })

  it('shows no gap notice when only normal events arrive', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit:event', event({ id: 2, actor: 'bob', action: 'user.role.change' }))
    })

    expect(await screen.findByText('bob')).toBeInTheDocument()
    expect(screen.queryByText(/Missed \d+ live/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
  })

  it('adds up the count across gap frames instead of showing only the last batch', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    act(() => {
      source.emit('audit-gap', { dropped: 5 })
    })

    // Each frame reports only its own drops: 3 + 5 missed since the refresh.
    expect(await screen.findByText(/Missed 8 live events/)).toBeInTheDocument()
    expect(screen.queryByText(/Missed 3 live events/)).toBeNull()
    expect(screen.queryByText(/Missed 5 live events/)).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Refresh' })).toHaveLength(1)
  })

  it('starts the gap count over after a refresh', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByText(/Missed \d+ live/)).toBeNull())

    act(() => {
      source.emit('audit-gap', { dropped: 2 })
    })

    // The refreshed page is whole again: only the new gap counts.
    expect(await screen.findByText(/Missed 2 live events/)).toBeInTheDocument()
  })

  it('shows a single gap frame with its own count', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 1 })
    })

    expect(await screen.findByText(/Missed 1 live event since the last refresh/)).toBeInTheDocument()
  })
})
