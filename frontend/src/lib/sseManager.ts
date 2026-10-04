import { createContext } from 'react'

/** Callback invoked when an SSE event arrives for a subscribed topic. */
export type EventHandler = (data: string) => void

/** A subscription registered by a useSSE consumer. */
export interface Subscription {
  id: number
  topic: string
  /** Map of event-name → handler. */
  events: Record<string, EventHandler>
  /** Called when the underlying EventSource reconnects (optional). */
  onReconnect?: () => void
}

/** Internal manager that coordinates subscriptions, EventSource, and BroadcastChannel. */
export interface SSEManager {
  subscribe(sub: Subscription): void
  unsubscribe(id: number): void
}

// ---------------------------------------------------------------------------
// Manager slot
// ---------------------------------------------------------------------------
//
// Exactly one manager exists per tab. It is created and destroyed by
// `SSEProvider` (which owns the authentication-driven lifecycle) and read by
// `useSSE` through the context below. Modelling the live manager as an external
// store keeps the create/destroy effect free of a synchronous setState: the
// provider publishes the new manager, and subscribers re-render from it.

let currentManager: SSEManager | null = null
const listeners = new Set<() => void>()

/** Publish the tab's live manager (or `null` once it has been destroyed). */
export function setCurrentSSEManager(manager: SSEManager | null): void {
  if (currentManager === manager) return
  currentManager = manager
  for (const listener of [...listeners]) listener()
}

export function subscribeToSSEManager(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function getSSEManagerSnapshot(): SSEManager | null {
  return currentManager
}

export const SSEContext = createContext<SSEManager | null>(null)
