import { describe, it, expect } from 'vitest'
import {
  ciSourceLabel,
  formatTimestamp,
  INVALID_DATE_FALLBACK,
  parseApiTimestamp,
  resolveBuildDisplayId,
  resolveBuildUrl,
  sanitizeHttpHref,
} from '../utils'

describe('ciSourceLabel', () => {
  it('returns "Prow" for prow analysis_type', () => {
    expect(ciSourceLabel({ analysis_type: 'prow' })).toBe('Prow')
  })

  it('returns "Jenkins" for jenkins analysis_type', () => {
    expect(ciSourceLabel({ analysis_type: 'jenkins' })).toBe('Jenkins')
  })

  it('returns "CI Build" for file analysis_type', () => {
    expect(ciSourceLabel({ analysis_type: 'file' })).toBe('CI Build')
  })

  it('returns "CI Build" for raw analysis_type', () => {
    expect(ciSourceLabel({ analysis_type: 'raw' })).toBe('CI Build')
  })

  it('defaults to "Jenkins" for missing analysis_type', () => {
    expect(ciSourceLabel({ ai_provider: 'claude' })).toBe('Jenkins')
  })

  it('defaults to "Jenkins" for undefined request_params', () => {
    expect(ciSourceLabel(undefined)).toBe('Jenkins')
  })

  it('defaults to "Jenkins" for empty request_params', () => {
    expect(ciSourceLabel({})).toBe('Jenkins')
  })

  it('defaults to "Jenkins" for unknown analysis_type', () => {
    expect(ciSourceLabel({ analysis_type: 'unknown_ci' })).toBe('Jenkins')
  })
})

describe('sanitizeHttpHref', () => {
  it('strips userinfo from http(s) URLs', () => {
    expect(sanitizeHttpHref('https://token@github.com/org/repo')).toBe(
      'https://github.com/org/repo',
    )
    expect(sanitizeHttpHref('https://user:pass@host.example/path')).toBe( // pragma: allowlist secret
      'https://host.example/path',
    )
  })

  it('rejects non-http schemes and invalid values', () => {
    expect(sanitizeHttpHref('javascript:alert(1)')).toBeNull()
    expect(sanitizeHttpHref('github.com/org/repo')).toBeNull()
    expect(sanitizeHttpHref('')).toBeNull()
  })
})

describe('resolveBuildUrl', () => {
  it('prefers build_url over jenkins_url', () => {
    expect(resolveBuildUrl({ build_url: 'https://prow.example/build', jenkins_url: 'https://jenkins.example/job/1' }))
      .toBe('https://prow.example/build')
  })

  it('falls back to jenkins_url', () => {
    expect(resolveBuildUrl({ jenkins_url: 'https://jenkins.example/job/1' }))
      .toBe('https://jenkins.example/job/1')
  })

  it('returns null when neither field is set', () => {
    expect(resolveBuildUrl({})).toBeNull()
    expect(resolveBuildUrl(null)).toBeNull()
  })

  it('falls back to jenkins_url when build_url is invalid', () => {
    expect(resolveBuildUrl({
      build_url: 'javascript:alert(1)',
      jenkins_url: 'https://jenkins.example/job/1',
    })).toBe('https://jenkins.example/job/1')
  })

  it('strips credentials and rejects unsafe schemes', () => {
    expect(resolveBuildUrl({ build_url: 'https://user:pass@prow.example.com/job' })) // pragma: allowlist secret
      .toBe('https://prow.example.com/job')
    expect(resolveBuildUrl({ build_url: 'javascript:alert(1)' })).toBeNull()
  })
})

describe('resolveBuildDisplayId', () => {
  it('prefers request_params.build_id for Prow', () => {
    expect(resolveBuildDisplayId({
      build_number: 42,
      request_params: { build_id: '2072664659076321280' },
    })).toBe('2072664659076321280')
  })

  it('falls back to build_id then build_number', () => {
    expect(resolveBuildDisplayId({ build_id: '999' })).toBe('999')
    expect(resolveBuildDisplayId({ build_number: 42 })).toBe('42')
  })

  it('returns null for missing build info', () => {
    expect(resolveBuildDisplayId({})).toBeNull()
    expect(resolveBuildDisplayId({ build_number: 0 })).toBeNull()
  })
})

describe('parseApiTimestamp', () => {
  const EXPECTED_UTC_MS = Date.UTC(2026, 10, 20, 14, 27, 56)

  it('parses naive SQLite UTC strings as UTC rather than local time', () => {
    expect(parseApiTimestamp('2026-11-20 14:27:56').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp(' 2026-11-20 14:27:56 ').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp('2026-11-20T14:27:56').getTime()).toBe(EXPECTED_UTC_MS)
  })

  it('does not shift timestamps that already carry Z or an explicit offset', () => {
    expect(parseApiTimestamp('2026-11-20T14:27:56Z').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp('2026-11-20T14:27:56z').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp('2026-11-20T16:27:56+02:00').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp('2026-11-20T09:27:56-05:00').getTime()).toBe(EXPECTED_UTC_MS)
    expect(parseApiTimestamp('2026-11-20 16:27:56+0200').getTime()).toBe(EXPECTED_UTC_MS)
  })
})

describe('formatTimestamp', () => {
  const EXPECTED_LOCAL = new Date(Date.UTC(2026, 10, 20, 14, 27, 56)).toLocaleString()

  it('formats naive SQLite UTC strings in local time', () => {
    expect(formatTimestamp('2026-11-20 14:27:56')).toBe(EXPECTED_LOCAL)
  })

  it('does not double-shift timestamps with Z or explicit offsets', () => {
    expect(formatTimestamp('2026-11-20T14:27:56Z')).toBe(EXPECTED_LOCAL)
    expect(formatTimestamp('2026-11-20T16:27:56+02:00')).toBe(EXPECTED_LOCAL)
    expect(formatTimestamp('2026-11-20T09:27:56-05:00')).toBe(EXPECTED_LOCAL)
  })

  it('returns the fallback dash for empty or invalid timestamps', () => {
    expect(formatTimestamp(null)).toBe(INVALID_DATE_FALLBACK)
    expect(formatTimestamp(undefined)).toBe(INVALID_DATE_FALLBACK)
    expect(formatTimestamp('')).toBe(INVALID_DATE_FALLBACK)
    expect(formatTimestamp('not-a-date')).toBe(INVALID_DATE_FALLBACK)
  })
})
