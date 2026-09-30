import { useCallback, useEffect, useState } from 'react'

/**
 * Reports whether a scroll container still has content below the fold.
 *
 * The global scrollbar is 8px with a transparent track, so on browsers that
 * use overlay scrollbars there is no visible hint that a page continues. A
 * user landing on Settings saw the profile form and no sign that their AI
 * keys were further down.
 */
export function useHasMoreBelow<T extends HTMLElement>() {
  const [el, setEl] = useState<T | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const ref = useCallback((node: T | null) => setEl(node), [])

  useEffect(() => {
    if (!el) return
    const measure = () => {
      setHasMore(el.scrollHeight - el.scrollTop - el.clientHeight > 4)
    }
    measure()
    el.addEventListener('scroll', measure, { passive: true })
    // Re-measure when the routed content changes size. Guarded because the
    // observer is absent in jsdom, which renders this component in tests.
    if (typeof ResizeObserver === 'undefined') {
      return () => el.removeEventListener('scroll', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    for (const child of Array.from(el.children)) observer.observe(child)
    return () => {
      el.removeEventListener('scroll', measure)
      observer.disconnect()
    }
  }, [el])

  return { ref, hasMore }
}
