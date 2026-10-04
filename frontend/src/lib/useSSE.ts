import { useContext, useEffect, useRef } from 'react'
import {
  SSEContext,
  type EventHandler,
  type Subscription,
} from '@/lib/sseManager'

let nextSubId = 1


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
