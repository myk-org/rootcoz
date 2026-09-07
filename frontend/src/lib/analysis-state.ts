export const ANALYSIS_STATE_OPTIONS = ['all', 'submitted', 'analyzed'] as const
export type AnalysisStateFilter = typeof ANALYSIS_STATE_OPTIONS[number]
export const ANALYSIS_STATE_LABELS: Record<AnalysisStateFilter, string> = {
  all: 'All',
  submitted: 'Submitted',
  analyzed: 'Analyzed',
}

export function analysisStateLabel(state?: string): string {
  return state === 'submitted'
    ? ANALYSIS_STATE_LABELS.submitted
    : ANALYSIS_STATE_LABELS.analyzed
}

export function parseAnalysisState(raw: string | null): AnalysisStateFilter {
  return raw === 'submitted' || raw === 'analyzed' ? raw : 'all'
}
