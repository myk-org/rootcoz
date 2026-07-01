import { useEffect, useRef } from 'react'

/**
 * Unique per-tab identifier for leader election.
 * Stable across the tab's lifetime.
 */
const TAB_ID = `${Date.now()}-${Math.random().toString(36).slice(2)}`

/** Election timeout base (ms). Random jitter is added to avoid ties. */
const ELECTION_TIMEOUT_MS = 150

/** Maximum backoff for reconnecting a CLOSED EventSource. */
const MAX_RECONNECT_DELAY_MS = 30_000

/**
 * Messages exchanged over BroadcastChannel for the SSE sharing protocol.
 *
 * Leader election:
 *   who-is-leader → leader responds with 'leader'
 *   No response within timeout → caller becomes leader
 *
 * Event relay:
 *   Leader forwards SSE events as 'sse-event' messages
 *
 * Leader shutdown:
 *   leader-down → triggers re-election among followers
 */
interface ChannelMessage {
  type: 'who-is-leader' | 'leader' | 'leader-down' | 'sse-event' | 'sse-open' | 'sse-error'
  tabId?: string
  /** SSE event name (for sse-event). */
  name?: string
  /** SSE event data string (for sse-event). */
  data?: string
  /** Whether EventSource.readyState is CLOSED (for sse-error). */
  closed?: boolean
}

interface UseSharedSSEOptions {
  /** SSE endpoint URL. Pass `null` to disable the connection. */
  url: string | null
  /**
   * Map of SSE event names to callbacks.
   * Each callback receives the raw `event.data` string.
   */
  events: Record<string, (data: string) => void>
  /** Called when the underlying EventSource fires an error. */
  onError?: (closed: boolean) => void
  /** Called when the underlying EventSource opens (or reconnects). */
  onOpen?: () => void
}

/**
 * Share SSE connections across browser tabs via BroadcastChannel.
 *
 * Browsers enforce a 6 concurrent HTTP/1.1 connection limit per domain.
 * Each SSE stream holds one slot permanently. With multiple tabs open,
 * all slots are exhausted and the app hangs.
 *
 * This hook elects one tab as the "leader" that owns the actual
 * `EventSource`. The leader relays events to all other tabs ("followers")
 * via `BroadcastChannel`. If the leader tab closes, a follower takes over.
 *
 * Falls back to a direct `EventSource` when `BroadcastChannel` is
 * unavailable (e.g., in test environments).
 *
 * @example
 * useSharedSSE({
 *   url: '/api/navbar/stream',
 *   events: {
 *     'active-count': (data) => setActiveCount(parseInt(data, 10)),
 *     'unread-count': (data) => setUnreadCount(parseInt(data, 10)),
 *   },
 * })
 */
export function useSharedSSE({ url, events, onError, onOpen }: UseSharedSSEOptions): void {
  // Keep callbacks in refs so the effect doesn't re-run on every render.
  // The callbacks are always read from .current when invoked, ensuring
  // the latest closure is used.
  const eventsRef = useRef(events)
  eventsRef.current = events
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  const onOpenRef = useRef(onOpen)
  onOpenRef.current = onOpen

  useEffect(() => {
    if (!url) return

    // Fallback for environments without BroadcastChannel (e.g., JSDOM tests)
    if (typeof BroadcastChannel === 'undefined') {
      const eventSource = new EventSource(url)
      for (const name of Object.keys(eventsRef.current)) {
        eventSource.addEventListener(name, (e: Event) => {
          eventsRef.current[name]?.((e as MessageEvent).data)
        })
      }
      eventSource.onopen = () => onOpenRef.current?.()
      eventSource.onerror = () => {
        onErrorRef.current?.(eventSource.readyState === EventSource.CLOSED)
      }
      return () => eventSource.close()
    }

    // ---- Shared SSE via BroadcastChannel ----

    const channelName = `rootcoz-sse:${url}`
    const channel = new BroadcastChannel(channelName)
    const eventNames = Object.keys(eventsRef.current)

    let isLeader = false
    let eventSource: EventSource | null = null
    let electionTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectDelay = 1000
    let destroyed = false

    // ---- Leader: open EventSource and relay events ----

    function createEventSource() {
      if (destroyed) return
      eventSource?.close()
      eventSource = new EventSource(url!)

      for (const name of eventNames) {
        eventSource.addEventListener(name, (e: Event) => {
          const data = (e as MessageEvent).data as string
          channel.postMessage({ type: 'sse-event', name, data })
          eventsRef.current[name]?.(data)
        })
      }

      eventSource.onopen = () => {
        reconnectDelay = 1000
        channel.postMessage({ type: 'sse-open' })
        onOpenRef.current?.()
      }

      eventSource.onerror = () => {
        const closed = eventSource?.readyState === EventSource.CLOSED
        channel.postMessage({ type: 'sse-error', closed })
        onErrorRef.current?.(closed)

        if (closed && !destroyed) {
          // Browser gave up auto-reconnect — retry with exponential backoff
          reconnectTimer = setTimeout(() => {
            if (!destroyed && isLeader) createEventSource()
          }, reconnectDelay)
          reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
        }
      }
    }

    function becomeLeader() {
      if (destroyed || isLeader) return
      isLeader = true
      channel.postMessage({ type: 'leader', tabId: TAB_ID })
      createEventSource()
    }

    // ---- Election ----

    function startElection() {
      if (destroyed) return
      channel.postMessage({ type: 'who-is-leader' })
      // Random jitter avoids ties when multiple tabs open simultaneously
      const jitter = Math.random() * 100
      electionTimer = setTimeout(() => {
        if (!destroyed) becomeLeader()
      }, ELECTION_TIMEOUT_MS + jitter)
    }

    // ---- Channel message handler ----

    channel.onmessage = (msg: MessageEvent<ChannelMessage>) => {
      const { type, name, data, closed } = msg.data

      switch (type) {
        case 'who-is-leader':
          if (isLeader) channel.postMessage({ type: 'leader', tabId: TAB_ID })
          break

        case 'leader':
          // Another tab is leader — cancel our election
          if (electionTimer) {
            clearTimeout(electionTimer)
            electionTimer = null
          }
          break

        case 'sse-event':
          if (!isLeader && name) eventsRef.current[name]?.(data ?? '')
          break

        case 'sse-open':
          if (!isLeader) onOpenRef.current?.()
          break

        case 'sse-error':
          if (!isLeader) onErrorRef.current?.(!!closed)
          break

        case 'leader-down':
          if (!isLeader && !destroyed) startElection()
          break
      }
    }

    // Ensure leader-down is sent even if React cleanup hasn't run yet
    function handleBeforeUnload() {
      if (isLeader && !destroyed) {
        destroyed = true
        channel.postMessage({ type: 'leader-down' })
      }
    }
    window.addEventListener('beforeunload', handleBeforeUnload)

    // Start election
    startElection()

    // ---- Cleanup ----
    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload)
      if (electionTimer) clearTimeout(electionTimer)
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (!destroyed && isLeader) channel.postMessage({ type: 'leader-down' })
      destroyed = true
      eventSource?.close()
      channel.close()
    }
  }, [url])
}
