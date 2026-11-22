import { repoNameFromUrl } from '@/lib/utils'

export interface RepoEntry {
  name: string
  url: string
  ref?: string
}

export interface RepoOption {
  /** Stable unique identifier built from normalized URL + ref (never display name). */
  value: string
  /** Display name as provided (or derived from URL). */
  name: string
  /** Original repository URL preserved verbatim for API submission. */
  url: string
  /** Original git ref, if provided. */
  ref?: string
  /** Human-readable label (disambiguated only when names or URLs collide). */
  label: string
}

const FULL_COMMIT_SHA_RE = /^[0-9a-f]{40,64}$/i

/** Normalize a repository URL for identity comparison while preserving case of repo path. */
export function normalizeRepoUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, '').replace(/\.git$/i, '').replace(/\/+$/, '')
  try {
    const parsed = new URL(trimmed)
    parsed.username = ''
    parsed.password = ''
    parsed.pathname = parsed.pathname.replace(/\/+$/, '').replace(/\.git$/i, '').replace(/\/+$/, '')
    return parsed.toString().replace(/\/+$/, '')
  } catch {
    return trimmed
  }
}

/** Shorten full 40–64 char hex commit SHAs to 7 chars while keeping branch/tag names readable. */
export function formatRepoRef(ref: string): string {
  const trimmed = ref.trim()
  return FULL_COMMIT_SHA_RE.test(trimmed) ? trimmed.slice(0, 7) : trimmed
}

function repoPathQualifier(normalizedUrl: string): string {
  try {
    const parsed = new URL(normalizedUrl)
    return parsed.pathname.replace(/^\/+/, '') || parsed.host
  } catch {
    return normalizedUrl
  }
}

function repoHostPathQualifier(normalizedUrl: string): string {
  try {
    const parsed = new URL(normalizedUrl)
    const path = parsed.pathname.replace(/^\/+/, '')
    return path ? `${parsed.host}/${path}` : parsed.host
  } catch {
    return normalizedUrl
  }
}

function countLabels(items: Array<{ label: string }>): Map<string, number> {
  const counts = new Map<string, number>()
  for (const item of items) {
    counts.set(item.label, (counts.get(item.label) ?? 0) + 1)
  }
  return counts
}

/**
 * Build deduplicated, stably-keyed repository picker options.
 *
 * - Collapses entries sharing the same normalized URL and ref into one option
 *   (keeping the first entry's original URL/ref for submission).
 * - Preserves entries with the same name or URL but different refs as distinct
 *   options with disambiguated labels (`name @ shortRef`).
 * - Leaves unique display names unchanged.
 */
export function buildRepoOptions(repos?: RepoEntry[]): RepoOption[] {
  if (!repos || repos.length === 0) return []

  const seen = new Set<string>()
  const unique: Array<{
    value: string
    normalizedUrl: string
    normalizedRef: string
    name: string
    url: string
    ref?: string
  }> = []

  for (const repo of repos) {
    const rawUrl = repo.url.trim()
    if (!rawUrl) continue
    const normalizedUrl = normalizeRepoUrl(rawUrl)
    const normalizedRef = (repo.ref ?? '').trim()
    const value = `${normalizedUrl}::${normalizedRef}`
    if (seen.has(value)) continue
    seen.add(value)
    unique.push({
      value,
      normalizedUrl,
      normalizedRef,
      name: repo.name.trim() || repoNameFromUrl(rawUrl),
      url: repo.url,
      ref: repo.ref,
    })
  }

  const nameCounts = new Map<string, number>()
  const urlCounts = new Map<string, number>()
  for (const entry of unique) {
    nameCounts.set(entry.name, (nameCounts.get(entry.name) ?? 0) + 1)
    urlCounts.set(entry.normalizedUrl, (urlCounts.get(entry.normalizedUrl) ?? 0) + 1)
  }

  let labeled = unique.map((entry) => {
    const collides =
      (nameCounts.get(entry.name) ?? 0) > 1 ||
      (urlCounts.get(entry.normalizedUrl) ?? 0) > 1
    const label = collides && entry.normalizedRef
      ? `${entry.name} @ ${formatRepoRef(entry.normalizedRef)}`
      : entry.name
    return { entry, baseLabel: label, label }
  })

  // If shortened commit SHAs collapse distinct refs onto the same label, expand them to full refs.
  const refsByLabel = new Map<string, Set<string>>()
  for (const item of labeled) {
    const refs = refsByLabel.get(item.label) ?? new Set<string>()
    refs.add(item.entry.normalizedRef)
    refsByLabel.set(item.label, refs)
  }
  labeled = labeled.map((item) => {
    if ((refsByLabel.get(item.label)?.size ?? 0) > 1 && item.entry.normalizedRef) {
      const expanded = `${item.entry.name} @ ${item.entry.normalizedRef}`
      return { ...item, baseLabel: expanded, label: expanded }
    }
    return item
  })

  // Fallback disambiguators when distinct URLs still share a label (forks, mirrors on different hosts/schemes).
  const qualifiers = [repoPathQualifier, repoHostPathQualifier, (url: string) => url]
  for (const qualify of qualifiers) {
    const labelCounts = countLabels(labeled)
    if (!labeled.some((item) => (labelCounts.get(item.label) ?? 0) > 1)) break
    labeled = labeled.map((item) =>
      (labelCounts.get(item.label) ?? 0) > 1
        ? { ...item, label: `${item.baseLabel} (${qualify(item.entry.normalizedUrl)})` }
        : item,
    )
  }

  return labeled.map(({ entry, label }) => ({
    value: entry.value,
    name: entry.name,
    url: entry.url,
    ref: entry.ref,
    label,
  }))
}
