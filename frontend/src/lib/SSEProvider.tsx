import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useAuth } from '@/lib/auth'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Callback invoked when an SSE event arrives for a subscribed topic. */
type EventHandler = (data: string) => void

/** A subscription registered by a useSSE consumer. */
interface Subscription {
  id: number
  topic: string
  /** Map of event-name → handler. */
  events: Record<string, EventHandler>
  /** Called when the underlying EventSource reconnects (optional). */
  onReconnect?: () => void
}

/** Internal manager that coordinates subscriptions, EventSource, and BroadcastChannel. */
interface SSEManager {
  subscribe(sub: Subscription): void
  unsubscribe(id: number): void
}

// ---------------------------------------------------------------------------
// BroadcastChannel protocol
// ---------------------------------------------------------------------------

const TAB_ID = `${Date.now()}-${Math.random().toString(36).slice(2)}`

interface ChannelMessage {
  type:
    | 'who-is-leader'
    | 'leader'
    | 'leader-down'
    | 'sse-event'
    | 'sse-open'
    | 'topics-update'
    | 'topics-request'
    | 'tab-down'
  tabId?: string
  /** Full SSE event name (for sse-event). */
  name?: string
  /** SSE event data string (for sse-event). */
  data?: string
  /** Bare topics for this tab — for building ?topics= query (for topics-update). */
  topics?: string[]
  /** Full event names (topic:eventName) for addEventListener registration (for topics-update). */
  eventNames?: string[]
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CHANNEL_NAME = 'rootcoz-sse-mux'
const LOCK_NAME = 'rootcoz-sse-leader'
const RECONNECT_DEBOUNCE_MS = 150
const LEADER_HEARTBEAT_MS = 5_000
const LEADER_TIMEOUT_MS = 12_000
const MAX_RECONNECT_DELAY_MS = 30_000

// ---------------------------------------------------------------------------
// Manager implementation (navigator.locks-based leader election)
// ---------------------------------------------------------------------------

function createSSEManager(): SSEManager & { destroy(): void } {
  const subscriptions = new Map<number, Subscription>()
  let destroyed = false

  // ---- Leader state ----
  let isLeader = false
  let eventSource: EventSource | null = null
  let reconnectDebounceTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectBackoffTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectDelay = 1000
  /** Currently active event listeners on the EventSource (for cleanup). */
  let activeListeners: Array<{ name: string; handler: (e: Event) => void }> = []
  /** Leader heartbeat interval (leader sends heartbeat to followers). */
  let heartbeatInterval: ReturnType<typeof setInterval> | null = null
  /** Follower watchdog — detects stale leader via heartbeat timeout. */
  let heartbeatWatchdog: ReturnType<typeof setTimeout> | null = null
  /** Abort controller for the navigator.locks request — used to cancel on destroy. */
  let lockAbort: AbortController | null = null

  // Cross-tab topic tracking: leader aggregates data from all tabs
  /** Bare topics per tab — used for building /api/stream?topics= */
  const tabTopics = new Map<string, Set<string>>()
  /** Full event names per tab — used for registering EventSource listeners */
  const tabEventNames = new Map<string, Set<string>>()

  // ---- BroadcastChannel ----
  const hasBroadcast = typeof BroadcastChannel !== 'undefined'
  const channel = hasBroadcast ? new BroadcastChannel(CHANNEL_NAME) : null

  // ---- Helpers ----

  /** Compute the union of all local subscriptions' topics. */
  function getLocalTopics(): Set<string> {
    const topics = new Set<string>()
    for (const sub of subscriptions.values()) {
      topics.add(sub.topic)
    }
    return topics
  }

  /** Compute the combined bare topic set (local + all remote tabs) for the ?topics= query. */
  function getCombinedTopics(): string[] {
    const all = new Set<string>()
    // Local
    for (const t of getLocalTopics()) all.add(t)
    // Remote tabs (only relevant for leader)
    if (isLeader) {
      for (const topics of tabTopics.values()) {
        for (const t of topics) all.add(t)
      }
    }
    return [...all].sort()
  }

  /** Dispatch an incoming event to matching subscribers. */
  function dispatchEvent(fullEventName: string, data: string) {
    for (const sub of subscriptions.values()) {
      const prefix = sub.topic + ':'
      if (fullEventName.startsWith(prefix)) {
        const eventName = fullEventName.slice(prefix.length)
        sub.events[eventName]?.(data)
      }
    }
  }

  /** Notify all subscribers that the connection reconnected. */
  function notifyReconnect() {
    for (const sub of subscriptions.values()) {
      sub.onReconnect?.()
    }
  }

  // ---- EventSource management ----

  function removeEventSourceListeners() {
    if (!eventSource) return
    for (const { name, handler } of activeListeners) {
      eventSource.removeEventListener(name, handler)
    }
    activeListeners = []
  }

  function buildEventSourceUrl(topics: string[]): string {
    if (topics.length === 0) return ''
    return `/api/stream?topics=${encodeURIComponent(topics.join(','))}`
  }

  function connectEventSource() {
    if (destroyed) return
    const topics = getCombinedTopics()

    // Close existing
    removeEventSourceListeners()
    eventSource?.close()
    eventSource = null

    if (topics.length === 0) return

    const url = buildEventSourceUrl(topics)
    eventSource = new EventSource(url)

    // Build listeners for all subscribed topic:event combinations
    const listenerNames = new Set<string>()
    for (const sub of subscriptions.values()) {
      for (const eventName of Object.keys(sub.events)) {
        listenerNames.add(`${sub.topic}:${eventName}`)
      }
    }
    // Register listeners for locally known event names
    for (const fullName of listenerNames) {
      const handler = (e: Event) => {
        const data = (e as MessageEvent).data as string
        dispatchEvent(fullName, data)
        // Relay to follower tabs
        channel?.postMessage({ type: 'sse-event', name: fullName, data })
      }
      eventSource.addEventListener(fullName, handler)
      activeListeners.push({ name: fullName, handler })
    }

    // For the leader: also register listeners for remote tab event names
    if (isLeader) {
      for (const [, remoteNames] of tabEventNames) {
        for (const fullName of remoteNames) {
          if (!listenerNames.has(fullName)) {
            listenerNames.add(fullName)
            const handler = (e: Event) => {
              const data = (e as MessageEvent).data as string
              // Don't dispatch locally — this is a remote-only event
              channel?.postMessage({ type: 'sse-event', name: fullName, data })
            }
            eventSource.addEventListener(fullName, handler)
            activeListeners.push({ name: fullName, handler })
          }
        }
      }
    }

    eventSource.onopen = () => {
      reconnectDelay = 1000
      notifyReconnect()
      channel?.postMessage({ type: 'sse-open' })
    }

    eventSource.onerror = () => {
      // Close immediately and reconnect manually with backoff.
      // This prevents the browser's built-in auto-reconnect from opening
      // duplicate connections that exhaust the per-domain connection pool.
      const es = eventSource
      removeEventSourceListeners()
      es?.close()
      eventSource = null

      if (!destroyed && isLeader) {
        if (reconnectBackoffTimer) clearTimeout(reconnectBackoffTimer)
        reconnectBackoffTimer = setTimeout(() => {
          reconnectBackoffTimer = null
          if (!destroyed && isLeader) connectEventSource()
        }, reconnectDelay)
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
      }
    }
  }

  /** Schedule a debounced reconnection. */
  function scheduleReconnect() {
    if (destroyed) return
    if (reconnectDebounceTimer) clearTimeout(reconnectDebounceTimer)
    reconnectDebounceTimer = setTimeout(() => {
      reconnectDebounceTimer = null
      if (isLeader) {
        connectEventSource()
      }
      // Broadcast our topics to the leader
      broadcastTopics()
    }, RECONNECT_DEBOUNCE_MS)
  }

  // ---- Cross-tab communication ----

  /** Get local full event names (topic:eventName) for broadcasting. */
  function getLocalEventNames(): string[] {
    const names: string[] = []
    for (const sub of subscriptions.values()) {
      for (const eventName of Object.keys(sub.events)) {
        names.push(`${sub.topic}:${eventName}`)
      }
    }
    return [...new Set(names)]
  }

  function broadcastTopics() {
    channel?.postMessage({
      type: 'topics-update',
      tabId: TAB_ID,
      topics: [...getLocalTopics()],
      eventNames: getLocalEventNames(),
    })
  }

  // ---- Leader lifecycle ----

  function startHeartbeat() {
    if (heartbeatInterval) clearInterval(heartbeatInterval)
    heartbeatInterval = setInterval(() => {
      channel?.postMessage({ type: 'leader', tabId: TAB_ID })
    }, LEADER_HEARTBEAT_MS)
  }

  function stopHeartbeat() {
    if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null }
  }

