import { describe, it, expect } from 'vitest'
import {
  trimTrailingPunctuation,
  deduplicateMatches,
  autoLinkAnalysis,
  buildFileUrl,
  matchRepo,
  type LinkMatch,
  type RepoUrl,
} from '../autoLink'

/* ------------------------------------------------------------------ */
/*  trimTrailingPunctuation                                            */
/* ------------------------------------------------------------------ */

describe('trimTrailingPunctuation', () => {
  it('strips trailing period', () => {
    expect(trimTrailingPunctuation('https://example.com.')).toBe('https://example.com')
  })

  it('strips trailing comma and semicolon', () => {
    expect(trimTrailingPunctuation('https://example.com,')).toBe('https://example.com')
    expect(trimTrailingPunctuation('https://example.com;')).toBe('https://example.com')
  })

  it('strips trailing angle bracket', () => {
    expect(trimTrailingPunctuation('https://example.com>')).toBe('https://example.com')
  })

  it('strips unbalanced trailing paren', () => {
    expect(trimTrailingPunctuation('https://example.com/path)')).toBe('https://example.com/path')
  })

  it('keeps balanced parens', () => {
    expect(trimTrailingPunctuation('https://en.wikipedia.org/wiki/Foo_(bar)')).toBe(
      'https://en.wikipedia.org/wiki/Foo_(bar)',
    )
  })

  it('strips multiple trailing punctuation chars', () => {
    // Period is stripped first, then the unbalanced paren
    expect(trimTrailingPunctuation('https://example.com/path).')).toBe('https://example.com/path')
  })
})

/* ------------------------------------------------------------------ */
/*  deduplicateMatches                                                 */
/* ------------------------------------------------------------------ */

describe('deduplicateMatches', () => {
  it('removes overlapping matches keeping earlier one', () => {
    const matches: LinkMatch[] = [
      { start: 0, end: 30, text: 'first', href: 'https://a.com' },
      { start: 10, end: 40, text: 'overlap', href: 'https://b.com' },
      { start: 50, end: 70, text: 'no-overlap', href: 'https://c.com' },
    ]
    const result = deduplicateMatches(matches)
    expect(result).toHaveLength(2)
    expect(result[0].text).toBe('first')
    expect(result[1].text).toBe('no-overlap')
  })

  it('returns empty for empty input', () => {
    expect(deduplicateMatches([])).toEqual([])
  })

  it('keeps all non-overlapping matches', () => {
    const matches: LinkMatch[] = [
      { start: 0, end: 5, text: 'a', href: 'x' },
      { start: 5, end: 10, text: 'b', href: 'y' },
      { start: 10, end: 15, text: 'c', href: 'z' },
    ]
    expect(deduplicateMatches(matches)).toHaveLength(3)
  })
})

/* ------------------------------------------------------------------ */
/*  buildFileUrl                                                       */
/* ------------------------------------------------------------------ */

describe('buildFileUrl', () => {
  it('builds URL without line number', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'src/main.py')).toBe(
      'https://github.com/org/repo/blob/HEAD/src/main.py',
    )
  })

  it('builds URL with numeric line number', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'src/main.py', 42)).toBe(
      'https://github.com/org/repo/blob/HEAD/src/main.py#L42',
    )
  })

  it('builds URL with string line number', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'src/main.py', '99')).toBe(
      'https://github.com/org/repo/blob/HEAD/src/main.py#L99',
    )
  })

  it('strips trailing slash from base URL', () => {
    expect(buildFileUrl('https://github.com/org/repo/', 'conftest.py')).toBe(
      'https://github.com/org/repo/blob/HEAD/conftest.py',
    )
  })

  it('handles undefined line number', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'file.ts', undefined)).toBe(
      'https://github.com/org/repo/blob/HEAD/file.ts',
    )
  })

  it('uses custom ref in URL', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'src/main.py', undefined, 'develop')).toBe(
      'https://github.com/org/repo/blob/develop/src/main.py',
    )
  })

  it('handles line number 0', () => {
    expect(buildFileUrl('https://github.com/org/repo', 'file.py', 0)).toBe(
      'https://github.com/org/repo/blob/HEAD/file.py#L0',
    )
  })
})

/* ------------------------------------------------------------------ */
/*  autoLinkAnalysis with empty repoUrls (URL-only mode)               */
/* ------------------------------------------------------------------ */

