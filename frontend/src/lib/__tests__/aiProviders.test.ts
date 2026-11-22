import { describe, expect, it } from 'vitest'
import {
  buildProviderOptions,
  formatProviderLabel,
  KNOWN_PROVIDER_LABELS,
  normalizeProvider,
} from '@/lib/aiProviders'

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

describe('normalizeProvider', () => {
  it('lowercases and trims provider identifiers without alias rewriting', () => {
    expect(normalizeProvider('  OpenAI  ')).toBe('openai')
    expect(normalizeProvider('Vertex-Claude')).toBe('vertex-claude')
    expect(normalizeProvider('GEMINI')).toBe('gemini')
  })
})

describe('formatProviderLabel', () => {
  it('maps every built-in sidecar provider id to an explicit public label without internal prefixes', () => {
    for (const id of BUILTIN_SIDECAR_PROVIDERS) {
      expect(KNOWN_PROVIDER_LABELS).toHaveProperty(id)
      const label = formatProviderLabel(id)
      expect(label).toBeTruthy()
      expect(label).not.toMatch(/google-vertex|acpx|cli/i)
      expect(label).not.toMatch(/^(Openai|Openrouter|Xai|Zai|Nvidia|Deepseek)$/)
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

  it('strips legacy acpx- and *-cli wrappers instead of leaking them', () => {
    expect(formatProviderLabel('acpx-cursor')).toBe('Cursor')
    expect(formatProviderLabel('cli-cursor')).toBe('Cursor')
    expect(formatProviderLabel('cursor-cli')).toBe('Cursor')
    expect(formatProviderLabel('claude-cli')).toBe('Claude')
    expect(formatProviderLabel('gemini-cli')).toBe('Gemini')
  })

  it('preserves operator-configured casing for custom models.json providers consistently', () => {
    const originalIds = { mygateway: 'MyGateway', acmeai: 'AcmeAI' }
    // Folded catalog key + originalIds (dropdown options) matches raw casing (selected chips).
    expect(formatProviderLabel('mygateway', originalIds)).toBe('MyGateway')
    expect(formatProviderLabel('MyGateway')).toBe('MyGateway')
    expect(formatProviderLabel('MyGateway', originalIds)).toBe('MyGateway')
    expect(formatProviderLabel('acmeai', originalIds)).toBe('AcmeAI')
    // Lowercase-only unknown/legacy identifiers fall back to clean title case.
    expect(formatProviderLabel('legacy')).toBe('Legacy')
    expect(formatProviderLabel('')).toBe('')
  })
})

describe('buildProviderOptions', () => {
  it('maps built-in sidecar ids and custom providers through the shared label table sorted by label', () => {
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
})
