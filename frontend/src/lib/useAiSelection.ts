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
 * A fallback is judged by the same catalog rule as a remembered pair: one the server
 * cannot serve is no selection at all (#591), and the stored default is never written.
 */
export function useAiSelection(
  fallbackProvider = '',
  fallbackModel = '',
  { respectCleared = true }: { respectCleared?: boolean } = {},
) {
  const {
    username, canUseServerProviders, canUseFreeProviders, authenticated, loading: authLoading,
  } = useAuth()
  const { providers, providerKeys, providerStatus, settled: catalogSettled } = useProviderCatalog()
  // An empty or failed /api/ai-models says nothing about any pair.
  const catalogLoaded = providerKeys.length > 0
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

  // The one availability rule, shared by a remembered pair and a surface fallback:
  // a pair the loaded catalog cannot serve is not a selection (#591).
  const usable = useCallback((provider: string, model: string) => (
    catalogLoaded && isAnalysisAiAvailable(providers, providerStatus, provider, model, false, canUseServerProviders, canUseFreeProviders)
  ), [catalogLoaded, providers, providerStatus, canUseServerProviders, canUseFreeProviders])

  // A remembered pair this user can no longer use would be a dead selection, so it is
  // dropped and the surface falls back to its own default. Judged on the entry only —
  // a draft is this mount's business and must not keep the check from running.
  const pair = entry?.pair
  useEffect(() => {
    if (!entry || entry.owner !== username) return
    if (!pair || !pair.provider || !pair.model) return
    // An empty or failed /api/ai-models must not wipe a good pair, and the server
    // grants are still false until /api/auth/me resolves.
    if (!catalogLoaded || !authenticated || authLoading) return
    if (usable(pair.provider, pair.model)) return
    console.debug('[ai-pair] dropped unavailable remembered pair', `${pair.provider}/${pair.model}`)
    // Removing the entry is enough: any draft keyed to it stops being shown.
    writeLastAiPair(entry.owner, null)
  }, [entry, pair, username, catalogLoaded, authenticated, authLoading, usable])

  // The fallback (a surface's own default, or the stored server default) is applied
  // only once the catalog proves this server serves it. Before that nothing is applied:
  // guessing would issue a model listing for a provider absent from the catalog, which
  // the server answers 400 — the bug this guard exists to prevent. The stored default is
  // read, never written, cleared or replaced here.
  const fallbackApplied = fallbackProvider === '' || usable(fallbackProvider, fallbackModel)
  useEffect(() => {
    if (fallbackProvider === '' || fallbackApplied || !catalogLoaded) return
    console.debug('[ai-pair] ignored an unavailable default pair', `${fallbackProvider}/${fallbackModel}`)
  }, [fallbackApplied, fallbackProvider, fallbackModel, catalogLoaded])

  return {
    aiProvider: sel ? sel.provider : fallbackApplied ? normalizeProvider(fallbackProvider) : '',
    aiModel: sel ? sel.model : fallbackApplied ? fallbackModel : '',
    // A supplied fallback is undecided, not absent, until the catalog answers: a surface
    // that sends the pair (issue preview) must wait instead of posting an empty one (#591).
    aiPending: fallbackProvider !== '' && !sel && !catalogSettled,
    setAiPair,
  }
}
