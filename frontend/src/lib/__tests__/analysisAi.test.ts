import { describe, it, expect } from 'vitest'
import { completeAiPairOverride } from '../analysisAi'

describe('completeAiPairOverride', () => {
  it('returns both fields for a complete pair', () => {
    expect(completeAiPairOverride('claude', 'sonnet')).toEqual({ ai_provider: 'claude', ai_model: 'sonnet' })
  })

  it('returns nothing for a provider without a model', () => {
    expect(completeAiPairOverride('claude', '')).toEqual({})
  })

  it('returns nothing for a model without a provider', () => {
    expect(completeAiPairOverride('', 'sonnet')).toEqual({})
  })

  it('returns nothing for an empty selection', () => {
    expect(completeAiPairOverride('', '')).toEqual({})
  })
})
