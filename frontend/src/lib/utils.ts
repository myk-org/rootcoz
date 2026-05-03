import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Fallback value for invalid or missing dates. */
export const INVALID_DATE_FALLBACK = '\u2014'

/** Check if a failed analysis was due to AI/analysis timeout. */
export function isAnalysisTimeout(status: string, error?: string | null, summary?: string | null): boolean {
  if (status !== 'failed') return false
  const AI_TIMEOUT_PATTERNS = [
    'analysis timed out',
    'ai timed out',
    'ai cli timed out',
    'cli timed out',
    'ai_cli_timeout',
  ]
  const JENKINS_TIMEOUT_PATTERN = 'timed out waiting for jenkins job'
  const check = (s?: string | null) => {
    if (!s) return false
    const lower = s.toLowerCase()
    if (lower.includes(JENKINS_TIMEOUT_PATTERN)) return false
    return (
      AI_TIMEOUT_PATTERNS.some((p) => lower.includes(p)) ||
      /^timeouterror:\s*(?:$|.*\b(ai|analysis|cli)\b)/i.test(s)
    )
  }
  return check(error) || check(summary)
}

/** Format the duration between two dates as a compact string. */
export function formatDuration(start: Date, end: Date): string {
  if (isNaN(start.getTime()) || isNaN(end.getTime())) return INVALID_DATE_FALLBACK
  const diffMs = end.getTime() - start.getTime()
  if (diffMs < 0) return INVALID_DATE_FALLBACK
  const totalSeconds = Math.floor(diffMs / 1000)
  const mins = Math.floor(totalSeconds / 60)
  const secs = totalSeconds % 60
  if (mins === 0) return `${secs}s`
  return `${mins}m ${secs}s`
}

/** Parse a UTC timestamp from the API (SQLite format) into a Date. */
export function parseApiTimestamp(ts: string): Date {
  // Normalize: replace space separator with T, then append Z only when no
  // timezone offset is present.  The regex handles Z, +HH:MM, -HH:MM, +HHMM,
  // -HHMM, and bare +HH/-HH offsets.
  let normalized = ts.includes('T') ? ts : ts.replace(' ', 'T')
  if (!/(?:[Zz]|[+-]\d{2}(?::?\d{2})?)$/.test(normalized)) {
    normalized += 'Z'
  }
  return new Date(normalized)
}

/** Extract a human-readable repository name from a Git URL. */
export function repoNameFromUrl(url: string): string {
  return url.replace(/\/$/, '').split('/').pop()?.replace(/\.git$/, '') || 'repo'
}

/** Safely format a timestamp string for display, returning a dash for invalid values. */
export function formatTimestamp(ts: string | null | undefined): string {
  if (!ts) return INVALID_DATE_FALLBACK
  const parsed = parseApiTimestamp(ts)
  return Number.isNaN(parsed.getTime()) ? INVALID_DATE_FALLBACK : parsed.toLocaleString()
}
