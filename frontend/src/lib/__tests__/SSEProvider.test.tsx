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

/** Probes that stay in flight until the test says how the server answered. */
function deferredProbes() {
  const resolvers: Array<(status: number) => void> = []
  fetchMock.mockImplementation(
    () =>
      new Promise<Response>((resolve) =>
        resolvers.push((status) => resolve({ status } as Response)),
      ),
  )
  return {
    async resolveAll(status: number) {
      const pending = resolvers.splice(0)
      await act(async () => {
        for (const resolve of pending) resolve(status)
        await Promise.resolve()
      })
    },
  }
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

  it('reports the expiry on the first 401 probe, since a 401 is never retried', async () => {
    const source = await renderAndConnect()
    probe(401)

    await fail(source)

    expect(fetchMock).toHaveBeenCalledWith('/api/auth/me', { credentials: 'include' })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()
    expect(source.closed).toBe(true)

    // No reconnect: the 401 loop must stop rather than hammer the server.
    await fail(source, 3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(screen.getAllByText(EXPIRED_TEXT)).toHaveLength(1)
  })

  it('treats a 200 probe as transient and reconnects', async () => {
    const source = await renderAndConnect()
    probe(200)

    await fail(source)

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    // The browser will not retry a closed stream — the manager does it itself.
    expect(source.closed).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })
    expect(MockEventSource.instances).toHaveLength(2)
    expect(MockEventSource.instances[1].closed).toBe(false)
  })

  it('treats a network-error probe as transient and reconnects', async () => {
    const source = await renderAndConnect()
    probe('network')

    await fail(source)

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })
    expect(MockEventSource.instances).toHaveLength(2)
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
  /** Everything this tab posted, for asserting on the protocol. */
  sent: unknown[] = []
  constructor() {
    MockBroadcastChannel.instances.push(this)
  }
  postMessage(data: unknown) {
    this.sent.push(data)
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
    fetchMock.mockReset()
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('BroadcastChannel', MockBroadcastChannel)
    vi.stubGlobal('fetch', fetchMock)
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

  function posted(type: string) {
    return channel().sent.filter((m) => (m as { type?: string }).type === type)
  }

  it('promotes a follower after the leader goes silent for too long', async () => {
    renderTab()
    await settle()
    expect(openStreams()).toHaveLength(1)

    // Another tab claims leadership: this one follows, so it drops its own stream.
    hear({ type: 'leader', tabId: '0' })
    await settle()
    expect(openStreams()).toHaveLength(0)

    // Past the heartbeat timeout the follower assumes the leader is gone.
    await settle(13_000)

    const streams = openStreams()
    expect(streams).toHaveLength(1)
    expect(streams[0].url).toContain('/api/stream?topics=')
  })

  it('hands the expired session off from the leader exactly once', async () => {
    renderTab()
    await settle()
    const source = openStreams()[0]
    expect(source).toBeDefined()
    probe(401)
    const heartbeats = posted('leader').length

    await act(async () => {
      for (let i = 0; i < 3; i += 1) source.fail()
    })

    // One handoff on the channel, and this tab shows the banner too.
    expect(posted('session-expired')).toHaveLength(1)
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()
    expect(openStreams()).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // A dead leader stops claiming to be alive, and never re-handsoffs.
    expect(posted('leader')).toHaveLength(heartbeats)
    expect(posted('session-expired')).toHaveLength(1)
    expect(openStreams()).toHaveLength(0)
  })

  it('treats a channel message error as one dropped broadcast, not a lost channel', async () => {
    const onEvent = vi.fn()
    function Listener() {
      useSSE('navbar', { 'active-count': onEvent })
      return <div>listener</div>
    }

    render(
      <SSEProvider>
        <Listener />
      </SSEProvider>,
    )
    await settle()
    hear({ type: 'leader', tabId: '0' })
    await settle()
    expect(openStreams()).toHaveLength(0)
    const updates = posted('topics-update').length

    act(() => {
      channel().onmessageerror?.()
    })
    await settle()

    // No takeover on a single dropped message…
    expect(openStreams()).toHaveLength(0)
    // …and the tab is not muted: it still sends and still receives.
    hear({ type: 'topics-request' })
    expect(posted('topics-update')).toHaveLength(updates + 1)
    hear({ type: 'sse-event', name: 'navbar:active-count', data: '{"count":7}' })
    expect(onEvent).toHaveBeenCalledWith('{"count":7}')
  })

  it('never advertises leadership once the session expired', async () => {
    renderTab()
    await settle()
    const source = openStreams()[0]
    probe(401)

    await act(async () => {
      for (let i = 0; i < 3; i += 1) source.fail()
    })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()

    const heartbeats = posted('leader').length
    hear({ type: 'who-is-leader' })
    // A dead tab answers nothing, so a new tab will not cancel its election.
    expect(posted('leader')).toHaveLength(heartbeats)
  })

  it('does not expire when a successful open breaks the 401 run', async () => {
    renderTab()
    await settle()
    probe(401)

    for (let round = 0; round < 3; round += 1) {
      const source = MockEventSource.instances[MockEventSource.instances.length - 1]
      await act(async () => {
        source.fail()
        source.fail()
      })
      act(() => {
        source.open()
      })
      // Let the scheduled reconnect replace the closed stream.
      await settle(2_000)
    }

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    expect(openStreams()).toHaveLength(1)
  })

  it('discards 401 probes that were in flight when the stream reopened', async () => {
    renderTab()
    await settle()
    const source = openStreams()[0]
    const probes = deferredProbes()

    // Three probes in flight, then a successful open — they were measuring a
    // connection that no longer exists, so their 401s count for nothing.
    await act(async () => {
      source.fail()
      source.fail()
      source.fail()
    })
    act(() => {
      source.open()
    })
    await probes.resolveAll(401)

    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
    // And the retries keep going instead of the tab going quiet.
    const before = MockEventSource.instances.length
    await settle(5_000)
    expect(MockEventSource.instances.length).toBeGreaterThan(before)
  })

  it('shows the banner on a follower that hears of the expiry and opens no stream', async () => {
    renderTab()
    await settle()
    hear({ type: 'leader', tabId: '0' })
    await settle()
    expect(openStreams()).toHaveLength(0)

    hear({ type: 'session-expired' })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    // No promotion into the dead session, and no second banner.
    expect(openStreams()).toHaveLength(0)
    expect(screen.getAllByText(EXPIRED_TEXT)).toHaveLength(1)
  })

  it('keeps heartbeating and relaying events to a follower on a healthy session', async () => {
    const onEvent = vi.fn()
    function Listener() {
      useSSE('navbar', { 'active-count': onEvent })
      return <div>listener</div>
    }

    render(
      <SSEProvider>
        <Listener />
      </SSEProvider>,
    )
    await settle()
    expect(posted('leader')).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(posted('leader').length).toBeGreaterThan(1)
    expect(posted('session-expired')).toHaveLength(0)

    // The other tab keeps leadership; this one gets its events off the channel.
    hear({ type: 'leader', tabId: '0' })
    await settle()
    hear({ type: 'sse-event', name: 'navbar:active-count', data: '{"count":3}' })
    expect(onEvent).toHaveBeenCalledWith('{"count":3}')
  })
})