  function resetWatchdog() {
    if (heartbeatWatchdog) clearTimeout(heartbeatWatchdog)
    heartbeatWatchdog = setTimeout(() => {
      heartbeatWatchdog = null
      // Leader seems dead — try to acquire the lock ourselves
      if (!isLeader && !destroyed) {
        requestLeaderLock()
      }
    }, LEADER_TIMEOUT_MS)
  }

  function stopWatchdog() {
    if (heartbeatWatchdog) { clearTimeout(heartbeatWatchdog); heartbeatWatchdog = null }
  }

  function becomeLeader() {
    if (destroyed || isLeader) return
    isLeader = true
    stopWatchdog()
    channel?.postMessage({ type: 'leader', tabId: TAB_ID })
    // Ask all tabs for their topics
    channel?.postMessage({ type: 'topics-request' })
    startHeartbeat()
    // Connect with local topics first; remote topics will arrive soon
    connectEventSource()
  }

  function stepDown() {
    isLeader = false
    stopHeartbeat()
    if (reconnectBackoffTimer) { clearTimeout(reconnectBackoffTimer); reconnectBackoffTimer = null }
    removeEventSourceListeners()
    eventSource?.close()
    eventSource = null
    tabTopics.clear()
    tabEventNames.clear()
    // Start watching for leader heartbeats
    resetWatchdog()
  }