describe('autoLinkAnalysis with empty repoUrls (URL-only mode)', () => {
  it('converts GitHub PR URL to named link', () => {
    const segments = autoLinkAnalysis('see https://github.com/org/repo/pull/42 for details', [])
    expect(segments).toEqual([
      { type: 'text', text: 'see ' },
      { type: 'link', text: 'org/repo#42', href: 'https://github.com/org/repo/pull/42' },
      { type: 'text', text: ' for details' },
    ])
  })

  it('converts GitHub issue URL to named link', () => {
    const segments = autoLinkAnalysis('fix https://github.com/org/repo/issues/99', [])
    expect(segments).toEqual([
      { type: 'text', text: 'fix ' },
      { type: 'link', text: 'org/repo#99', href: 'https://github.com/org/repo/issues/99' },
    ])
  })

  it('converts Jira browse URL to ticket key', () => {
    const segments = autoLinkAnalysis('tracked in https://jira.example.com/browse/PROJ-123 already', [])
    expect(segments).toEqual([
      { type: 'text', text: 'tracked in ' },
      { type: 'link', text: 'PROJ-123', href: 'https://jira.example.com/browse/PROJ-123' },
      { type: 'text', text: ' already' },
    ])
  })

  it('converts generic URL to clickable link', () => {
    const segments = autoLinkAnalysis('visit https://docs.example.com/guide please', [])
    expect(segments).toEqual([
      { type: 'text', text: 'visit ' },
      { type: 'link', text: 'https://docs.example.com/guide', href: 'https://docs.example.com/guide' },
      { type: 'text', text: ' please' },
    ])
  })

  it('returns text-only segment when no URLs present', () => {
    const segments = autoLinkAnalysis('plain text without links', [])
    expect(segments).toEqual([{ type: 'text', text: 'plain text without links' }])
  })
})

/* ------------------------------------------------------------------ */
/*  autoLinkAnalysis                                                   */
/* ------------------------------------------------------------------ */

