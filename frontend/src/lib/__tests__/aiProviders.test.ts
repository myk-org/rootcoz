import { describe, expect, it } from 'vitest'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'

describe('normalizeProvider', () => {
  it('maps legacy *-cli aliases to canonical names', () => {
    expect(normalizeProvider('cursor-cli')).toBe('cursor')
    expect(normalizeProvider('claude-cli')).toBe('claude')
    expect(normalizeProvider('CURSOR')).toBe('cursor')
  })
})

describe('buildProviderOptions', () => {
  it('includes only providers with models', () => {
    expect(buildProviderOptions([]).map((o) => o.value)).toEqual([])
    expect(buildProviderOptions(['cursor']).map((o) => o.value)).toEqual([
      'cursor',
    ])
  })

  it('normalizes legacy current selection', () => {
    expect(
      buildProviderOptions([], ['cursor-cli']).map((o) => o.value),
    ).toEqual(['cursor'])
  })

  it('keeps current selection even without models', () => {
    expect(
      buildProviderOptions(['cursor'], ['claude']).map((o) => o.value),
    ).toEqual(['claude', 'cursor'])
  })
})
