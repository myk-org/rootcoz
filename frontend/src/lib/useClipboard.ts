import { useEffect, useRef, useState } from 'react'

export function useClipboard(resetMs = 2000) {
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const copy = async (text: string, key = 'default'): Promise<boolean> => {
    if (!navigator.clipboard?.writeText) return false
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopiedKey(null), resetMs)
      return true
    } catch {
      setCopiedKey(null)
      return false
    }
  }

  return { copiedKey, isCopied: (k = 'default') => copiedKey === k, copy }
}
