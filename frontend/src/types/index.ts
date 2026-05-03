/* ------------------------------------------------------------------ */
/*  TypeScript types for API response shapes.                          */
/*                                                                     */
/*  The backend returns untyped dicts (not Pydantic response models),  */
/*  so auto-generation from the OpenAPI schema is not possible.        */
/*  These types ARE the contract definition for the frontend.          */
/*                                                                     */
/*  When modifying backend response shapes, update the corresponding   */
/*  types here. If the backend adds typed response models in the       */
/*  future, switch to auto-generated types (e.g. openapi-typescript).  */
/* ------------------------------------------------------------------ */

/** Shared status union matching the backend contract. */
export type AnalysisStatus = 'waiting' | 'pending' | 'running' | 'completed' | 'failed'

// -- Auth -----------------------------------------------------------

export interface AuthUser {
  username: string
  role: string
  is_admin: boolean
}

export interface AdminUser {
  username: string
  role: string
  created_at: string
  last_seen: string | null
}

export interface CreateUserResponse {
  username: string
  api_key: string
  role: string
}

export interface RotateKeyResponse {
  username: string
  new_api_key: string
}

export interface ChangeRoleResponse {
  username: string
  role: string
  api_key?: string
}

// -- Analysis domain ------------------------------------------------

export interface JiraMatch {
  key: string
  summary: string
  status: string
  priority: string
  url: string
  score: number
}

export interface ProductBugReport {
  title: string
  severity: string
  component: string
  description: string
  evidence: string
  jira_search_keywords: string[]
  jira_matches: JiraMatch[]
}

export interface CodeFix {
  file: string
  line: string
  change: string
  original_code?: string | null
  suggested_code?: string | null
}

export interface AnalysisDetail {
  classification: string
  affected_tests: string[]
  details: string
  artifacts_evidence: string
  code_fix?: CodeFix
  product_bug_report?: ProductBugReport
}

export interface PeerRound {
  round: number
  ai_provider: string
  ai_model: string
  role: 'orchestrator' | 'peer'
  classification: string
  details: string
  agrees_with_orchestrator: boolean | null
}

export interface PeerDebate {
  consensus_reached: boolean
  rounds_used: number
  max_rounds: number
  ai_configs: AiConfig[]
  rounds: PeerRound[]
}

export interface FailureAnalysis {
  test_name: string
  error: string
  analysis: AnalysisDetail
  error_signature: string
  peer_debate?: PeerDebate | null
}

export interface ChildJobAnalysis {
  job_name: string
  build_number: number
  jenkins_url: string | null
  summary: string | null
  failures: FailureAnalysis[]
  failed_children: ChildJobAnalysis[]
  note: string | null
}

export interface AnalysisResult {
  job_id: string
  job_name: string
  build_number: number
  jenkins_url: string | null
  status: AnalysisStatus
  summary: string
  ai_provider: string
  ai_model: string
  failures: FailureAnalysis[]
  child_job_analyses: ChildJobAnalysis[]
  token_usage?: TokenUsageSummary
  error?: string
  progress_log?: Array<{ phase: string; timestamp: number }>
  progress_phase?: string
  request_params?: {
    ai_provider: string
    ai_model: string
    peer_ai_configs?: AiConfig[]
    peer_analysis_max_rounds?: number
    tests_repo_url?: string
    tests_repo_ref?: string
    additional_repos?: Array<{ name: string; url: string; ref?: string }>
    force?: boolean
    [key: string]: unknown
  }
}

// -- Dashboard ------------------------------------------------------

export interface DashboardJob {
  job_id: string
  jenkins_url: string | null
  status: AnalysisStatus
  created_at: string
  completed_at?: string | null
  analysis_started_at?: string | null
  reviewed_count: number
  comment_count: number
  job_name?: string
  build_number?: number
  failure_count?: number
  child_job_count?: number
  summary?: string
  error?: string
}

// -- Comments & Reviews ---------------------------------------------

export interface Comment {
  id: number
  job_id: string
  test_name: string
  child_job_name: string
  child_build_number: number
  comment: string
  username: string
  created_at: string
}

export interface ReviewState {
  reviewed: boolean
  username: string
  updated_at: string
}

export interface CommentsAndReviews {
  comments: Comment[]
  reviews: Record<string, ReviewState>
}

// -- Bug creation ---------------------------------------------------

