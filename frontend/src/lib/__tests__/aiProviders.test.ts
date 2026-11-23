import { describe, expect, it } from 'vitest'
import {
  buildProviderOptions,
  collectProviderIds,
  disambiguateProviderLabels,
  extractOriginalProviderIds,
  formatProviderLabel,
  formatProviderModelLabel,
  KNOWN_PROVIDER_LABELS,
  normalizeProvider,
} from '@/lib/providerCatalog'

/**
 * Full set of built-in sidecar provider IDs (@earendil-works/pi-ai KnownProvider
 * + @myk-org/pi-vertex-claude + rootcoz public aliases). Every ID must have an
 * explicit entry in KNOWN_PROVIDER_LABELS so no internal ID leaks through.
 */
const BUILTIN_SIDECAR_PROVIDERS = [
  'amazon-bedrock',
  'ant-ling',
  'anthropic',
  'azure',
  'baseten',
  'cerebras',
  'claude',
  'cloudflare-ai-gateway',
  'cloudflare-workers-ai',
  'deepseek',
  'fireworks',
  'gemini',
  'github-copilot',
  'google',
  'google-vertex',
  'google-vertex-claude',
  'groq',
  'huggingface',
  'kimi-coding',
  'meta',
  'minimax',
  'minimax-cn',
  'mistral',
  'moonshotai',
  'moonshotai-cn',
  'nvidia',
  'openai',
  'openai-codex',
  'opencode',
  'opencode-go',
  'openrouter',
  'qwen-token-plan',
  'qwen-token-plan-cn',
  'qwen-token-plan-individual',
  'qwen-token-plan-individual-cn',
  'radius',
  'together',
  'typesafe',
  'vercel-ai-gateway',
  'xai',
  'xiaomi',
  'xiaomi-token-plan-ams',
  'xiaomi-token-plan-cn',
  'xiaomi-token-plan-sgp',
  'zai',
  'zai-coding-cn',
] as const

function findUnmappedBuiltinProviders(providerIds: readonly string[]): string[] {
  return providerIds.filter((id) => !Object.hasOwn(KNOWN_PROVIDER_LABELS, normalizeProvider(id)))
}

describe('normalizeProvider', () => {
  it('lowercases and trims provider identifiers without alias rewriting', () => {
    expect(normalizeProvider('  OpenAI  ')).toBe('openai')
    expect(normalizeProvider('Vertex-Claude')).toBe('vertex-claude')
    expect(normalizeProvider('GEMINI')).toBe('gemini')
  })
})

describe('formatProviderLabel', () => {
  it('maps every built-in sidecar provider id explicitly and flags unknown built-in ids', () => {
    expect(findUnmappedBuiltinProviders(BUILTIN_SIDECAR_PROVIDERS)).toEqual([])
    expect(findUnmappedBuiltinProviders([...BUILTIN_SIDECAR_PROVIDERS, 'unknown-sidecar-id'])).toEqual([
      'unknown-sidecar-id',
    ])

    for (const id of BUILTIN_SIDECAR_PROVIDERS) {
      const label = formatProviderLabel(id)
      expect(label).toBeTruthy()
      expect(label).not.toMatch(/google-vertex|acpx|cli/i)
      expect(label).not.toMatch(/^(Openai|Openrouter|Xai|Zai|Nvidia|Deepseek)$/)
    }
  })

  it('contains no removed Cursor, ACPX, or CLI alias mappings', () => {
    for (const removed of ['cursor', 'acpx-cursor', 'cli-cursor', 'cursor-cli', 'claude-cli', 'gemini-cli']) {
      expect(Object.hasOwn(KNOWN_PROVIDER_LABELS, removed)).toBe(false)
    }
  })

  it('maps internal Vertex and Google sidecar ids to clean product labels', () => {
    expect(formatProviderLabel('google-vertex-claude')).toBe('Claude')
    expect(formatProviderLabel('google-vertex')).toBe('Gemini')
    expect(formatProviderLabel('google')).toBe('Gemini')
    expect(formatProviderLabel('openai')).toBe('OpenAI')
    expect(formatProviderLabel('openrouter')).toBe('OpenRouter')
    expect(formatProviderLabel('anthropic')).toBe('Anthropic')
  })

  it('preserves operator-configured casing and affixes for custom models.json providers', () => {
    const originalIds = extractOriginalProviderIds({
      MyGateway: [],
      AcmeAI: [],
      'MyGateway-CLI': [],
      'cli-proxy': [],
    })
    expect(formatProviderLabel('mygateway', originalIds)).toBe('MyGateway')
    expect(formatProviderLabel('MyGateway')).toBe('MyGateway')
    expect(formatProviderLabel('acmeai', originalIds)).toBe('AcmeAI')
    expect(formatProviderLabel('mygateway-cli', originalIds)).toBe('MyGateway-CLI')
    expect(formatProviderLabel('cli-proxy', originalIds)).toBe('Cli Proxy')
    expect(formatProviderLabel('')).toBe('')
  })

  it('does not disguise unmapped non-catalog ids with title-case fallback', () => {
    expect(formatProviderLabel('unknown-builtin-id')).toBe('unknown-builtin-id')
  })

  it('does not collide with Object.prototype properties such as constructor or toString', () => {
    const customOriginals = extractOriginalProviderIds({ constructor: [], toString: [] })
    expect(formatProviderLabel('constructor', customOriginals)).toBe('Constructor')
    expect(formatProviderLabel('toString', customOriginals)).toBe('toString')
    expect(buildProviderOptions(['constructor', 'toString', 'openai'], [], customOriginals)).toEqual([
      { value: 'constructor', label: 'Constructor' },
      { value: 'openai', label: 'OpenAI' },
      { value: 'tostring', label: 'toString' },
    ])
  })
})

