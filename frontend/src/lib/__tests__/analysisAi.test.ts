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

describe('session support', () => {
  const providers: Record<string, AiModel[]> = {
    enmaas: [
      { id: 'listed-only', name: 'Listed only', provider: 'EnMaaS', credential_sources: ['server'], verified: true, session_supported: false },
      { id: 'usable', name: 'Usable', provider: 'EnMaaS', credential_sources: ['server'], verified: true, session_supported: true },
    ],
  }

  it('a pair the sidecar would refuse is not available even when it is listed', () => {
    // A saved or preselected NO SESSION pair must not leave Start enabled.
    expect(isAnalysisAiAvailable(providers, {}, 'enmaas', 'listed-only', false)).toBe(false)
    expect(isAnalysisAiAvailable(providers, {}, 'enmaas', 'usable', false)).toBe(true)
  })

  it('the unverified-model bypass does not admit a refused listed row', () => {
    // A user key without a model listing allows free typing, but a listed row
    // the sidecar refuses still must not leave Start enabled.
    const status = { enmaas: { has_api_key: true, modelListingSupported: false } }
    expect(isAnalysisAiAvailable(providers, status, 'enmaas', 'listed-only', false)).toBe(false)
    expect(isAnalysisAiAvailable(providers, status, 'enmaas', 'typed-by-hand', false)).toBe(true)
  })

  it('a model without the flag is unaffected', () => {
    expect(isAnalysisAiAvailable({ enmaas: [{ id: 'plain', name: 'Plain', provider: 'EnMaaS', credential_sources: ['server'] }] }, {}, 'enmaas', 'plain', false)).toBe(true)
  })
})
