import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useAuth } from '@/lib/auth'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'
import {
  SSEContext,
  getSSEManagerSnapshot,
  setCurrentSSEManager,
  subscribeToSSEManager,
  type SSEManager,
  type Subscription,
} from '@/lib/sseManager'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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
    | 'session-expired'
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
/** Consecutive 401s, with no successful open in between, mean the session died. */
const EXPIRY_FAILURE_LIMIT = 3

/**
 * Generic EventSource errors carry no HTTP status — a network blip and an
 * expired session look identical. Ask the server who the session still is.
 */
async function probeSession(): Promise<number | null> {
  try {
    const response = await fetch('/api/auth/me', { credentials: 'include' })
    return response.status
  } catch {
    // A failed probe is a network problem, never proof of expiry.
    return null
  }
}

/**
 * Watches the stream's connect attempts. The server answers an expired session
 * with 401 on every reconnect, so an unbroken run of 401s is the signal — once
 * tripped the manager stops reconnecting until the manager is recreated (page
 * reload or a new login). Anything else (200, network error) is transient: the
 * retries continue and the user is not asked to sign in again.
 */
function createSessionGuard(onExpired: () => void) {
  let failures = 0
  let expired = false
  /** Bumped by every successful open — a probe started before it is stale. */
  let generation = 0
  return {
    get expired() {
      return expired
    },
    /** The stream opened — the session is alive and the run is broken. */
    connected() {
      failures = 0
      generation += 1
    },
    /** Another tab proved the session is dead — same once-only trip, no probe. */
    markExpired() {
      if (expired) return
      expired = true
      onExpired()
    },
    /**
     * Probe the session after a failed attempt; true once it counts as expired.
     * `strikes` is how many consecutive 401s it takes: one where the browser
     * will never retry the stream itself, three on the leader's reconnect path.
     */
    async failed(strikes = EXPIRY_FAILURE_LIMIT): Promise<boolean> {
      if (expired) return true
      const startedAt = generation
      const status = await probeSession()
      if (expired) return true
      // A stream opened while this probe was in flight: its verdict belongs to
      // a connection that is gone, so it counts neither as a failure nor a hit.
      if (startedAt !== generation) return false
      if (status !== 401) {
        failures = 0
        return false
      }
      failures += 1
      if (failures < strikes) return false
      failures = 0
      expired = true
      onExpired()
      return true
    },
  }
}

// ---------------------------------------------------------------------------
// Manager implementation (navigator.locks-based leader election)
// ---------------------------------------------------------------------------