describe('formatProviderModelLabel', () => {
  it('formats provider and optional model consistently using originalIds', () => {
    const originalIds = extractOriginalProviderIds({ AcmeAI: [] })
    expect(formatProviderModelLabel('google-vertex-claude', 'claude-sonnet-4-6')).toBe('Claude / claude-sonnet-4-6')
    expect(formatProviderModelLabel('acmeai', 'model-1', originalIds)).toBe('AcmeAI / model-1')
    expect(formatProviderModelLabel('acmeai', 'model-1', originalIds, '/')).toBe('AcmeAI/model-1')
    expect(formatProviderModelLabel('openai', '')).toBe('OpenAI')
    expect(formatProviderModelLabel(undefined, 'gpt-5')).toBe('')
  })
})

describe('buildProviderOptions & disambiguateProviderLabels', () => {
  it('maps built-in sidecar ids and custom providers sorted by label', () => {
    expect(buildProviderOptions([])).toEqual([])
    expect(
      buildProviderOptions(
        ['openai', 'google-vertex-claude', 'google-vertex', 'mygateway'],
        [],
        { mygateway: 'MyGateway' },
      ),
    ).toEqual([
      { value: 'google-vertex-claude', label: 'Claude' },
      { value: 'google-vertex', label: 'Gemini' },
      { value: 'mygateway', label: 'MyGateway' },
      { value: 'openai', label: 'OpenAI' },
    ])
  })

  it('disambiguates distinct provider ids that share a public label in the same catalog', () => {
    expect(buildProviderOptions(['google', 'google-vertex', 'openai'])).toEqual([
      { value: 'google', label: 'Gemini (API)' },
      { value: 'google-vertex', label: 'Gemini (Vertex)' },
      { value: 'openai', label: 'OpenAI' },
    ])
    expect(disambiguateProviderLabels(['google-vertex'])).toEqual({
      'google-vertex': 'Gemini',
    })
  })

  it('normalizes current selection casing and whitespace', () => {
    expect(buildProviderOptions([], ['  OpenAI  '])).toEqual([
      { value: 'openai', label: 'OpenAI' },
    ])
  })

  it('keeps current selection even without models', () => {
    expect(buildProviderOptions(['gemini'], ['claude'])).toEqual([
      { value: 'claude', label: 'Claude' },
      { value: 'gemini', label: 'Gemini' },
    ])
  })

  it('preserves full-catalog disambiguated labels when filtering selectable provider options', () => {
    const fullLabels = disambiguateProviderLabels(['google', 'google-vertex'])
    expect(buildProviderOptions(['google-vertex'], [], undefined, fullLabels)).toEqual([
      { value: 'google-vertex', label: 'Gemini (Vertex)' },
    ])
  })

  it('collectProviderIds flattens single strings and iterables while dropping empty values', () => {
    expect(
      collectProviderIds(['google', null, '', '  '], ' google-vertex ', undefined, ['openai']),
    ).toEqual(['google', 'google-vertex', 'openai'])
  })
})