  // ---- navigator.locks-based leader election ----

  function requestLeaderLock() {
    if (destroyed) return
    // navigator.locks is available in all modern browsers
    if (typeof navigator !== 'undefined' && navigator.locks) {
      lockAbort = new AbortController()
      navigator.locks.request(
        LOCK_NAME,
        { signal: lockAbort.signal },
        () => {
          // We acquired the lock — we are the leader
          if (destroyed) return Promise.resolve()
          becomeLeader()
          // Return a promise that never resolves — we hold the lock until
          // this tab closes or destroy() is called. When the tab closes,
          // the browser auto-releases the lock, and the next waiting tab
          // gets it — no beforeunload needed.
          return new Promise<void>((resolve) => {
            // Store resolve so destroy() can release the lock
            lockReleaseResolve = resolve
          })
        },
      ).catch(() => {
        // AbortError when destroy() cancels, or NotSupportedError — ignore
      })
    } else {
      // Fallback for environments without navigator.locks (e.g. older browsers):
      // use the BroadcastChannel-based election
      fallbackElection()
    }
  }

  let lockReleaseResolve: (() => void) | null = null

  /** BroadcastChannel-only election fallback (for environments without navigator.locks). */
  let electionTimer: ReturnType<typeof setTimeout> | null = null

  function fallbackElection() {
    if (destroyed) return
    if (electionTimer) { clearTimeout(electionTimer); electionTimer = null }
    channel?.postMessage({ type: 'who-is-leader' })
    const jitter = Math.random() * 100
    electionTimer = setTimeout(() => {
      electionTimer = null
      if (!destroyed) becomeLeader()
    }, 150 + jitter)
  }