/**
 * jsdom has no navigator.locks, so the tests above elect through the channel.
 * This one hands the test the moment a queued lock request would be granted.
 */
class MockLocks {
  requests: Array<{ rejected: boolean; grant: () => void }> = []
  request = (
    _name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<void>,
  ) =>
    new Promise<void>((resolve, reject) => {
      const entry = {
        rejected: false,
        grant: () => {
          void callback().then(resolve, reject)
        },
      }
      options.signal?.addEventListener('abort', () => {
        entry.rejected = true
        reject(new DOMException('Aborted', 'AbortError'))
      })
      this.requests.push(entry)
    })
}

describe('SSEProvider leader lock', () => {
  let locks: MockLocks

  beforeEach(() => {
    vi.useFakeTimers()
    MockEventSource.instances = []
    MockBroadcastChannel.instances = []
    fetchMock.mockReset()
    locks = new MockLocks()
    Object.defineProperty(navigator, 'locks', { value: locks, configurable: true })
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('BroadcastChannel', MockBroadcastChannel)
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    delete (navigator as { locks?: unknown }).locks
  })

  it('never lets an expired follower take the lock it is queued for', async () => {
    render(
      <SSEProvider>
        <Subscriber />
      </SSEProvider>,
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })
    // The lock is held by another tab: this one waits, no stream yet.
    expect(locks.requests).toHaveLength(1)
    expect(openStreams()).toHaveLength(0)

    act(() => {
      MockBroadcastChannel.instances[0].onmessage?.({ data: { type: 'session-expired' } } as MessageEvent)
    })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()
    // The queued request is cancelled instead of waiting to promote a dead tab.
    expect(locks.requests[0].rejected).toBe(true)

    // Even if the browser grants it late, a dead tab must not lead.
    act(() => {
      locks.requests[0].grant()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })
    const sent = MockBroadcastChannel.instances[0].sent
    expect(sent.filter((m) => (m as { type?: string }).type === 'leader')).toHaveLength(0)
    expect(openStreams()).toHaveLength(0)
  })
})
