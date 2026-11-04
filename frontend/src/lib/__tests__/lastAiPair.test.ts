import { afterEach, describe, expect, it } from 'vitest'
import { readLastAiPair, subscribeLastAiPair, writeLastAiPair } from '@/lib/lastAiPair'

afterEach(() => {
  localStorage.clear()
})

const KEY = (username: string) => `rootcoz_last_ai_pair:${username}`

describe('lastAiPair store', () => {
  it('round-trips a pair per username and keeps two users apart', () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    writeLastAiPair('bob', { provider: 'openai', model: 'gpt-4o' })

    expect(readLastAiPair('alice')).toEqual({ owner: 'alice', pair: { provider: 'claude', model: 'sonnet' } })
    expect(readLastAiPair('bob')).toEqual({ owner: 'bob', pair: { provider: 'openai', model: 'gpt-4o' } })
    // Only a provider and a model are stored — never a credential or an identity.
    expect(JSON.parse(localStorage.getItem(KEY('alice')) as string)).toEqual({ provider: 'claude', model: 'sonnet' })
  })

  it('returns the same snapshot object until the stored value changes', () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    const first = readLastAiPair('alice')
    expect(readLastAiPair('alice')).toBe(first)
    writeLastAiPair('alice', { provider: 'openai', model: 'gpt-4o' })
    expect(readLastAiPair('alice')).not.toBe(first)
  })

  it('persists nothing without a username', () => {
    writeLastAiPair('', { provider: 'claude', model: 'sonnet' })
    expect(localStorage.length).toBe(0)
    expect(readLastAiPair('')).toBeNull()
  })

  it('keeps a previous entry when a half pair is written, and only null removes it', () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    writeLastAiPair('alice', { provider: 'claude', model: '' })
    writeLastAiPair('alice', { provider: '', model: 'sonnet' })
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })
    writeLastAiPair('alice', null)
    expect(readLastAiPair('alice')).toBeNull()
    expect(localStorage.getItem(KEY('alice'))).toBeNull()
  })

  it('normalizes a legacy provider alias and keeps an explicit clear as a value', () => {
    writeLastAiPair('alice', { provider: 'cursor-cli', model: 'sonnet' })
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'cli-cursor', model: 'sonnet' })
    writeLastAiPair('alice', { provider: '', model: '' })
    expect(readLastAiPair('alice')).toEqual({ owner: 'alice', pair: { provider: '', model: '' } })
  })

  it('reads corrupt or wrongly shaped entries as absent', () => {
    localStorage.setItem(KEY('alice'), '{not json')
    expect(readLastAiPair('alice')).toBeNull()
    localStorage.setItem(KEY('alice'), JSON.stringify(['claude', 'sonnet']))
    expect(readLastAiPair('alice')).toBeNull()
    localStorage.setItem(KEY('alice'), JSON.stringify({ provider: 'claude' }))
    expect(readLastAiPair('alice')).toBeNull()
  })

  it('follows a change made in another tab and notifies the mounted surfaces', () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    const before = readLastAiPair('alice')
    const changes: string[] = []
    const unsubscribeAlice = subscribeLastAiPair('alice', () => changes.push('alice'))
    const unsubscribeBob = subscribeLastAiPair('bob', () => changes.push('bob'))

    const raw = JSON.stringify({ provider: 'openai', model: 'gpt-4o' })
    localStorage.setItem(KEY('alice'), raw)
    window.dispatchEvent(new StorageEvent('storage', { key: KEY('alice'), newValue: raw }))

    expect(changes).toEqual(['alice'])
    const after = readLastAiPair('alice')
    expect(after).not.toBe(before)
    expect(after?.pair).toEqual({ provider: 'openai', model: 'gpt-4o' })
    unsubscribeAlice()
    unsubscribeBob()
  })
})
