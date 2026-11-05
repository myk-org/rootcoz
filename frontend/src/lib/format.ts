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

/** Format a byte count with binary units (e.g. 1536 → "1.5 KiB"). */
export function formatBytes(bytes: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`
}

/** Humanise a process uptime in seconds (e.g. 93784 → "1d 2h"). */
export function formatUptime(seconds: number): string {
  const total = Math.floor(seconds)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${total % 60}s`
  return `${total}s`
}

/**
 * Format summed AI call duration as seconds, or null when nothing was recorded.
 *
 * Summed call time is not wall-clock analysis time — callers must say so.
 */
export function formatSummedDuration(totalDurationMs: number | null | undefined): string | null {
  if (!totalDurationMs || totalDurationMs <= 0) return null
  return `${(totalDurationMs / 1000).toFixed(1)}s`
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
