/** Format a number with K/M suffixes (e.g. 15000 → "15K", 1500000 → "1.5M"). */
export function formatCompactNumber(n: number): string {
  if (n >= 1_000_000) {
    const m = n / 1_000_000
    return m % 1 === 0 ? `${m}M` : `${m.toFixed(1)}M`
  }
  if (n >= 1_000) {
    const k = n / 1_000
    return k % 1 === 0 ? `${k}K` : `${k.toFixed(1)}K`
  }
  return String(n)
}

/**
 * Unescape literal `\n` and `\t` sequences that backends sometimes embed
 * in code-snippet strings, turning them into real newline / tab characters.
 */
export function unescapeCodeContent(text: string): string {
  return text.replace(/\\\\[nt]|\\[nt]/g, (match) => {
    if (match === '\\n') return '\n'
    if (match === '\\t') return '\t'
    if (match === '\\\\n') return '\\n'
    if (match === '\\\\t') return '\\t'
    return match
  })
}

/** Format USD cost for display. Returns '$0.00' for null/zero/undefined. */
export function formatCost(value: number | null | undefined): string {
  if (value == null || value === 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}
