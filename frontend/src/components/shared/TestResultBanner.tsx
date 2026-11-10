import { type TestIntegrationResult } from '@/lib/api'

/** Result banner for a connection test.
 *
 *  Both test endpoints (`/api/admin/settings/test/{integration}` and
 *  `/api/user/tokens/test/{integration}`) answer the same `{ok, message,
 *  details?}` shape, so admin settings and the profile page render the very
 *  same banner instead of drifting apart. */
export function TestResultBanner({
  result,
  className = 'mx-4 mb-3',
}: {
  result: TestIntegrationResult
  /** Layout offset — the admin cards own their padding, the profile form does not. */
  className?: string
}) {
  return (
    <div
      role="status"
      className={`${className} rounded-md border px-3 py-2 text-sm ${
        result.ok
          ? 'border-signal-green/30 bg-signal-green/10 text-signal-green'
          : 'border-signal-red/30 bg-signal-red/10 text-signal-red'
      }`}
    >
      <p className="font-medium">{result.ok ? 'Connection successful' : 'Connection failed'}</p>
      {result.message && <p className="mt-0.5">{result.message}</p>}
      {result.details && Object.keys(result.details).length > 0 && (
        <pre className="mt-1 overflow-x-auto rounded bg-surface-card p-2 text-xs text-text-secondary">
          {JSON.stringify(result.details, null, 2)}
        </pre>
      )}
    </div>
  )
}