export interface SimilarIssue {
  number: number | null
  key: string
  title: string
  url: string
  status: string
}

export interface PreviewIssueResponse {
  title: string
  body: string
  similar_issues: SimilarIssue[]
}

export interface CreateIssueResponse {
  url: string
  key: string
  number?: number | null
  title: string
  comment_id: number
}

// -- History --------------------------------------------------------

export interface FailureHistoryEntry {
  id: number
  job_id: string
  job_name: string
  build_number: number
  test_name: string
  error_message: string
  error_signature: string
  classification: string
  child_job_name: string
  child_build_number: number
  analyzed_at: string
}

export interface TestHistory {
  total_runs: number
  failures: number
  passes: number | null
  failure_rate: number | null
  first_seen: string
  last_seen: string
  last_classification: string
  classifications: Record<string, number>
  recent_runs: Array<{
    job_id: string
    job_name: string
    build_number: number
    classification: string
    analyzed_at: string
    child_job_name: string
    child_build_number: number
    error_message: string
  }>
  comments: Array<{ comment: string; username: string; created_at: string }>
  consecutive_failures: number
  note?: string
}

// -- Grouping -------------------------------------------------------

export interface GroupedFailure {
  signature: string
  tests: FailureAnalysis[]
  count: number
  id: string
}

// -- AI Config ------------------------------------------------------

export interface AiConfig {
  ai_provider: string
  ai_model: string
}

// -- Result wrapper (from GET /results/{jobId}) ---------------------

export interface ResultResponse {
  job_id: string
  jenkins_url: string | null
  status: AnalysisStatus
  result: AnalysisResult | null
  created_at: string
  completed_at?: string | null
  analysis_started_at?: string | null
  base_url: string | null
  result_url: string | null
  capabilities?: {
    github_issues_enabled: boolean
    jira_issues_enabled: boolean
    server_github_token?: boolean
    server_jira_token?: boolean
    server_jira_email?: boolean
    server_jira_project_key?: string
    reportportal?: boolean
    reportportal_project?: string
    feedback_enabled?: boolean
  }
}

// -- Report Portal --------------------------------------------------

export interface ReportPortalPushResult {
  pushed: number
  unmatched: string[]
  errors: string[]
  launch_id: number | null
}

// -- Comment enrichment ---------------------------------------------

export interface CommentEnrichment {
  type: 'github_pr' | 'github_issue' | 'jira'
  key: string
  status: string
}

// -- Token Usage ----------------------------------------------------

export interface TokenUsageEntry {
  provider: string
  model: string
  call_type: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost_usd: number | null
  duration_ms: number | null
}

export interface TokenUsageSummary {
  total_input_tokens: number
  total_output_tokens: number
  total_cache_read_tokens: number
  total_cache_write_tokens: number
  total_tokens: number
  total_cost_usd: number | null
  total_duration_ms: number
  total_calls: number
  calls: TokenUsageEntry[]
}

export interface TokenUsagePeriod {
  calls: number
  tokens: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface TokenUsageDashboard {
  today: TokenUsagePeriod
  this_week: TokenUsagePeriod
  this_month: TokenUsagePeriod
  top_models: { model: string; calls: number; cost_usd: number }[]
  top_jobs: { job_id: string; calls: number; cost_usd: number }[]
}

export interface TokenUsageRecord {
  id: string
  job_id: string
  created_at: string
  ai_provider: string
  ai_model: string
  call_type: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost_usd: number | null
  duration_ms: number | null
  prompt_chars: number
  response_chars: number
}

// -- Job Metadata ---------------------------------------------------

export interface JobMetadata {
  job_name: string
  team: string | null
  tier: string | null
  version: string | null
  labels: string[]
}

export interface DashboardJobWithMetadata extends DashboardJob {
  metadata?: JobMetadata | null
}

// -- Feedback -----------------------------------------------------

export interface FeedbackRequest {
  description: string
  console_errors: string[]
  failed_api_calls: { status: number; endpoint: string; error: string }[]
  page_state: { url?: string; active_filters?: string; report_id?: string }
  user_agent: string
}

export interface FeedbackPreviewResponse {
  title: string
  body: string
  labels: string[]
}

export interface FeedbackCreateRequest {
  title: string
  body: string
  labels: string[]
}

export interface FeedbackCreateResponse {
  issue_url: string
  issue_number: number
  title: string
}
