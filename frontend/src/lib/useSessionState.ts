import { useState, useCallback } from 'react'

/**
 * Like useState but persists to sessionStorage.
 * Survives page refresh (F5) but clears when the tab closes.
 *
 * Supports boolean and string values. Booleans are stored as 'true'/'false'
 * and parsed back; strings are stored and retrieved as-is.
 */
export function useSessionState<T extends boolean | string>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = sessionStorage.getItem(key)
      if (stored === null) return initial
      // If the initial value is boolean, parse stored value as boolean
      if (typeof initial === 'boolean') return (stored === 'true') as T
      return stored as T
    } catch {
      return initial
    }
  })

  const set = useCallback(
    (v: T) => {
      try {
        sessionStorage.setItem(key, String(v))
      } catch {
        // sessionStorage full or disabled — ignore
      }
      setValue(v)
    },
    [key],
  )

  return [value, set]
}
