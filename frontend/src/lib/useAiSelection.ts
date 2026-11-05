import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'
import { useAuth } from '@/lib/auth'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { normalizeProvider } from '@/lib/aiProviders'
import { isAnalysisAiAvailable } from '@/lib/analysisAi'
import { readLastAiPair, subscribeLastAiPair, writeLastAiPair, type AiPair, type AiPairEntry } from '@/lib/lastAiPair'

/**
 * The last AI pair this user picked, shared by every AI-assisted surface (issue
 * creation, comment intent, feedback, chat) and remembered across page loads.
 * Three states: nothing remembered falls back to the surface defaults, an explicitly
 * cleared pair means the server default ('' — fallbacks ignored), and a remembered
 * pair is used as-is. A half pair (provider picked, model not yet, or a model still
 * being typed) is a draft local to this mount and is never persisted.
 *
 * `respectCleared: false` treats a cleared pair as "use my fallbacks" instead —
 * for surfaces that cannot run on the server default (chat needs a concrete pair).
 */
export function useAiSelection(
  fallbackProvider = '',
  fallbackModel = '',
  { respectCleared = true }: { respectCleared?: boolean } = {},
) {
  const {
    username, canUseServerProviders, canUseFreeProviders, authenticated, loading: authLoading,
  } = useAuth()
  const { providers, providerKeys, providerStatus } = useProviderCatalog()
  // Re-reads on every write for this user, so all mounted surfaces stay in sync.
  const entry = useSyncExternalStore(
    useCallback((onChange) => subscribeLastAiPair(username, onChange), [username]),
    useCallback(() => readLastAiPair(username), [username]),
  )
  // The in-progress pick, tagged with the entry it was derived from: any write for
  // this user (from here or another surface) and any username change drops it, so a
  // stale draft can never shadow the remembered pair.
  const [draft, setDraft] = useState<{ entry: AiPairEntry | null; pair: AiPair } | null>(null)
  const remembered = entry?.pair ?? null
  const clearedIgnored = !respectCleared && remembered?.provider === '' && remembered.model === ''
  const sel = draft && draft.entry === entry ? draft.pair : (clearedIgnored ? null : remembered)
  // Writes go to the user this selection was read for, never to whatever username
  // is current — an in-tab logout→login must not copy a pair into the next account.
  const owner = entry?.owner ?? username

  const setAiPair = useCallback((provider: string, model: string, { commit = true } = {}) => {
    const pair = { provider: normalizeProvider(provider), model }
    if (provider && model) {
      // Keystrokes in the model combobox arrive per character; only a pick, blur or
      // Enter may store them, or a half-typed model id becomes the remembered pair.
      if (commit) {
        writeLastAiPair(owner, pair)
        setDraft(null)
      } else setDraft({ entry, pair })
    } else if (provider || model) {
      // Mid-selection: keep the previous entry, the user has not finished picking.
      setDraft({ entry, pair })
    } else if (commit) {
      // "Server default" is a decision, so it is stored, not left absent.
      writeLastAiPair(owner, pair)
      setDraft(null)
    } else setDraft({ entry, pair })
  }, [entry, owner])

  // A remembered pair this user can no longer use would be a dead selection, so it is
  // dropped and the surface falls back to its own default. Judged on the entry only —
  // a draft is this mount's business and must not keep the check from running.
  const pair = entry?.pair
  const catalogLoaded = providerKeys.length > 0
  useEffect(() => {
    if (!entry || entry.owner !== username) return
    if (!pair || !pair.provider || !pair.model) return
    // An empty or failed /api/ai-models must not wipe a good pair, and the server
    // grants are still false until /api/auth/me resolves.
    if (!catalogLoaded || !authenticated || authLoading) return
    if (isAnalysisAiAvailable(providers, providerStatus, pair.provider, pair.model, false, canUseServerProviders, canUseFreeProviders)) return
    console.debug('[ai-pair] dropped unavailable remembered pair', `${pair.provider}/${pair.model}`)
    // Removing the entry is enough: any draft keyed to it stops being shown.
    writeLastAiPair(entry.owner, null)
  }, [entry, pair, username, catalogLoaded, authenticated, authLoading, providers, providerStatus, canUseServerProviders, canUseFreeProviders])

  return {
    aiProvider: sel ? sel.provider : normalizeProvider(fallbackProvider),
    aiModel: sel ? sel.model : fallbackModel,
    setAiPair,
  }
}
