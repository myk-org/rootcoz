import { useCallback, useRef } from 'react'

/** Runs a submit at most once at a time.
 *
 *  Guards a double click that lands before the busy phase re-renders (a ref,
 *  not state, so the second click sees it immediately) and releases the guard
 *  on failure so the retry button still works. */
export function useSubmitGuard() {
  const busy = useRef(false)
  return useCallback(async (submit: () => Promise<void>) => {
    if (busy.current) return
    busy.current = true
    try {
      await submit()
    } finally {
      busy.current = false
    }
  }, [])
}
