import { describe, it, expect, beforeEach } from 'vitest'
import { getRecentErrors } from '../errorCapture'

describe('errorCapture', () => {
  // The module is already imported and listeners installed as a side effect.
  // We can trigger errors and verify they're captured.

  beforeEach(() => {
    // Flush any errors captured from other tests by reading them.
    // The module keeps state across tests within the same import.
    // We need a fresh start, so we'll verify behavior additively.
  })

  it('captures window.onerror events', () => {
    const before = getRecentErrors().length
    // Simulate a global error by calling window.onerror directly
    if (typeof window.onerror === 'function') {
      window.onerror('Test error message', 'test.js', 10, 5, new Error('test'))
    }
    const after = getRecentErrors()
    expect(after.length).toBeGreaterThan(before)
    expect(after[after.length - 1]).toContain('Test error message')
  })

  it('captures unhandled rejection events', () => {
    const before = getRecentErrors().length
    // Dispatch an unhandledrejection event
    const event = new Event('unhandledrejection') as PromiseRejectionEvent
    Object.defineProperty(event, 'reason', { value: new Error('rejection reason') })
    window.dispatchEvent(event)
    const after = getRecentErrors()
    expect(after.length).toBeGreaterThan(before)
    expect(after[after.length - 1]).toContain('rejection reason')
  })

  it('returns a copy of the errors array', () => {
    const a = getRecentErrors()
    const b = getRecentErrors()
    expect(a).not.toBe(b)
    expect(a).toEqual(b)
  })
})
