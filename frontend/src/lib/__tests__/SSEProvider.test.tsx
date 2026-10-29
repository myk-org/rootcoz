import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SSEProvider } from '@/lib/SSEProvider'
import { useSSE } from '@/lib/useSSE'

vi.mock('@/lib/auth', () => ({ useAuth: () => ({ authenticated: true }) }))

const EXPIRED_TEXT = /session expired/i

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.closed = true
  }
  open() {
    this.onopen?.()
  }
  fail() {
    this.onerror?.()
  }
}

/** The manager only opens a stream once something subscribes. */
function Subscriber() {
  useSSE('navbar', { 'active-count': () => {} })
  return <div>page content</div>
}

const fetchMock = vi.fn()

/** The guard asks /api/auth/me whether the session survived; the answer decides. */
function probe(status: number | 'network') {
  fetchMock.mockImplementation(() =>
    status === 'network'
      ? Promise.reject(new TypeError('Failed to fetch'))
      : Promise.resolve({ status } as Response),
  )
}

describe('SSEProvider session expiry', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    MockEventSource.instances = []
    fetchMock.mockReset()
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('fetch', fetchMock)
    // No BroadcastChannel → the manager every non-multiplexed environment gets.
    vi.stubGlobal('BroadcastChannel', undefined)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  async function renderAndConnect() {
    render(
      <SSEProvider>
        <Subscriber />
      </SSEProvider>,
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })
    expect(MockEventSource.instances).toHaveLength(1)
    return MockEventSource.instances[0]
  }

  /** A generic error carries no status, so the guard probes the session first. */
  async function fail(source: MockEventSource, times = 1) {
    await act(async () => {
      for (let i = 0; i < times; i += 1) source.fail()
    })
  }

  it('surfaces the expired session after three 401 probes and stops reconnecting', async () => {
    const source = await renderAndConnect()
    probe(401)

    await fail(source, 2)
    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    expect(source.closed).toBe(false)

    await fail(source)
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/me', { credentials: 'include' })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()
    expect(source.closed).toBe(true)

    // No new stream: the 401 loop must stop rather than hammer the server.
    await fail(source, 3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(screen.getAllByText(EXPIRED_TEXT)).toHaveLength(1)
  })

  it('treats a 200 probe as transient and keeps retrying', async () => {
    const source = await renderAndConnect()
    probe(200)

    await fail(source, 5)

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    // The stream was never given up on — the browser keeps reconnecting it.
    expect(source.closed).toBe(false)
  })

  it('treats a network-error probe as transient', async () => {
    const source = await renderAndConnect()
    probe('network')

    await fail(source, 5)

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    expect(source.closed).toBe(false)
  })

  it('does not expire when a successful connection breaks the 401 run', async () => {
    const source = await renderAndConnect()
    probe(401)

    await fail(source, 2)
    act(() => {
      source.open()
    })
    await fail(source, 2)
    act(() => {
      source.open()
    })
    await fail(source, 2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
  })
})

/**
 * Two tabs share one stream through a BroadcastChannel. The mock delivers in a
 * later task, like the real API — delivering inside postMessage would reorder
 * the leader election.
 */
class MockBroadcastChannel {
  static instances: MockBroadcastChannel[] = []
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onmessageerror: (() => void) | null = null
  closed = false
  constructor() {
    MockBroadcastChannel.instances.push(this)
  }
  postMessage(data: unknown) {
    queueMicrotask(() => {
      for (const other of MockBroadcastChannel.instances) {
        if (other !== this && !other.closed) other.onmessage?.({ data } as MessageEvent)
      }
    })
  }
  close() {
    this.closed = true
    this.onclose?.()
  }
}

function openStreams() {
  return MockEventSource.instances.filter((source) => !source.closed)
}

describe('SSEProvider follower promotion', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    MockEventSource.instances = []
    MockBroadcastChannel.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('BroadcastChannel', MockBroadcastChannel)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  function renderTab() {
    return render(
      <SSEProvider>
        <Subscriber />
      </SSEProvider>,
    )
  }

  /** Past the 150–250ms election window. */
  async function settle(ms = 400) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })
  }

  function channel() {
    return MockBroadcastChannel.instances[0]
  }

  function hear(message: unknown) {
    act(() => {
      channel().onmessage?.({ data: message } as MessageEvent)
    })
  }

  it('promotes a follower whose channel dies and never duplicates a healthy stream', async () => {
    renderTab()
    await settle()
    expect(openStreams()).toHaveLength(1)

    // Another tab claims leadership: this one follows, so it drops its own stream.
    hear({ type: 'leader', tabId: '0' })
    await settle()
    expect(openStreams()).toHaveLength(0)

    // Channel gone — nothing can arrive any more, so the tab takes over itself.
    act(() => {
      channel().close()
    })
    await settle()

    const streams = openStreams()
    expect(streams).toHaveLength(1)
    expect(streams[0].url).toContain('/api/stream?topics=')
  })
})
