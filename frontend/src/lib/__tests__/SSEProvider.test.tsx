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

describe('SSEProvider session expiry', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    MockEventSource.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
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

  it('surfaces the expired session once and stops reconnecting', async () => {
    const source = await renderAndConnect()

    act(() => {
      source.fail()
      source.fail()
    })
    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()

    act(() => {
      source.fail()
    })
    expect(screen.getByText(EXPIRED_TEXT)).toBeInTheDocument()
    expect(source.closed).toBe(true)

    // No new stream: the 401 loop must stop rather than hammer the server.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(screen.getAllByText(EXPIRED_TEXT)).toHaveLength(1)
  })

  it('does not expire when a successful connection breaks the failure run', async () => {
    const source = await renderAndConnect()

    act(() => {
      source.open()
      source.fail()
      source.fail()
    })
    act(() => {
      source.open()
      source.fail()
      source.fail()
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(screen.queryByText(EXPIRED_TEXT)).not.toBeInTheDocument()
  })
})
