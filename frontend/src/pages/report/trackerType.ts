/** Detect tracker type from URL string patterns (no HTTP requests). */
export function detectTrackerType(url: string): string {
  const lower = url.toLowerCase()
  if (lower.includes('github.com')) return 'github'
  if (lower.includes('jira') || lower.includes('atlassian')) return 'jira'
  return ''
}
