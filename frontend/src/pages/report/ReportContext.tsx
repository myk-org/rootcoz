import { createContext, useContext, useReducer, useRef, useCallback, type Dispatch, type ReactNode } from 'react'
import { api } from '@/lib/api'
import { reviewKey } from '@/lib/reviewKey'
import type { AnalysisResult, Comment, ReviewState, CommentsAndReviews, AiConfig, CommentEnrichment } from '@/types'

interface ReportState {
  result: AnalysisResult | null
  createdAt: string
  completedAt: string
  analysisStartedAt: string
  comments: Comment[]
  reviews: Record<string, ReviewState>
  enrichments: Record<string, CommentEnrichment[]>
  classifications: Record<string, string>
  githubIssuesEnabled: boolean
  jiraIssuesEnabled: boolean
  reportportalAvailable: boolean
  reportportalProject: string
  serverJiraProjectKey: string
  aiConfigs: AiConfig[]
  loading: boolean
  error: string
  /** Number of comment editors with non-empty text (pauses comment polling when > 0). */
  commentDraftCount: number
  reAnalyzeOpen: boolean
  /** Incremented on every optimistic local mutation (ADD_COMMENT, REMOVE_COMMENT, SET_REVIEW)
   *  so that in-flight poll responses can detect stale data and skip overwriting. */
  localMutationRev: number
}

type ReportAction =
  | { type: 'SET_RESULT'; payload: { result: AnalysisResult; createdAt: string; completedAt: string; analysisStartedAt: string } }
  | { type: 'SET_COMMENTS_AND_REVIEWS'; payload: CommentsAndReviews }
  | { type: 'ADD_COMMENT'; payload: Comment }
  | { type: 'REMOVE_COMMENT'; payload: number }
  | { type: 'SET_REVIEW'; payload: { key: string; state: ReviewState } }
  | { type: 'SET_GITHUB_ISSUES_ENABLED'; payload: boolean }
  | { type: 'SET_JIRA_ISSUES_ENABLED'; payload: boolean }
  | { type: 'SET_REPORTPORTAL_AVAILABLE'; payload: boolean }
  | { type: 'SET_REPORTPORTAL_PROJECT'; payload: string }
  | { type: 'SET_SERVER_JIRA_PROJECT_KEY'; payload: string }
  | { type: 'SET_AI_CONFIGS'; payload: AiConfig[] }
  | { type: 'SET_ENRICHMENTS'; payload: Record<string, CommentEnrichment[]> }
  | { type: 'SET_CLASSIFICATIONS'; payload: Record<string, string> }
  | { type: 'SET_LOADING'; payload: boolean }
  | { type: 'SET_ERROR'; payload: string }
  | { type: 'INCREMENT_DRAFT_COUNT' }
  | { type: 'DECREMENT_DRAFT_COUNT' }
  | { type: 'SET_RE_ANALYZE_OPEN'; payload: boolean }
  | {
      type: 'OVERRIDE_CLASSIFICATION'
      payload: {
        testName: string
        testNames?: string[]
        classification: string
        childJobName?: string
        childBuildNumber?: number
      }
    }

const initialState: ReportState = {
  result: null,
  createdAt: '',
  completedAt: '',
  analysisStartedAt: '',
  comments: [],
  reviews: {},
  enrichments: {},
  classifications: {},
  githubIssuesEnabled: false,
  jiraIssuesEnabled: false,
  reportportalAvailable: false,
  reportportalProject: '',
  serverJiraProjectKey: '',
  aiConfigs: [],
  loading: true,
  error: '',
  commentDraftCount: 0,
  reAnalyzeOpen: false,
  localMutationRev: 0,
}

