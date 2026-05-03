/**
 * Captures console errors and unhandled rejections in a circular buffer.
 * Side-effect module: initializes listeners on import.
 */

const MAX_ERRORS = 20
const recentErrors: string[] = []

function pushError(msg: string) {
  if (recentErrors.length >= MAX_ERRORS) {
    recentErrors.shift()
  }
  recentErrors.push(msg)
}

/** Return a snapshot of the recent console errors. */
export function getRecentErrors(): string[] {
  return [...recentErrors]
}

// --- Install global listeners (side-effect) ---

const prevOnError = window.onerror
window.onerror = (message, source, lineno, colno, error) => {
  const parts = [String(message)]
  if (source) parts.push(`at ${source}:${lineno ?? '?'}:${colno ?? '?'}`)
  if (error?.stack) parts.push(error.stack)
  pushError(parts.join(' '))

  if (typeof prevOnError === 'function') {
    return prevOnError(message, source, lineno, colno, error)
  }
  return false
}

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason
  if (reason instanceof Error) {
    pushError(reason.stack ?? reason.message)
  } else {
    pushError(String(reason))
  }
})
