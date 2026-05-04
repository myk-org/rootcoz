import { useEffect, useRef } from 'react'

/**
 * Returns a ref that always holds the latest value.
 * Useful for keeping SSE/event handlers stable while still
 * calling the most recent version of a callback.
 */
export function useLatestRef<T>(value: T) {
  const ref = useRef(value)
  useEffect(() => {
    ref.current = value
  }, [value])
  return ref
}
