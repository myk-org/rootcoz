import { describe, expect, it } from 'vitest'
import { buildRepoOptions, formatRepoRef, normalizeRepoUrl } from '../repoOptions'

describe('normalizeRepoUrl', () => {
  it('strips trailing .git and trailing slashes while preserving path case', () => {
    expect(normalizeRepoUrl('https://github.com/kubevirt/containerized-data-importer.git')).toBe(
      'https://github.com/kubevirt/containerized-data-importer',
    )
    expect(normalizeRepoUrl('https://github.com/kubevirt/containerized-data-importer/')).toBe(
      'https://github.com/kubevirt/containerized-data-importer',
    )
    expect(normalizeRepoUrl('https://GitHub.com/KubeVirt/containerized-data-importer.git/')).toBe(
      'https://github.com/KubeVirt/containerized-data-importer',
    )
  })

  it('handles non-URL git remotes cleanly', () => {
    expect(normalizeRepoUrl('git@github.com:kubevirt/cdi.git/')).toBe('git@github.com:kubevirt/cdi')
  })
})

describe('formatRepoRef', () => {
  it('shortens 40-char commit SHAs to 7 chars and keeps branch names intact', () => {
    expect(formatRepoRef('87b98b49e2c5802816cbb787dadac9475c0cab32')).toBe('87b98b4') // pragma: allowlist secret
    expect(formatRepoRef('main')).toBe('main')
    expect(formatRepoRef('release-v1.60')).toBe('release-v1.60')
  })
})

describe('buildRepoOptions', () => {
  it('keeps a single repo with its clean display name unchanged', () => {
    const options = buildRepoOptions([
      {
        name: 'containerized-data-importer',
        url: 'https://github.com/kubevirt/containerized-data-importer',
        ref: 'main',
      },
    ])

    expect(options).toHaveLength(1)
    expect(options[0].label).toBe('containerized-data-importer')
    expect(options[0].url).toBe('https://github.com/kubevirt/containerized-data-importer')
  })

  it('keeps distinct repo display names clean without ref suffixes', () => {
    const options = buildRepoOptions([
      { name: 'tests-repo', url: 'https://github.com/org/tests-repo', ref: 'main' },
      { name: 'product-repo', url: 'https://github.com/org/product-repo', ref: 'release-1.0' },
    ])

    expect(options.map((o) => o.label)).toEqual(['tests-repo', 'product-repo'])
    expect(new Set(options.map((o) => o.value)).size).toBe(2)
  })

  it('collapses duplicate entries with the same normalized URL and same ref into one entry', () => {
    const options = buildRepoOptions([
      {
        name: 'containerized-data-importer',
        url: 'https://github.com/kubevirt/containerized-data-importer.git',
        ref: 'main',
      },
      {
        name: 'containerized-data-importer',
        url: 'https://github.com/kubevirt/containerized-data-importer/',
        ref: 'main',
      },
    ])

    expect(options).toHaveLength(1)
    expect(options[0].label).toBe('containerized-data-importer')
    // Preserves the first entry's original URL for submission.
    expect(options[0].url).toBe('https://github.com/kubevirt/containerized-data-importer.git')
  })

  it('preserves same-name repos at different refs with distinct values and shortened SHA labels', () => {
    const sha = '87b98b49e2c5802816cbb787dadac9475c0cab32' // pragma: allowlist secret
    const options = buildRepoOptions([
      {
        name: 'containerized-data-importer',
        url: 'https://github.com/kubevirt/containerized-data-importer',
        ref: sha,
      },
      {
        name: 'containerized-data-importer',
        url: 'https://github.com/kubevirt/containerized-data-importer.git',
        ref: 'main',
      },
    ])

    expect(options).toHaveLength(2)
    expect(options[0].label).toBe('containerized-data-importer @ 87b98b4')
    expect(options[1].label).toBe('containerized-data-importer @ main')
    expect(options[0].value).not.toBe(options[1].value)
    // Original URLs preserved verbatim for submission.
    expect(options[0].url).toBe('https://github.com/kubevirt/containerized-data-importer')
    expect(options[1].url).toBe('https://github.com/kubevirt/containerized-data-importer.git')
  })

  it('qualifies forks that share both display name and ref', () => {
    const options = buildRepoOptions([
      { name: 'cdi', url: 'https://github.com/kubevirt/cdi', ref: 'main' },
      { name: 'cdi', url: 'https://github.com/myfork/cdi', ref: 'main' },
    ])

    expect(options).toHaveLength(2)
    expect(options[0].label).toBe('cdi @ main (kubevirt/cdi)')
    expect(options[1].label).toBe('cdi @ main (myfork/cdi)')
    expect(options[0].value).not.toBe(options[1].value)
  })
})
