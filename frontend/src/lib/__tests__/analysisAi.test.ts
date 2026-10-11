import { describe, it, expect } from 'vitest'
import { completeAiPairOverride, isAnalysisAiAvailable, usableModels, visibleModels } from '../analysisAi'
import type { AiModel } from '@/types'

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

describe('free provider grants', () => {
  const providers: Record<string, AiModel[]> = {
    enmaas: [
      { id: 'glm-5-3', name: 'GLM 5.3', provider: 'EnMaaS', credential_sources: ['server'], verified: true, free: true },
      { id: 'paid-model', name: 'Paid', provider: 'EnMaaS', credential_sources: ['server'], verified: true },
    ],
  }

  it('free grant admits free server models without the server grant', () => {
    const models = usableModels(providers, 'EnMaaS', false, {}, false, true)
    expect(models.map((m) => m.id)).toEqual(['glm-5-3'])
  })

  it('free grant admits free models with the server toggle forced on', () => {
    const models = usableModels(providers, 'enmaas', true, {}, false, true)
    expect(models.map((m) => m.id)).toEqual(['glm-5-3'])
  })

  it('without any grant no server models are usable', () => {
    expect(usableModels(providers, 'enmaas', false, {}, false, false)).toHaveLength(0)
    expect(usableModels(providers, 'enmaas', true, {}, false, false)).toHaveLength(0)
  })

  it('the server grant keeps every server model usable', () => {
    expect(usableModels(providers, 'enmaas', false, {}, true, false)).toHaveLength(2)
  })

  it('visibleModels marks non-free server models locked for free-only users', () => {
    const models = visibleModels(providers, 'enmaas', false, {}, false, true)
    const byId = Object.fromEntries(models.map((m) => [m.id, m]))
    expect(byId['glm-5-3'].serverLocked).toBe(false)
    expect(byId['paid-model'].serverLocked).toBe(true)
  })

  it('isAnalysisAiAvailable accepts a free pair for a free-only user', () => {
    expect(isAnalysisAiAvailable(providers, {}, 'EnMaaS', 'glm-5-3', false, false, true)).toBe(true)
    expect(isAnalysisAiAvailable(providers, {}, 'EnMaaS', 'paid-model', false, false, true)).toBe(false)
    expect(isAnalysisAiAvailable(providers, {}, 'enmaas', 'glm-5-3', true, false, true)).toBe(true)
    expect(isAnalysisAiAvailable(providers, {}, 'enmaas', 'glm-5-3', true, false, false)).toBe(false)
  })
})
