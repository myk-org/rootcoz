/* ------------------------------------------------------------------ */
/*  Auto-link: convert URLs and file paths in text to clickable links  */
/* ------------------------------------------------------------------ */

import { repoNameFromUrl } from '@/lib/utils'

/** Pattern constant for GitHub PR URLs. Use new RegExp(...) for stateful operations. */
export const GITHUB_PR_RE = /https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)\S*/g
/** Pattern constant for GitHub issue URLs. Use new RegExp(...) for stateful operations. */
export const GITHUB_ISSUE_RE = /https?:\/\/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)\S*/g
/** Pattern constant for Jira browse URLs. Use new RegExp(...) for stateful operations. */
export const JIRA_BROWSE_RE = /https?:\/\/[^/]+\/browse\/([A-Z][A-Z0-9]+-\d+)\S*/g
/** Pattern constant for generic URLs. Use new RegExp(...) for stateful operations. */
export const GENERIC_URL_RE = /https?:\/\/\S+/g

/**
 * Matches file paths like `conftest.py`, `utils/helpers.py:42`,
 * `path/to/deep/file.yaml`, etc. Uses negative lookbehind for `://`,
 * `/`, `.`, word characters, and hyphens to avoid matching tails of
 * URLs, partial words, or fragments from version strings like
 * `3.11/path.py`.
 * Use new RegExp(...) for stateful operations.
 */
export const FILE_PATH_RE =
  /(?<![:\\/.\w-])(?:(?:[a-zA-Z0-9_-]+\/)+[a-zA-Z0-9_.-]+|[a-zA-Z0-9_-]+)\.(?:py|yaml|yml|json|cfg|ini|toml|sh|js|ts|go|java|xml|html|md|txt|conf|properties|groovy|rb|rs)(?::(\d+))?/g

export type LinkMatch = { start: number; end: number; text: string; href: string }

export interface LinkSegment {
  type: 'text' | 'link'
  text: string
  href?: string
}

export interface RepoUrl {
  name: string
  url: string
  ref: string
}