describe('autoLinkAnalysis', () => {
  const repo: RepoUrl[] = [{ name: 'my-repo', url: 'https://github.com/org/my-repo', ref: 'main' }]

  it('links conftest.py with repo URL', () => {
    const segments = autoLinkAnalysis('see conftest.py for setup', repo)
    expect(segments).toEqual([
      { type: 'text', text: 'see ' },
      { type: 'link', text: 'conftest.py', href: 'https://github.com/org/my-repo/blob/main/conftest.py' },
      { type: 'text', text: ' for setup' },
    ])
  })

  it('links file path with line number', () => {
    const segments = autoLinkAnalysis('error at utilities/utils.py:361', repo)
    expect(segments).toEqual([
      { type: 'text', text: 'error at ' },
      {
        type: 'link',
        text: 'utilities/utils.py:361',
        href: 'https://github.com/org/my-repo/blob/main/utilities/utils.py#L361',
      },
    ])
  })

  it('links deep nested file path', () => {
    const segments = autoLinkAnalysis('check path/to/deep/file.yaml', repo)
    expect(segments).toEqual([
      { type: 'text', text: 'check ' },
      {
        type: 'link',
        text: 'path/to/deep/file.yaml',
        href: 'https://github.com/org/my-repo/blob/main/path/to/deep/file.yaml',
      },
    ])
  })

  it('returns text-only segments when no file paths present', () => {
    const segments = autoLinkAnalysis('no files here', repo)
    expect(segments).toEqual([{ type: 'text', text: 'no files here' }])
  })

  it('skips file-path matching when repoUrls is empty', () => {
    const segments = autoLinkAnalysis('see conftest.py for setup', [])
    expect(segments).toEqual([{ type: 'text', text: 'see conftest.py for setup' }])
  })

  it('does not double-match URLs containing .py (URL takes priority)', () => {
    const text = 'see https://github.com/org/repo/blob/main/conftest.py for details'
    const segments = autoLinkAnalysis(text, repo)
    // The whole URL should be a single link, not split into URL + file path
    const links = segments.filter((s) => s.type === 'link')
    expect(links).toHaveLength(1)
    expect(links[0].href).toBe('https://github.com/org/repo/blob/main/conftest.py')
  })

  it('handles mixed text with both URLs and file paths', () => {
    const text = 'Fix conftest.py as shown in https://github.com/org/repo/pull/10 review'
    const segments = autoLinkAnalysis(text, repo)
    const links = segments.filter((s) => s.type === 'link')
    expect(links).toHaveLength(2)
    expect(links[0].text).toBe('conftest.py')
    expect(links[0].href).toBe('https://github.com/org/my-repo/blob/main/conftest.py')
    expect(links[1].text).toBe('org/repo#10')
    expect(links[1].href).toBe('https://github.com/org/repo/pull/10')
  })

  it('strips trailing slash from repo URL before building href', () => {
    const repoWithSlash: RepoUrl[] = [{ name: 'repo', url: 'https://github.com/org/repo/', ref: 'main' }]
    const segments = autoLinkAnalysis('edit main.py now', repoWithSlash)
    const link = segments.find((s) => s.type === 'link')
    expect(link?.href).toBe('https://github.com/org/repo/blob/main/main.py')
  })

  it('matches various file extensions', () => {
    const extensions = ['yaml', 'yml', 'json', 'toml', 'sh', 'js', 'ts', 'go', 'java', 'groovy', 'rs']
    for (const ext of extensions) {
      const segments = autoLinkAnalysis(`file config.${ext} here`, repo)
      const links = segments.filter((s) => s.type === 'link')
      expect(links).toHaveLength(1)
      expect(links[0].text).toBe(`config.${ext}`)
    }
  })

  it('also detects URLs via the URL matchers', () => {
    const text = 'see https://jira.example.com/browse/BUG-42 for context'
    const segments = autoLinkAnalysis(text, repo)
    const links = segments.filter((s) => s.type === 'link')
    expect(links).toHaveLength(1)
    expect(links[0].text).toBe('BUG-42')
  })

  it('matches file path to correct repo by name prefix', () => {
    const repos: RepoUrl[] = [
      { name: 'tests', url: 'https://github.com/org/tests', ref: 'main' },
      { name: 'infra', url: 'https://github.com/org/infra', ref: 'develop' },
    ]
    const segments = autoLinkAnalysis('check infra/config.yaml here', repos)
    const link = segments.find(s => s.type === 'link')
    // Path should be config.yaml (stripped prefix), not infra/config.yaml
    expect(link?.href).toBe('https://github.com/org/infra/blob/develop/config.yaml')
  })

  it('uses repo ref in file links', () => {
    const repos: RepoUrl[] = [{ name: 'my-repo', url: 'https://github.com/org/my-repo', ref: 'v2.0.0' }]
    const segments = autoLinkAnalysis('see conftest.py for setup', repos)
    const link = segments.find(s => s.type === 'link')
    expect(link?.href).toBe('https://github.com/org/my-repo/blob/v2.0.0/conftest.py')
  })

  it('skips unmatched file paths in multi-repo mode', () => {
    const repos: RepoUrl[] = [
      { name: 'tests', url: 'https://github.com/org/tests', ref: 'main' },
      { name: 'infra', url: 'https://github.com/org/infra', ref: 'develop' },
    ]
    const segments = autoLinkAnalysis('see conftest.py for setup', repos)
    // conftest.py doesn't match any repo prefix, so it should NOT be linked
    expect(segments).toEqual([{ type: 'text', text: 'see conftest.py for setup' }])
  })

  it('does not match version-string directory paths', () => {
    const segments = autoLinkAnalysis('Python 3.11/path.py is broken', repo)
    // Should NOT match any substring of '3.11/path.py' — the dot and digit
    // lookbehinds block matches starting after dots or digits.
    const links = segments.filter(s => s.type === 'link')
    expect(links).toHaveLength(0)
  })

  it('still matches directory paths whose name ends with a digit', () => {
    const segments = autoLinkAnalysis('see test1/file.py for details', repo)
    const links = segments.filter(s => s.type === 'link')
    expect(links).toHaveLength(1)
    expect(links[0].text).toBe('test1/file.py')
  })
})

/* ------------------------------------------------------------------ */
/*  matchRepo                                                          */
/* ------------------------------------------------------------------ */

describe('matchRepo', () => {
  it('throws when repos is empty', () => {
    expect(() => matchRepo('some/file.py', [])).toThrow('matchRepo requires at least one repo')
  })

  it('returns repo with prefixMatched true when path starts with repo name', () => {
    const repos: RepoUrl[] = [
      { name: 'tests', url: 'https://github.com/org/tests', ref: 'main' },
      { name: 'infra', url: 'https://github.com/org/infra', ref: 'develop' },
    ]
    const result = matchRepo('infra/config.yaml', repos)
    expect(result).toEqual({ repo: repos[1], prefixMatched: true })
  })

  it('falls back to first repo in single-repo mode when no prefix matches', () => {
    const repos: RepoUrl[] = [{ name: 'my-repo', url: 'https://github.com/org/my-repo', ref: 'main' }]
    const result = matchRepo('conftest.py', repos)
    expect(result).toEqual({ repo: repos[0], prefixMatched: false })
  })

  it('returns no repo in multi-repo mode when no prefix matches', () => {
    const repos: RepoUrl[] = [
      { name: 'tests', url: 'https://github.com/org/tests', ref: 'main' },
      { name: 'infra', url: 'https://github.com/org/infra', ref: 'develop' },
    ]
    const result = matchRepo('conftest.py', repos)
    expect(result).toEqual({ prefixMatched: false })
    expect(result.repo).toBeUndefined()
  })
})
