import type { ExporterPushResult } from '@/types'

export type GreenwavePushOutcome = 'failure' | 'partial' | 'noop' | 'success'

export interface GreenwavePushState {
  outcome: GreenwavePushOutcome
  title: string
}

/**
 * Maps a greenwave push roundtrip to one of the four result states —
 * the single source for every greenwave result display.
 *
 * Backend precedence: a hard error / failed push, then a partial push,
 * then a no-op (nothing matched the outcome map — not a failure), then success.
 */
export function greenwavePushState(
  result: ExporterPushResult | null,
  error: string | null,
): GreenwavePushState | null {
  if (result === null && !error) return null
  const pushed = result?.pushed ?? 0
  const hasResultErrors = (result?.errors?.length ?? 0) > 0
  if (error || (result !== null && !result.success && (hasResultErrors || pushed > 0))) {
    return { outcome: 'failure', title: 'Failed to push to Greenwave.' }
  }
  if (result !== null && result.success && hasResultErrors) {
    return { outcome: 'partial', title: 'Some results could not be pushed to Greenwave.' }
  }
  if (result !== null && pushed === 0 && !hasResultErrors) {
    return { outcome: 'noop', title: 'No results could be pushed — no failure matched GREENWAVE_OUTCOME_MAP.' }
  }
  return { outcome: 'success', title: `Pushed ${pushed} result${pushed !== 1 ? 's' : ''} to Greenwave.` }
}