/** Check if a URL is safe to render as a clickable link (HTTP/HTTPS only). */
export function isSafeHref(href: string): boolean {
  try {
    const url = new URL(href, 'https://example.invalid')
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

export function trimTrailingPunctuation(url: string): string {
  let result = url
  while (result.length > 0) {
    const last = result[result.length - 1]
    if (last === '>') {
      result = result.slice(0, -1)
      continue
    }
    if (last === ')') {
      const opens = (result.match(/\(/g) || []).length
      const closes = (result.match(/\)/g) || []).length
      if (closes > opens) {
        result = result.slice(0, -1)
        continue
      }
      break
    }
    if (/[.,;:!?]/.test(last)) {
      result = result.slice(0, -1)
      continue
    }
    break
  }
  return result
}

export function collectMatches(
  raw: string,
  pattern: RegExp,
  matches: LinkMatch[],
  textFn: (m: RegExpMatchArray) => string,
) {
  for (const m of raw.matchAll(pattern)) {
    const start = m.index!
    const href = trimTrailingPunctuation(m[0])
    const end = start + href.length
    const text = textFn(m)
    matches.push({ start, end, text, href })
  }
}

/** Remove overlapping matches, preferring earlier entries (specific patterns added first). */
export function deduplicateMatches(matches: LinkMatch[]): LinkMatch[] {
  const sorted = [...matches].sort((a, b) => a.start - b.start)
  const result: typeof matches = []
  for (const m of sorted) {
    if (result.length > 0 && m.start < result[result.length - 1].end) continue
    result.push(m)
  }
  return result
}

/** Clone a RegExp to reset its lastIndex (required for stateful `g` flag regexes). */
function cloneRegex(pattern: RegExp): RegExp {
  return new RegExp(pattern.source, pattern.flags)
}

/** Run all URL matchers (GitHub PRs, issues, Jira, generic) and push results into matches. */
function collectUrlMatches(text: string, matches: LinkMatch[]) {
  collectMatches(text, cloneRegex(GITHUB_PR_RE), matches, (m) => `${m[1]}#${m[2]}`)
  collectMatches(text, cloneRegex(GITHUB_ISSUE_RE), matches, (m) => `${m[1]}#${m[2]}`)
  collectMatches(text, cloneRegex(JIRA_BROWSE_RE), matches, (m) => m[1])
  collectMatches(text, cloneRegex(GENERIC_URL_RE), matches, (m) => trimTrailingPunctuation(m[0]))
}

/**
 * Build a file URL from a base repo URL, file path, optional line number, and git ref.
 * Handles trailing-slash stripping and `/blob/{ref}/` + `#L` construction.
 * Defaults to 'HEAD' when ref is empty or not provided.
 */
export function buildFileUrl(baseUrl: string, filePath: string, lineNumber?: string | number, ref: string = 'HEAD'): string {
  const normalized = baseUrl.replace(/\/$/, '')
  const anchor = lineNumber != null && lineNumber !== '' ? `#L${lineNumber}` : ''
  const encodedRef = encodeURIComponent(ref || 'HEAD')
  const encodedPath = filePath.split('/').map(encodeURIComponent).join('/')
  return `${normalized}/blob/${encodedRef}/${encodedPath}${anchor}`
}

/** Build RepoUrl[] from analysis result request_params. */
export function buildRepoUrls(requestParams?: {
  tests_repo_url?: string
  tests_repo_ref?: string
  additional_repos?: Array<{ name: string; url: string; ref?: string }>
  [key: string]: unknown
}): RepoUrl[] {
  if (!requestParams) return []
  const urls: RepoUrl[] = []
  const testsUrl = requestParams.tests_repo_url
  const testsRef = (requestParams.tests_repo_ref as string) ?? ''
  if (testsUrl) urls.push({ name: repoNameFromUrl(String(testsUrl)), url: String(testsUrl), ref: testsRef || 'HEAD' })
  const additional = requestParams.additional_repos
  if (additional) {
    for (const ar of additional) {
      if (ar.url) urls.push({ name: ar.name || repoNameFromUrl(ar.url), url: ar.url, ref: ar.ref || 'HEAD' })
    }
  }
  return urls
}

/** Match a file path to the repo whose name is a prefix of the path. Falls back to first repo only in single-repo mode. */
export function matchRepo(filePath: string, repos: RepoUrl[]): { repo?: RepoUrl; prefixMatched: boolean } {
  if (repos.length === 0) throw new Error('matchRepo requires at least one repo')
  for (const repo of repos) {
    if (filePath.startsWith(repo.name + '/')) return { repo, prefixMatched: true }
  }
  return repos.length === 1
    ? { repo: repos[0], prefixMatched: false }
    : { prefixMatched: false }
}

/** Convert plain text to link segments by detecting URLs AND file paths (for analysis text). */
export function autoLinkAnalysis(text: string, repoUrls: RepoUrl[]): LinkSegment[] {
  const matches: LinkMatch[] = []

  // URL matchers (added first so they take priority during dedup)
  collectUrlMatches(text, matches)

  // File-path matcher (only if we have repo URLs to link to)
  if (repoUrls.length > 0) {
    for (const m of text.matchAll(cloneRegex(FILE_PATH_RE))) {
      const start = m.index!
      const fullMatch = m[0]
      const lineNumber = m[1] // capture group for :lineNumber
      const filePath = lineNumber ? fullMatch.slice(0, fullMatch.lastIndexOf(':')) : fullMatch
      const { repo, prefixMatched } = matchRepo(filePath, repoUrls)
      if (!repo) continue
      const relPath = prefixMatched ? filePath.slice(repo.name.length + 1) : filePath
      const href = buildFileUrl(repo.url, relPath, lineNumber, repo.ref)
      matches.push({ start, end: start + fullMatch.length, text: fullMatch, href })
    }
  }

  const deduplicated = deduplicateMatches(matches)

  return buildSegments(text, deduplicated)
}

/** Build LinkSegment[] from raw text and sorted, deduplicated matches. */
function buildSegments(raw: string, matches: LinkMatch[]): LinkSegment[] {
  const segments: LinkSegment[] = []
  let cursor = 0
  for (const m of matches) {
    if (m.start > cursor) segments.push({ type: 'text', text: raw.slice(cursor, m.start) })
    segments.push({ type: 'link', text: m.text, href: m.href })
    cursor = m.end
  }
  if (cursor < raw.length) segments.push({ type: 'text', text: raw.slice(cursor) })

  return segments.length > 0 ? segments : [{ type: 'text', text: raw }]
}