function createSSEManager(onExpired: () => void): SSEManager & { destroy(): void } {
  const subscriptions = new Map<number, Subscription>()
  let destroyed = false
  // A probe can outlive the manager (logout, new login): its 401 verdict then
  // describes a session this tab no longer has, so it must not raise the banner.
  const guard = createSessionGuard(() => {
    if (!destroyed) handleSessionExpired()
  })

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
  let channel: BroadcastChannel | null = hasBroadcast ? new BroadcastChannel(CHANNEL_NAME) : null

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
    if (destroyed || guard.expired) return
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
      guard.connected()
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

      // An expired session keeps answering 401 — stop retrying it. Anything
      // else (network blip, server restart) is transient: reconnect.
      void guard.failed().then((expired) => {
        // `eventSource` set again = a reconnect already happened while probing.
        if (expired || destroyed || !isLeader || eventSource) return
        if (reconnectBackoffTimer) clearTimeout(reconnectBackoffTimer)
        reconnectBackoffTimer = setTimeout(() => {
          reconnectBackoffTimer = null
          if (!destroyed && isLeader) connectEventSource()
        }, reconnectDelay)
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
      })
    }
  }

  /** Schedule a debounced reconnection. */
  function scheduleReconnect() {
    if (destroyed || guard.expired) return
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

  /**
   * The session is dead. The leader hands off — it stops serving, tells the
   * other tabs and releases the lock so one of them can take over — and this
   * tab shows the banner. The guard runs this at most once per manager.
   */
  function handleSessionExpired() {
    stopHeartbeat()
    stopWatchdog()
    // A dead tab must never advertise or take leadership of the dead session:
    // drop the flag now and stop any queued lock request from promoting it.
    const wasLeader = isLeader
    isLeader = false
    tabTopics.clear()
    tabEventNames.clear()
    if (lockAbort) { lockAbort.abort(); lockAbort = null }
    if (wasLeader) {
      if (reconnectDebounceTimer) { clearTimeout(reconnectDebounceTimer); reconnectDebounceTimer = null }
      if (reconnectBackoffTimer) { clearTimeout(reconnectBackoffTimer); reconnectBackoffTimer = null }
      removeEventSourceListeners()
      eventSource?.close()
      eventSource = null
      channel?.postMessage({ type: 'session-expired' })
      if (lockReleaseResolve) { lockReleaseResolve(); lockReleaseResolve = null }
    }
    onExpired()
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
          // We acquired the lock — but a dead tab must not take the stream.
          if (destroyed || guard.expired) return Promise.resolve()
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
  // A messageerror means one broadcast was dropped, not that the channel is
  // gone (and BroadcastChannel fires no close event at all): the tab keeps
  // listening. A silent leader is caught by the heartbeat watchdog instead.
  if (channel) {
    channel.onmessage = (msg: MessageEvent<ChannelMessage>) => {
      const m = msg.data

      switch (m.type) {
        case 'who-is-leader':
          if (isLeader) channel?.postMessage({ type: 'leader', tabId: TAB_ID })
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

        case 'session-expired':
          // The leader's session died. Show the same banner here, and mark the
          // guard so this tab never promotes itself into the dead session.
          guard.markExpired()
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
    // The only deliberate path that gives up the channel — this tab closes it.
    channel?.close()
    channel = null
  }

  return { subscribe, unsubscribe, destroy }
}

// ---------------------------------------------------------------------------
// Fallback manager (no BroadcastChannel — e.g., JSDOM tests)
// ---------------------------------------------------------------------------

function createFallbackManager(onExpired: () => void): SSEManager & { destroy(): void } {
  const subscriptions = new Map<number, Subscription>()
  let destroyed = false
  // A probe can outlive the manager (logout, new login): its 401 verdict then
  // describes a session this tab no longer has, so it must not raise the banner.
  const guard = createSessionGuard(() => {
    if (!destroyed) onExpired()
  })
  let eventSource: EventSource | null = null
  let activeListeners: Array<{ name: string; handler: (e: Event) => void }> = []
  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let backoffTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectDelay = 1000

  function connectEventSource() {
    if (destroyed || guard.expired) return

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
      reconnectDelay = 1000
      guard.connected()
      for (const sub of subscriptions.values()) sub.onReconnect?.()
    }
    eventSource.onerror = () => {
      // EventSource treats a 401 as fatal: it fires onerror once and closes for
      // good, so this tab probes the session and decides whether to retry.
      const es = eventSource
      for (const { name, handler } of activeListeners) es?.removeEventListener(name, handler)
      activeListeners = []
      es?.close()
      if (eventSource === es) eventSource = null
      // One 401 is enough here — the browser will never retry it.
      void guard.failed(1).then((expired) => {
        if (expired || destroyed || eventSource) return
        // Back off like the leader path does: a down server must not be probed
        // on every 150ms tick by every open tab.
        if (backoffTimer) clearTimeout(backoffTimer)
        backoffTimer = setTimeout(() => {
          backoffTimer = null
          if (!destroyed) connectEventSource()
        }, reconnectDelay)
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
      })
    }
  }

  function scheduleReconnect() {
    if (destroyed || guard.expired) return
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
      if (backoffTimer) clearTimeout(backoffTimer)
      for (const { name, handler } of activeListeners) {
        eventSource?.removeEventListener(name, handler)
      }
      eventSource?.close()
    },
  }
}

// ---------------------------------------------------------------------------
// SSEProvider component
// ---------------------------------------------------------------------------

export function SSEProvider({ children }: { children: ReactNode }) {
  const { authenticated } = useAuth()
  // Surfaced to the user as a re-login prompt; nothing is redirected or cleared.
  // Tied to the auth state it was reported for, so a login/logout starts clean.
  const [expiry, setExpiry] = useState({ authenticated, expired: false })
  if (expiry.authenticated !== authenticated) {
    setExpiry({ authenticated, expired: false })
  }

  // Create/destroy the manager based on authentication state.
  // When the user logs out, the manager is destroyed so a logged-out
  // leader tab doesn't keep sending heartbeats and failing /api/stream
  // calls with 401 — blocking other tabs from taking over.
  // The manager is published through the module-level store (not React state)
  // so publishing it never cascades a synchronous render from the effect.
  useEffect(() => {
    if (!authenticated) return
    // New credentials — the fresh manager gets a new budget of attempts.
    const onExpired = () => setExpiry({ authenticated, expired: true })
    const manager = typeof BroadcastChannel !== 'undefined'
      ? createSSEManager(onExpired)
      : createFallbackManager(onExpired)
    setCurrentSSEManager(manager)
    return () => {
      setCurrentSSEManager(null)
      manager.destroy()
    }
  }, [authenticated])

  const manager = useSyncExternalStore(subscribeToSSEManager, getSSEManagerSnapshot, getSSEManagerSnapshot)

  const value = authenticated ? manager : null

  return (
    <SSEContext.Provider value={value}>
      {expiry.expired && (
        <CustomMessageBanner
          className="fixed inset-x-0 top-0 z-50 rounded-none text-center"
          message="Your session expired — live updates are paused. Reload the page and sign in again to resume them."
        />
      )}
      {children}
    </SSEContext.Provider>
  )
}
