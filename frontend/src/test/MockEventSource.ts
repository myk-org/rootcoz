/**
 * Minimal `EventSource` double for the vitest suites.
 *
 * jsdom defines no `EventSource`, and the SSE consumers assume one exists, so
 * this is installed as the environment default in `src/test/setup.ts`; a test
 * that needs to inspect its own stream registers the instance it created via
 * `MockEventSource.instances`.
 */
export class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  /** Handlers still registered per event name — a removed one is gone for good. */
  listeners = new Map<string, Set<(e: MessageEvent) => void>>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (e: MessageEvent) => void) {
    const handlers = this.listeners.get(name) ?? new Set()
    handlers.add(handler)
    this.listeners.set(name, handlers)
  }

  removeEventListener(name: string, handler: (e: MessageEvent) => void) {
    this.listeners.get(name)?.delete(handler)
  }

  close() {
    this.closed = true
  }

  /** Deliver one event the way a live stream would (objects are JSON-encoded). */
  emit(name: string, data: unknown) {
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    for (const handler of this.listeners.get(name) ?? []) {
      handler({ data: payload } as MessageEvent)
    }
  }

  open() {
    this.onopen?.()
  }

  fail() {
    this.onerror?.()
  }

  /** Most recently constructed instance — the stream under test. */
  static get latest(): MockEventSource | undefined {
    return MockEventSource.instances[MockEventSource.instances.length - 1]
  }

  static reset() {
    MockEventSource.instances = []
  }
}