  // ---- Channel message handler ----
  if (channel) {
    channel.onmessage = (msg: MessageEvent<ChannelMessage>) => {
      const m = msg.data

      switch (m.type) {
        case 'who-is-leader':
          if (isLeader) channel.postMessage({ type: 'leader', tabId: TAB_ID })
          break

        case 'leader': {
          const theirId = m.tabId ?? ''
          // Cancel any pending fallback election
          if (electionTimer) { clearTimeout(electionTimer); electionTimer = null }
          if (isLeader && theirId && theirId < TAB_ID) {
            stepDown()
          }
          if (!isLeader) {
            // Leader is alive — reset watchdog and send our topics
            resetWatchdog()
            broadcastTopics()
          }
          break
        }

        case 'sse-event':
          if (!isLeader && m.name) dispatchEvent(m.name, m.data ?? '')
          break

        case 'sse-open':
          if (!isLeader) notifyReconnect()
          break

        case 'topics-update':
          if (isLeader && m.tabId && m.tabId !== TAB_ID) {
            tabTopics.set(m.tabId, new Set(m.topics ?? []))
            tabEventNames.set(m.tabId, new Set(m.eventNames ?? []))
            scheduleReconnect()
          }
          break

        case 'topics-request':
          // A new leader is asking for our topics
          broadcastTopics()
          break

        case 'tab-down':
          if (isLeader && m.tabId) {
            tabTopics.delete(m.tabId)
            tabEventNames.delete(m.tabId)
            scheduleReconnect()
          }
          break

        case 'leader-down':
          // Leader announced shutdown — the lock will be released and
          // the next queued tab will acquire it automatically.
          // Reset watchdog in case lock acquisition is slow.
          if (!isLeader && !destroyed) resetWatchdog()
          break
      }
    }
  }

  // ---- beforeunload ----
  function handleBeforeUnload() {
    if (destroyed) return
    if (isLeader) {
      channel?.postMessage({ type: 'leader-down' })
    } else {
      channel?.postMessage({ type: 'tab-down', tabId: TAB_ID })
    }
    // Lock auto-releases on tab close — no need to resolve here
  }
  window.addEventListener('beforeunload', handleBeforeUnload)

  // Start leader election
  if (channel) {
    requestLeaderLock()
  }

  // ---- Public API ----

  function subscribe(sub: Subscription) {
    subscriptions.set(sub.id, sub)
    scheduleReconnect()
  }

  function unsubscribe(id: number) {
    subscriptions.delete(id)
    scheduleReconnect()
  }

  function destroy() {
    window.removeEventListener('beforeunload', handleBeforeUnload)
    if (!destroyed && isLeader) channel?.postMessage({ type: 'leader-down' })
    if (!destroyed && !isLeader) channel?.postMessage({ type: 'tab-down', tabId: TAB_ID })
    destroyed = true
    stopHeartbeat()
    stopWatchdog()
    if (electionTimer) clearTimeout(electionTimer)
    if (reconnectDebounceTimer) clearTimeout(reconnectDebounceTimer)
    if (reconnectBackoffTimer) clearTimeout(reconnectBackoffTimer)
    removeEventSourceListeners()
    eventSource?.close()
    // Release the lock if held
    if (lockReleaseResolve) { lockReleaseResolve(); lockReleaseResolve = null }
    // Abort any pending lock request
    if (lockAbort) { lockAbort.abort(); lockAbort = null }
    channel?.close()
  }

  return { subscribe, unsubscribe, destroy }
}

// ---------------------------------------------------------------------------
// Fallback manager (no BroadcastChannel — e.g., JSDOM tests)
// ---------------------------------------------------------------------------

function createFallbackManager(): SSEManager & { destroy(): void } {
  const subscriptions = new Map<number, Subscription>()
  let eventSource: EventSource | null = null
  let activeListeners: Array<{ name: string; handler: (e: Event) => void }> = []
  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let destroyed = false

  function connectEventSource() {
    if (destroyed) return

    // Cleanup old
    for (const { name, handler } of activeListeners) {
      eventSource?.removeEventListener(name, handler)
    }
    activeListeners = []
    eventSource?.close()
    eventSource = null

    // Build topics
    const topics = new Set<string>()
    for (const sub of subscriptions.values()) topics.add(sub.topic)
    if (topics.size === 0) return

    const url = `/api/stream?topics=${encodeURIComponent([...topics].sort().join(','))}`
    eventSource = new EventSource(url)

    // Register listeners
    const listenerNames = new Set<string>()
    for (const sub of subscriptions.values()) {
      for (const eventName of Object.keys(sub.events)) {
        listenerNames.add(`${sub.topic}:${eventName}`)
      }
    }

    for (const fullName of listenerNames) {
      const handler = (e: Event) => {
        const data = (e as MessageEvent).data as string
        for (const sub of subscriptions.values()) {
          const prefix = sub.topic + ':'
          if (fullName.startsWith(prefix)) {
            const eventName = fullName.slice(prefix.length)
            sub.events[eventName]?.(data)
          }
        }
      }
      eventSource.addEventListener(fullName, handler)
      activeListeners.push({ name: fullName, handler })
    }

    eventSource.onopen = () => {
      for (const sub of subscriptions.values()) sub.onReconnect?.()
    }
    eventSource.onerror = () => { /* auto-reconnects */ }
  }

  function scheduleReconnect() {
    if (destroyed) return
    if (debounceTimer) clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => {
      debounceTimer = null
      connectEventSource()
    }, RECONNECT_DEBOUNCE_MS)
  }

  return {
    subscribe(sub) {
      subscriptions.set(sub.id, sub)
      scheduleReconnect()
    },
    unsubscribe(id) {
      subscriptions.delete(id)
      scheduleReconnect()
    },
    destroy() {
      destroyed = true
      if (debounceTimer) clearTimeout(debounceTimer)
      for (const { name, handler } of activeListeners) {
        eventSource?.removeEventListener(name, handler)
      }
      eventSource?.close()
    },
  }
}

