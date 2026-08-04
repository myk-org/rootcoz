import { AlertTriangle, Lightbulb } from 'lucide-react'
import type { CrossFailurePattern } from '../../types'

export function CrossFailurePatterns({ patterns }: { patterns: CrossFailurePattern[] }) {
  if (!patterns || patterns.length === 0) return null

  return (
    <section className="animate-slide-up">
      <h2 className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-3">
        Cross-Failure Patterns ({patterns.length})
      </h2>
      <div className="space-y-3">
        {patterns.map((p, i) => (
          <div
            key={`cfp-${i}`}
            className="rounded-lg border border-border-default bg-bg-secondary p-4"
          >
            <div className="flex items-start gap-2 mb-2">
              <AlertTriangle className="h-4 w-4 text-signal-orange mt-0.5 shrink-0" />
              <p className="text-sm font-medium text-text-primary">{p.pattern}</p>
            </div>
            {p.suggested_root_cause && (
              <div className="flex items-start gap-2 mb-2 ml-6">
                <Lightbulb className="h-3.5 w-3.5 text-signal-blue mt-0.5 shrink-0" />
                <p className="text-sm text-text-secondary">{p.suggested_root_cause}</p>
              </div>
            )}
            {p.affected_tests.length > 0 && (
              <div className="ml-6 mt-2">
                <p className="text-xs text-text-tertiary mb-1">
                  Affected tests ({p.affected_tests.length}):
                </p>
                <div className="flex flex-wrap gap-1">
                  {p.affected_tests.map((test) => (
                    <span
                      key={test}
                      className="inline-block rounded bg-bg-tertiary px-2 py-0.5 text-xs font-mono text-text-secondary"
                    >
                      {test}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
