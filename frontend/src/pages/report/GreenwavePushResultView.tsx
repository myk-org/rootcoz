import { CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'
import type { ExporterPushResult } from '@/types'
import { greenwavePushState } from './greenwavePushState'

/**
 * The one greenwave push result display: hard failure (red), partial (amber),
 * no-op (amber) and success (green) with pushed/waived/skipped counts.
 * Rendered inside GreenwavePushForm wherever a push was attempted.
 */
export function GreenwavePushResultView({ result, error }: {
  result: ExporterPushResult | null
  error: string | null
}) {
  const state = greenwavePushState(result, error)
  if (!state) return null

  const pushed = result?.pushed ?? 0
  const waived = result?.waived ?? 0
  const hasResultErrors = (result?.errors?.length ?? 0) > 0
  const icon =
    state.outcome === 'failure' ? <XCircle className="h-5 w-5 text-signal-red" />
    : state.outcome === 'success' ? <CheckCircle2 className="h-5 w-5 text-signal-green" />
    : <AlertTriangle className="h-5 w-5 text-signal-orange" />

  return (
    <div className="space-y-3 pt-2 min-w-0 text-xs text-text-tertiary">
      <p className="flex items-center gap-2 text-sm font-medium text-text-primary">
        {icon}
        {state.title}
      </p>
      {error ? (
        <p className="text-signal-red" role="alert">{error}</p>
      ) : result && (
        <>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5">
            <span>Pushed: {pushed}</span>
            <span>Waived: {waived}</span>
            <span>Skipped: {result.skipped ?? 0}</span>
          </div>
          {result.message && <p>{result.message}</p>}
          {hasResultErrors && (
            <ul className="list-disc pl-4 space-y-1 text-signal-orange" role="alert">
              {result.errors!.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