// ---------------------------------------------------------------------------
// React context
// ---------------------------------------------------------------------------

const SSEContext = createContext<SSEManager | null>(null)

let nextSubId = 1

export function SSEProvider({ children }: { children: ReactNode }) {
  const { authenticated } = useAuth()
  const [manager, setManager] = useState<(SSEManager & { destroy(): void }) | null>(null)

  // Create/destroy manager based on authentication state.
  // When the user logs out, the manager is destroyed so a logged-out
  // leader tab doesn't keep sending heartbeats and failing /api/stream
  // calls with 401 — blocking other tabs from taking over.
  // Uses useState (not useRef) so the context value triggers a rerender
  // when the manager is created after login.
  useEffect(() => {
    if (authenticated) {
      const m = typeof BroadcastChannel !== 'undefined'
        ? createSSEManager()
        : createFallbackManager()
      setManager(m)
      return () => { m.destroy() }
    } else {
      setManager(prev => { prev?.destroy(); return null })
    }
  }, [authenticated])

  const value = authenticated ? manager : null

  return <SSEContext.Provider value={value}>{children}</SSEContext.Provider>
}

// ---------------------------------------------------------------------------
// useSSE hook
// ---------------------------------------------------------------------------

/**
 * Subscribe to a multiplexed SSE topic.
 *
 * All `useSSE` hooks share a single EventSource connection via the
 * `SSEProvider`. The provider aggregates subscribed topics and connects
 * to `GET /api/stream?topics=...`. Events arrive prefixed with the topic
 * (e.g., `navbar:active-count`) and are dispatched to the matching hook.
 *
 * @param topic  Topic to subscribe to (e.g., `'navbar'`, `'results:abc123'`).
 *               Pass `null` to disable the subscription (conditional SSE).
 * @param events Map of event-name → handler callback.
 * @param options.onReconnect Called when the underlying EventSource reconnects.
 *
 * @example
 * useSSE('navbar', {
 *   'active-count': (data) => setActiveCount(parseInt(data, 10)),
 *   'unread-count': (data) => setUnreadCount(parseInt(data, 10)),
 * })
 *
 * @example
 * // Conditional subscription — null topic means no connection
 * useSSE(isActive ? `results:${jobId}` : null, {
 *   'status-changed': () => refetch(),
 * })
 */
export function useSSE(
  topic: string | null,
  events: Record<string, EventHandler>,
  options?: { onReconnect?: () => void },
): void {
  const manager = useContext(SSEContext)
  const eventsRef = useRef(events)
  eventsRef.current = events
  const onReconnectRef = useRef(options?.onReconnect)
  onReconnectRef.current = options?.onReconnect

  // Stable subscription ID per hook instance
  const subIdRef = useRef(nextSubId++)

  useEffect(() => {
    if (!manager || !topic) return

    const sub: Subscription = {
      id: subIdRef.current,
      topic,
      // Wrap in getters so the manager always calls the latest callbacks
      get events() {
        return Object.fromEntries(
          Object.keys(eventsRef.current).map((k) => [
            k,
            (data: string) => eventsRef.current[k]?.(data),
          ]),
        )
      },
      get onReconnect() {
        return onReconnectRef.current
      },
    }

    manager.subscribe(sub)
    return () => manager.unsubscribe(sub.id)
  }, [manager, topic])
}