function reportReducer(state: ReportState, action: ReportAction): ReportState {
  switch (action.type) {
    case 'SET_RESULT':
      return { ...state, result: action.payload.result, createdAt: action.payload.createdAt, completedAt: action.payload.completedAt, analysisStartedAt: action.payload.analysisStartedAt, loading: false, error: '' }
    case 'SET_COMMENTS_AND_REVIEWS':
      return { ...state, comments: action.payload.comments, reviews: action.payload.reviews }
    case 'ADD_COMMENT':
      return { ...state, comments: [...state.comments, action.payload], localMutationRev: state.localMutationRev + 1 }
    case 'REMOVE_COMMENT':
      return { ...state, comments: state.comments.filter((c) => c.id !== action.payload), localMutationRev: state.localMutationRev + 1 }
    case 'SET_REVIEW':
      return { ...state, reviews: { ...state.reviews, [action.payload.key]: action.payload.state }, localMutationRev: state.localMutationRev + 1 }
    case 'SET_GITHUB_ISSUES_ENABLED':
      return { ...state, githubIssuesEnabled: action.payload }
    case 'SET_JIRA_ISSUES_ENABLED':
      return { ...state, jiraIssuesEnabled: action.payload }
    case 'SET_REPORTPORTAL_AVAILABLE':
      return { ...state, reportportalAvailable: action.payload }
    case 'SET_REPORTPORTAL_PROJECT':
      return { ...state, reportportalProject: action.payload }
    case 'SET_SERVER_JIRA_PROJECT_KEY':
      return { ...state, serverJiraProjectKey: action.payload }
    case 'SET_AI_CONFIGS':
      return { ...state, aiConfigs: action.payload }
    case 'SET_ENRICHMENTS':
      return { ...state, enrichments: action.payload }
    case 'SET_CLASSIFICATIONS':
      return { ...state, classifications: { ...action.payload, ...state.classifications } }
    case 'SET_LOADING':
      return { ...state, loading: action.payload }
    case 'SET_ERROR':
      return { ...state, error: action.payload, loading: false }
    case 'INCREMENT_DRAFT_COUNT':
      return { ...state, commentDraftCount: state.commentDraftCount + 1 }
    case 'DECREMENT_DRAFT_COUNT':
      return { ...state, commentDraftCount: Math.max(0, state.commentDraftCount - 1) }
    case 'SET_RE_ANALYZE_OPEN':
      return { ...state, reAnalyzeOpen: action.payload }
    case 'OVERRIDE_CLASSIFICATION': {
      if (!state.result) return state
      const { testName, testNames: explicitNames, classification, childJobName, childBuildNumber } = action.payload
      // Normalize: undefined childBuildNumber with a childJobName means wildcard (0),
      // matching the API normalization in ClassificationSelect (child_build_number ?? 0).
      const normalizedChildBuildNumber = childJobName ? (childBuildNumber ?? 0) : childBuildNumber
      const names = explicitNames ?? [testName]
      const nameSet = new Set(names)
      // Determine which classification-specific fields to clear based on the new classification
      const clearFields: Partial<Record<'code_fix' | 'product_bug_report', undefined>> =
        classification === 'CODE ISSUE' ? { product_bug_report: undefined }
        : classification === 'PRODUCT BUG' ? { code_fix: undefined }
        : classification === 'INFRASTRUCTURE' ? { code_fix: undefined, product_bug_report: undefined }
        : {}
      const patchFailures = (fs: typeof state.result.failures) =>
        (fs ?? []).map((f) =>
          nameSet.has(f.test_name) ? { ...f, analysis: { ...f.analysis, classification, ...clearFields } } : f,
        )
      const isWildcard = normalizedChildBuildNumber === 0
      /** Check whether a child node matches the target job name with wildcard/exact build semantics. */
      const isChildMatch = (c: { job_name: string; build_number: number }) =>
        !!childJobName && c.job_name === childJobName && (isWildcard || c.build_number === normalizedChildBuildNumber)
      const patchChildren = (
        cs: typeof state.result.child_job_analyses,
      ): typeof state.result.child_job_analyses =>
        (cs ?? []).map((c) =>
          isChildMatch(c)
            ? { ...c, failures: patchFailures(c.failures), failed_children: patchChildren(c.failed_children) }
            : { ...c, failed_children: patchChildren(c.failed_children) },
        )
      // When childBuildNumber === 0 (wildcard), materialize classification
      // entries for every matching child build so the review state reflects
      // each concrete build rather than only the wildcard key.
      const classificationEntries: Record<string, string> = {}
      if (isWildcard && childJobName) {
        const walkChildren = (cs: typeof state.result.child_job_analyses) => {
          for (const c of cs ?? []) {
            if (isChildMatch(c)) {
              for (const name of names) {
                classificationEntries[reviewKey(name, childJobName, c.build_number)] = classification
              }
            }
            walkChildren(c.failed_children)
          }
        }
        walkChildren(state.result.child_job_analyses)
        // Also store the wildcard key itself for lookup consistency
        for (const name of names) {
          classificationEntries[reviewKey(name, childJobName, 0)] = classification
        }
      } else {
        for (const name of names) {
          classificationEntries[reviewKey(name, childJobName, normalizedChildBuildNumber)] = classification
        }
      }
      return {
        ...state,
        result: {
          ...state.result,
          failures: childJobName ? state.result.failures : patchFailures(state.result.failures),
          child_job_analyses: patchChildren(state.result.child_job_analyses),
        },
        classifications: { ...state.classifications, ...classificationEntries },
      }
    }
    default:
      return state
  }
}

const StateCtx = createContext<ReportState>(initialState)
const DispatchCtx = createContext<Dispatch<ReportAction>>(() => {})
const RefreshEnrichmentsCtx = createContext<(jobId: string) => void>(() => {})

export function ReportProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reportReducer, initialState)
  const enrichmentSeqRef = useRef(0)
  const enrichmentInFlightRef = useRef(false)
  const pendingEnrichmentJobIdRef = useRef<string | null>(null)

  const refreshEnrichments = useCallback((jobId: string) => {
    if (enrichmentInFlightRef.current) {
      // Record the latest request so it runs when the current one finishes.
      // Advance the sequence counter to invalidate the current in-flight response
      // so stale data does not overwrite state.
      pendingEnrichmentJobIdRef.current = jobId
      enrichmentSeqRef.current += 1
      return
    }
    enrichmentInFlightRef.current = true
    pendingEnrichmentJobIdRef.current = null
    const seq = ++enrichmentSeqRef.current
    void api.post<{ enrichments: Record<string, CommentEnrichment[]> }>(`/results/${jobId}/enrich-comments`)
      .then((res) => {
        if (seq === enrichmentSeqRef.current) {
          dispatch({ type: 'SET_ENRICHMENTS', payload: res.enrichments ?? {} })
        }
      })
      .catch(() => {})
      .finally(() => {
        enrichmentInFlightRef.current = false
        const pending = pendingEnrichmentJobIdRef.current
        if (pending) {
          pendingEnrichmentJobIdRef.current = null
          refreshEnrichments(pending)
        }
      })
  }, [])

  return (
    <StateCtx.Provider value={state}>
      <DispatchCtx.Provider value={dispatch}>
        <RefreshEnrichmentsCtx.Provider value={refreshEnrichments}>{children}</RefreshEnrichmentsCtx.Provider>
      </DispatchCtx.Provider>
    </StateCtx.Provider>
  )
}

export const useReportState = () => useContext(StateCtx)
export const useReportDispatch = () => useContext(DispatchCtx)
export const useRefreshEnrichments = () => useContext(RefreshEnrichmentsCtx)

// Re-export for backward compatibility
export { reviewKey } from '@/lib/reviewKey'
