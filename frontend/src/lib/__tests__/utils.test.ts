import { describe, it, expect } from 'vitest'
import { ciSourceLabel, resolveBuildUrl, resolveBuildDisplayId } from '../utils'

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
