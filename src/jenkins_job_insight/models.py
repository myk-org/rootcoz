"""Pydantic request and response models."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    field_validator,
    model_serializer,
    model_validator,
)

from jenkins_job_insight.repository import RESERVED_REPO_NAMES


class AiConfigEntry(BaseModel):
    """Single AI provider/model configuration for peer analysis."""

    ai_provider: Literal["claude", "gemini", "cursor"] = Field(
        description="AI provider"
    )
    ai_model: str = Field(min_length=1, description="AI model identifier")

    @field_validator("ai_model")
    @classmethod
    def ai_model_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ai_model must not be blank")
        return v


class AdditionalRepo(BaseModel):
    """A named additional repository for AI analysis context."""

    name: str = Field(
        min_length=1, description="Descriptive name (used as cloned directory name)"
    )
    url: HttpUrl = Field(description="Repository URL to clone")
    ref: str = Field(
        default="",
        description="Git ref (branch/tag) for clone checkout and UI file links; empty = remote default branch",
    )
    token: str | None = Field(
        default=None,
        description="Authentication token for cloning private repos",
        json_schema_extra={"format": "password"},
    )

    @field_validator("ref")
    @classmethod
    def ref_strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        if "/" in v or "\\" in v:
            raise ValueError("name must not contain path separators ('/' or '\\')")
        if ".." in v:
            raise ValueError("name must not contain '..'")
        if v.startswith("."):
            raise ValueError("name must not start with '.'")
        if v in RESERVED_REPO_NAMES:
            raise ValueError(f"name '{v}' is reserved and cannot be used")
        return v


class BaseAnalysisRequest(BaseModel):
    """Shared fields for all analysis request types."""

    tests_repo_url: str | None = Field(
        default=None,
        description="URL of the tests repository (overrides env var default)",
    )
    tests_repo_token: str | None = Field(
        default=None,
        description="Authentication token for cloning private tests repo (overrides TESTS_REPO_TOKEN env var)",
        json_schema_extra={"format": "password"},
    )
    ai_provider: Literal["claude", "gemini", "cursor"] | None = Field(
        default=None,
        description="AI provider to use: claude, gemini, or cursor (overrides env var default)",
    )
    ai_model: str | None = Field(
        default=None,
        description="AI model to use (overrides env var default)",
    )
    enable_jira: bool | None = Field(
        default=None,
        description="Enable Jira bug search (default: true when Jira is configured, set false to skip)",
    )
    ai_cli_timeout: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="AI CLI timeout in minutes (overrides AI_CLI_TIMEOUT env var)",
    )
    max_concurrent_ai_calls: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Max concurrent AI CLI calls (overrides MAX_CONCURRENT_AI_CALLS env var)",
    )
    jira_url: str | None = Field(
        default=None,
        description="Jira instance URL (overrides JIRA_URL env var)",
    )
    jira_email: str | None = Field(
        default=None,
        description="Jira Cloud email (overrides JIRA_EMAIL env var)",
    )
    jira_api_token: str | None = Field(
        default=None,
        description="Jira Cloud API token (overrides JIRA_API_TOKEN env var)",
        json_schema_extra={"format": "password"},
    )
    jira_pat: str | None = Field(
        default=None,
        description="Jira Server/DC personal access token (overrides JIRA_PAT env var)",
        json_schema_extra={"format": "password"},
    )
    jira_project_key: str | None = Field(
        default=None,
        description="Jira project key to scope searches (overrides JIRA_PROJECT_KEY env var)",
    )
    jira_ssl_verify: bool | None = Field(
        default=None,
        description="Jira SSL verification (overrides JIRA_SSL_VERIFY env var)",
    )
    jira_max_results: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Max Jira search results (overrides JIRA_MAX_RESULTS env var)",
    )
    raw_prompt: str | None = Field(
        default=None,
        description="Raw prompt to append as additional AI instructions (overrides repo-level JOB_INSIGHT_PROMPT.md)",
    )
    github_token: str | None = Field(
        default=None,
        description="GitHub API token for private repo PR status in comments (overrides GITHUB_TOKEN env var)",
        json_schema_extra={"format": "password"},
    )
    peer_ai_configs: list[AiConfigEntry] | None = Field(
        default=None,
        description=(
            "List of peer AI configs for consensus analysis. "
            "Omit to inherit the server default; send [] to disable peer analysis "
            "for this request. Each peer reviews the main AI's analysis."
        ),
    )
    peer_analysis_max_rounds: Annotated[int, Field(ge=1, le=10)] = Field(
        default=3,
        description="Maximum debate rounds for peer analysis",
    )
    additional_repos: list[AdditionalRepo] | None = Field(
        default=None,
        description=(
            "Additional repository URLs for AI analysis context. "
            "Each entry has a name (used as subdirectory name) and URL. "
            "Omit to inherit the server default; send [] to disable."
        ),
    )

    @field_validator("tests_repo_token")
    @classmethod
    def _normalize_tests_repo_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("additional_repos")
    @classmethod
    def _unique_additional_repo_names(
        cls,
        v: list[AdditionalRepo] | None,
    ) -> list[AdditionalRepo] | None:
        if v is None:
            return v
        names = [ar.name for ar in v]
        dupes = [n for n in names if names.count(n) > 1]
        if dupes:
            raise ValueError(
                f"Duplicate additional repo names: {', '.join(sorted(set(dupes)))}"
            )
        return v


class AnalyzeRequest(BaseAnalysisRequest):
    """Request payload for analysis endpoint."""

    job_name: str = Field(
        description="Jenkins job name (can include folders like 'folder/job-name')"
    )
    build_number: int = Field(description="Build number to analyze")
    force: bool = Field(
        default=False,
        description="Force analysis even if the build succeeded (bypass SUCCESS early-return)",
    )
    wait_for_completion: bool = Field(
        default=True,
        description="Wait for Jenkins job to complete before analyzing",
    )
    poll_interval_minutes: Annotated[int, Field(gt=0)] = Field(
        default=2,
        description="Minutes between Jenkins status polls when waiting",
    )
    max_wait_minutes: Annotated[int, Field(ge=0)] = Field(
        default=0,
        description="Maximum minutes to wait for job completion (0 = no limit)",
    )
    jenkins_url: str | None = Field(
        default=None,
        description="Jenkins server URL (overrides JENKINS_URL env var)",
    )
    jenkins_user: str | None = Field(
        default=None,
        description="Jenkins username (overrides JENKINS_USER env var)",
    )
    jenkins_password: str | None = Field(
        default=None,
        description="Jenkins password or API token (overrides JENKINS_PASSWORD env var)",
        json_schema_extra={"format": "password"},
    )
    jenkins_ssl_verify: bool | None = Field(
        default=None,
        description="Jenkins SSL verification (overrides JENKINS_SSL_VERIFY env var)",
    )
    jenkins_timeout: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Jenkins API request timeout in seconds (overrides JENKINS_TIMEOUT env var).",
    )
    jenkins_artifacts_max_size_mb: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Maximum Jenkins artifacts size in MB (overrides JENKINS_ARTIFACTS_MAX_SIZE_MB env var)",
    )
    get_job_artifacts: bool | None = Field(
        default=None,
        description="Download all build artifacts for AI context (default: true, overrides GET_JOB_ARTIFACTS env var)",
    )


class TestFailure(BaseModel):
    """A single test failure extracted from Jenkins test report."""

    test_name: str = Field(
        description="Fully qualified test name (className.methodName)"
    )
    error_message: str = Field(default="", description="Error details/message")
    stack_trace: str = Field(default="", description="Full stack trace if available")
    duration: float = Field(default=0.0, description="Test duration in seconds")
    status: str = Field(
        default="FAILED", description="Test status (FAILED, REGRESSION, etc.)"
    )


class JiraMatch(BaseModel):
    """A Jira issue that potentially matches a product bug."""

    key: str = Field(description="Jira issue key (e.g., PROJ-123)")
    summary: str = Field(description="Issue summary/title")
    status: str = Field(
        default="", description="Issue status (e.g., Open, In Progress)"
    )
    priority: str = Field(default="", description="Issue priority (e.g., High, Medium)")
    url: str = Field(default="", description="Full URL to the Jira issue")
    score: float = Field(default=0.0, description="Relevance score (0.0-1.0)")


class ProductBugReport(BaseModel):
    """Structured product bug report from AI analysis."""

    title: str = Field(default="", description="Concise bug title")
    severity: str = Field(
        default="", description="Bug severity: critical/high/medium/low"
    )
    component: str = Field(default="", description="Affected component")
    description: str = Field(default="", description="What product behavior is broken")
    evidence: str = Field(default="", description="Relevant log snippets")
    jira_search_keywords: list[str] = Field(
        default_factory=list, description="AI-suggested keywords for Jira search"
    )
    jira_matches: list[JiraMatch] = Field(
        default_factory=list,
        description="Matched Jira issues (populated in post-processing)",
    )


class CodeFix(BaseModel):
    """Structured code fix suggestion from AI analysis."""

    file: str = Field(default="", description="File path to fix")
    line: str = Field(default="", description="Line number")
    change: str = Field(default="", description="Specific code change")
    original_code: str | None = Field(
        default=None,
        description="Optional complete original file content for diff/editor display (raw string, no markdown)",
    )
    suggested_code: str | None = Field(
        default=None,
        description="Complete replacement file content after applying the suggested fix (raw string, no markdown)",
    )
    tests_repo_search_keywords: list[str] = Field(
        default_factory=list,
        description="AI-suggested keywords for searching related issues in the tests repository",
    )
    tests_repo_matches: list["SimilarIssue"] = Field(
        default_factory=list,
        description="Matched issues from the tests repository (populated in post-processing)",
    )

    @field_validator("tests_repo_search_keywords")
    @classmethod
    def _normalize_tests_repo_search_keywords(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in v:
            stripped = item.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                result.append(stripped)
        return result


class AnalysisDetail(BaseModel):
    """Structured AI analysis broken into sections."""

    classification: str = Field(
        default="", description="CODE ISSUE, PRODUCT BUG, or INFRASTRUCTURE (override)"
    )
    affected_tests: list[str] = Field(
        default_factory=list, description="List of affected test names"
    )
    details: str = Field(default="", description="Detailed analysis text")
    artifacts_evidence: str = Field(
        default="",
        description="Verbatim log lines from build artifacts supporting the analysis (not a summary)",
    )
    code_fix: CodeFix | bool | None = Field(
        default=False, description="Code fix (if CODE ISSUE)"
    )
    product_bug_report: ProductBugReport | bool | None = Field(
        default=False, description="Bug report (if PRODUCT BUG)"
    )

    @model_validator(mode="after")
    def check_mutual_exclusivity(self) -> "AnalysisDetail":
        if self.code_fix and self.product_bug_report:
            raise ValueError("code_fix and product_bug_report are mutually exclusive")
        return self

    @model_serializer(mode="wrap")
    def _exclude_falsy_optionals(self, handler):
        d = handler(self)
        if not d.get("code_fix"):
            d.pop("code_fix", None)
        if not d.get("product_bug_report"):
            d.pop("product_bug_report", None)
        return d


class PeerRound(BaseModel):
    """One participant's contribution in a single debate round."""

    round: int  # Debate round number (1-indexed)
    ai_provider: str
    ai_model: str
    role: Literal["orchestrator", "peer"]
    classification: str
    details: str
    agrees_with_orchestrator: bool | None = (
        None  # None = failed/excluded from consensus
    )


class PeerDebate(BaseModel):
    """Full debate trail for a peer-analyzed failure group."""

    consensus_reached: bool
    rounds_used: int
    max_rounds: int
    ai_configs: list[AiConfigEntry]
    rounds: list[PeerRound]


class FailureAnalysis(BaseModel):
    """Analysis result for a single test failure."""

    test_name: str = Field(description="Name of the failed test")
    error: str = Field(description="Error message or exception")
    analysis: AnalysisDetail = Field(description="Structured AI analysis output")
    error_signature: str = Field(
        default="",
        description="SHA-256 hash of error + stack trace for deduplication",
    )
    peer_debate: PeerDebate | None = Field(
        default=None,
        description="Peer debate trail (present only when peer analysis was used)",
    )

    @field_validator("analysis", mode="before")
    @classmethod
    def _coerce_legacy_analysis(cls, v: object) -> object:
        """Accept legacy string format for backward compatibility.

        Data stored before the AnalysisDetail model was introduced has the
        analysis field as a plain string.  Wrap it in a dict so Pydantic can
        construct an AnalysisDetail with the text in the ``details`` field.
        """
        if isinstance(v, str):
            return {"details": v}
        return v


class ChildJobAnalysis(BaseModel):
    """Analysis result for a failed child job in a pipeline."""

    job_name: str = Field(description="Name of the child job")
    build_number: int = Field(description="Build number of the child job")
    jenkins_url: str | None = Field(
        default=None, description="URL of the child job build"
    )
    summary: str | None = Field(
        default=None, description="Summary of the child job failure analysis"
    )
    failures: list["FailureAnalysis"] = Field(
        default_factory=list, description="List of analyzed failures in child job"
    )
    failed_children: list["ChildJobAnalysis"] = Field(
        default_factory=list, description="Nested failed child jobs"
    )
    note: str | None = Field(
        default=None, description="Additional notes (e.g., max depth reached)"
    )


class TokenUsageEntry(BaseModel):
    """Token usage for a single AI CLI call."""

    provider: str = ""
    model: str = ""
    call_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None


class TokenUsageSummary(BaseModel):
    """Aggregated token usage for an entire analysis job."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float | None = None
    total_duration_ms: int = 0
    total_calls: int = 0
    calls: list[TokenUsageEntry] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Complete analysis result for a Jenkins job."""

    job_id: str = Field(description="Unique identifier for the analysis job")
    job_name: str = Field(default="", description="Jenkins job name")
    build_number: int = Field(default=0, description="Jenkins build number")
    jenkins_url: HttpUrl | None = Field(
        default=None,
        description="URL of the analyzed Jenkins job (None for non-Jenkins analysis)",
    )
    status: Literal["pending", "waiting", "running", "completed", "failed"] = Field(
        description="Current status of the analysis"
    )
    summary: str = Field(description="Summary of the analysis findings")
    ai_provider: str = Field(default="", description="AI provider used for analysis")
    ai_model: str = Field(default="", description="AI model used for analysis")
    failures: list[FailureAnalysis] = Field(
        default_factory=list, description="List of analyzed failures"
    )
    child_job_analyses: list[ChildJobAnalysis] = Field(
        default_factory=list,
        description="Analyses of failed child jobs in pipeline",
    )
    token_usage: TokenUsageSummary | None = Field(
        default=None,
        description="Aggregated token usage across all AI calls in this analysis",
    )


class JobStatus(BaseModel):
    """Status information for a queued analysis job."""

    job_id: str = Field(description="Unique identifier for the analysis job")
    status: Literal["pending", "waiting", "running", "completed", "failed"] = Field(
        description="Current status of the analysis"
    )
    created_at: datetime = Field(description="Timestamp when the job was created")


class AnalyzeFailuresRequest(BaseAnalysisRequest):
    """Request payload for direct failure analysis (no Jenkins)."""

    failures: list[TestFailure] | None = Field(
        default=None, description="Raw test failures to analyze"
    )
    raw_xml: Annotated[str, Field(max_length=50_000_000)] | None = Field(
        default=None,
        description="Raw JUnit XML content to extract failures from and enrich with analysis results",
    )

    @model_validator(mode="after")
    def check_input_source(self) -> "AnalyzeFailuresRequest":
        if self.failures and self.raw_xml:
            raise ValueError("Provide either 'failures' or 'raw_xml', not both")
        if not self.failures and not self.raw_xml:
            raise ValueError("Either 'failures' or 'raw_xml' must be provided")
        return self


class FailureAnalysisResult(BaseModel):
    """Analysis result for direct failure analysis (no Jenkins context)."""

    job_id: str = Field(description="Unique identifier for the analysis job")
    status: Literal["completed", "failed"] = Field(description="Analysis status")
    summary: str = Field(description="Summary of the analysis findings")
    ai_provider: str = Field(default="", description="AI provider used")
    ai_model: str = Field(default="", description="AI model used")
    failures: list[FailureAnalysis] = Field(
        default_factory=list, description="Analyzed failures"
    )
    enriched_xml: str | None = Field(
        default=None,
        description="Enriched JUnit XML with analysis results (only when raw_xml was provided in request)",
    )
    token_usage: TokenUsageSummary | None = Field(
        default=None, description="Token usage summary for this analysis"
    )


class _ChildJobFieldsValidator(BaseModel):
    """Mixin providing child_job_name + child_build_number cross-validation.

    child_build_number uses 0 as a wildcard meaning "not specified".
    Negative values are rejected.
    """

    child_job_name: str = ""
    child_build_number: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_child_fields(self):
        if not self.child_job_name and self.child_build_number > 0:
            raise ValueError(
                "child_job_name is required when child_build_number is set"
            )
        return self


class AddCommentRequest(_ChildJobFieldsValidator):
    """Request body for adding a comment to a test failure."""

    test_name: str
    comment: str
    # NOTE: error_signature is NOT sent by the browser.
    # It is read server-side from the pre-computed FailureAnalysis.error_signature
    # stored in the result data (computed during analysis when stack traces are available).


class SetReviewedRequest(_ChildJobFieldsValidator):
    """Request body for toggling the reviewed state of a test failure."""

    test_name: str
    reviewed: bool


class CommentResponse(BaseModel):
    """A single comment entry."""

    id: int
    job_id: str
    test_name: str
    child_job_name: str = ""
    child_build_number: int = 0
    comment: str
    username: str = ""
    created_at: str


class ReviewState(BaseModel):
    """Reviewed state for a single failure."""

    reviewed: bool
    updated_at: str


class CommentsAndReviewsResponse(BaseModel):
    """Combined response for all comments and review states for a job."""

    comments: list[CommentResponse]
    reviews: dict[str, ReviewState]


class ReviewStatusResponse(BaseModel):
    """Lightweight review summary for dashboard cards."""

    total_failures: int
    reviewed_count: int
    comment_count: int


class _TrackerCredentialsMixin(BaseModel):
    """Shared tracker credential fields for issue preview/create requests."""

    github_token: str = Field(
        default="", description="User's GitHub PAT for issue creation"
    )
    jira_token: str = Field(
        default="", description="User's Jira token for bug creation"
    )
    jira_email: str = Field(default="", description="User's Jira email for Cloud auth")
    jira_project_key: str = Field(
        default="", description="Override Jira project key for bug creation"
    )
    jira_security_level: str = Field(
        default="", description="Jira security level name for restricted issues"
    )
    github_repo_url: str = Field(
        default="", description="Override GitHub repo URL for issue creation"
    )

    @field_validator("jira_project_key", "jira_security_level", "github_repo_url")
    @classmethod
    def _strip_tracker_overrides(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


# NOTE: Preview/create request models intentionally do NOT inherit
# BaseAnalysisRequest. Target repositories (TESTS_REPO_URL, JIRA_URL) are
# configured at the server level, but users may provide their own tracker
# tokens (github_token, jira_token, jira_email) to create issues under
# their own identity. When user tokens are absent, the server falls back
# to its deployment credentials. Analysis overrides remain out of scope.
class PreviewIssueRequest(_ChildJobFieldsValidator, _TrackerCredentialsMixin):
    """Request body for previewing a GitHub issue or Jira bug."""

    test_name: str
    include_links: bool = False
    ai_provider: str = Field(
        default="", description="AI provider for content generation"
    )
    ai_model: str = Field(default="", description="AI model for content generation")
    issue_prompt: str = Field(
        default="",
        description="Additional AI instructions for issue generation",
    )


class CreateIssueRequest(_ChildJobFieldsValidator, _TrackerCredentialsMixin):
    """Request body for creating a GitHub issue or Jira bug."""

    test_name: str
    title: str
    body: str
    jira_issue_type: str = Field(
        default="Bug", description="Jira issue type name (e.g. Bug, Story, Task)"
    )

    @field_validator("jira_issue_type")
    @classmethod
    def jira_issue_type_not_empty(cls, v: str) -> str:
        stripped = v.strip()
        return stripped if stripped else "Bug"

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must not be empty")
        return v


OverrideClassificationLiteral = Literal["CODE ISSUE", "PRODUCT BUG", "INFRASTRUCTURE"]
HistoryClassificationLiteral = Literal[
    "FLAKY", "REGRESSION", "INFRASTRUCTURE", "KNOWN_BUG", "INTERMITTENT"
]


class OverrideClassificationRequest(_ChildJobFieldsValidator):
    """Request body for overriding a failure's classification."""

    test_name: str
    classification: OverrideClassificationLiteral


class SimilarIssue(BaseModel):
    """A similar issue found during duplicate detection."""

    number: int | None = Field(default=None, description="Issue number (GitHub)")
    key: str = Field(default="", description="Issue key (Jira)")
    title: str = Field(default="", description="Issue title/summary")
    url: str = Field(default="", description="URL to the issue")
    status: str = Field(default="", description="Issue status")


class PreviewIssueResponse(BaseModel):
    """Response from preview-github-issue or preview-jira-bug."""

    title: str = Field(description="Generated issue title")
    body: str = Field(description="Generated issue body (markdown)")
    similar_issues: list[SimilarIssue] = Field(
        default_factory=list,
        description="Similar existing issues found",
    )


class CreateIssueResponse(BaseModel):
    """Response from create-github-issue or create-jira-bug."""

    url: str = Field(description="URL to the created issue")
    key: str = Field(default="", description="Issue key (e.g., PROJ-123 for Jira)")
    number: int = Field(default=0, description="Issue number (GitHub)")
    title: str = Field(description="Issue title as created")
    comment_id: int = Field(
        default=0,
        description="ID of the auto-created comment linking to the issue",
    )


class ClassifyTestRequest(BaseModel):
    """Request body for classifying a test (e.g., FLAKY, REGRESSION)."""

    test_name: str
    classification: HistoryClassificationLiteral
    reason: str = ""
    job_name: str = ""
    references: str = ""
    job_id: str
    child_build_number: Annotated[int, Field(ge=0)] = 0

    @field_validator("job_id")
    @classmethod
    def job_id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("job_id must not be empty")
        return v

    @field_validator("classification", mode="before")
    @classmethod
    def normalize_classification(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v


class ReportPortalPushResult(BaseModel):
    """Result from pushing classifications to Report Portal."""

    pushed: int = Field(description="Number of items successfully updated")
    unmatched: list[str] = Field(
        default_factory=list,
        description="RP item names that could not be matched to JJI failures or mapped to a defect type",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Error messages from failed RP API calls",
    )
    launch_id: int | None = Field(default=None, description="Report Portal launch ID")


class _PushEndpointMixin(BaseModel):
    """Shared validation for Web Push endpoint URLs (HTTPS-only, length-bounded)."""

    endpoint: str = Field(max_length=2048, description="Push service endpoint URL")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Push endpoint must use HTTPS")
        return v


class PushSubscriptionRequest(_PushEndpointMixin):
    """Request body for subscribing to Web Push notifications."""

    p256dh_key: str = Field(
        max_length=256, description="Client public key for message encryption"
    )
    auth_key: str = Field(max_length=256, description="Client authentication secret")


class UnsubscribeRequest(_PushEndpointMixin):
    """Request body for unsubscribing from Web Push notifications."""


class BulkDeleteRequest(BaseModel):
    """Request body for bulk-deleting jobs."""

    job_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Job IDs to delete (1-500 per request).",
    )


class _JobMetadataFields(BaseModel):
    """Shared metadata fields for job metadata request/response models."""

    team: str | None = Field(default=None, description="Team owning this job")
    tier: str | None = Field(
        default=None, description="Service tier (e.g. critical, standard, low)"
    )
    version: str | None = Field(default=None, description="Version or release label")
    labels: list[str] = Field(
        default_factory=list, description="Arbitrary labels for categorization"
    )


class JobMetadata(_JobMetadataFields):
    """Metadata for a Jenkins job used for filtering and organization."""

    job_name: str = Field(description="Jenkins job name (primary key)")


class JobMetadataInput(_JobMetadataFields):
    """Input model for setting job metadata (no job_name — taken from URL path)."""


class BulkJobMetadataEntry(JobMetadata):
    """A single entry in a bulk metadata import."""


class BulkJobMetadataRequest(BaseModel):
    """Request body for bulk-importing job metadata."""

    items: list[BulkJobMetadataEntry] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Metadata entries to import (1-1000 per request).",
    )


class AnalyzeCommentRequest(BaseModel):
    """Request body for AI-driven comment intent analysis."""

    comment: str
    job_id: str = ""  # Used to resolve AI config from the analyzed job
    ai_provider: str | None = None
    ai_model: str | None = None


class AnalyzeCommentResponse(BaseModel):
    """Response from comment intent analysis."""

    suggests_reviewed: bool
    reason: str = ""


class FailedApiCall(BaseModel):
    """A single failed API call captured by the frontend."""

    status: int = Field(default=0, description="HTTP status code")
    endpoint: str = Field(default="", max_length=500, description="API endpoint path")
    error: str = Field(
        default="", max_length=2000, description="Error message or response body"
    )


class PageState(BaseModel):
    """Current page state when feedback was submitted."""

    url: str = Field(default="", max_length=500, description="Current page URL")
    active_filters: str = Field(
        default="", max_length=1000, description="Active filter selections"
    )
    report_id: str = Field(default="", max_length=200, description="Current report ID")


class FeedbackRequest(BaseModel):
    """User feedback submission (bug or feature request)."""

    feedback_type: str = Field(
        default="feedback",
        description="Type of feedback (auto-determined by AI if not specified)",
    )
    description: str = Field(
        min_length=1, max_length=10000, description="Natural language description"
    )
    console_errors: list[Annotated[str, Field(max_length=5000)]] = Field(
        default_factory=list, max_length=50, description="Browser console errors"
    )
    failed_api_calls: list[FailedApiCall] = Field(
        default_factory=list,
        description="Recent failed API responses",
    )
    page_state: PageState = Field(
        default_factory=PageState,
        description="Current page state when feedback was submitted",
    )
    user_agent: str = Field(
        default="", max_length=500, description="Browser user agent string"
    )


class FeedbackPreviewResponse(BaseModel):
    """Response from feedback preview (AI-generated title + body)."""

    title: str = Field(description="Generated issue title")
    body: str = Field(description="Generated issue body (markdown)")
    labels: list[str] = Field(default_factory=list, description="Issue labels")


class FeedbackCreateRequest(BaseModel):
    """Request to create a GitHub issue from a previewed feedback."""

    title: str = Field(min_length=1, max_length=500, description="Issue title")
    body: str = Field(
        min_length=1, max_length=50000, description="Issue body (markdown)"
    )
    labels: list[str] = Field(default_factory=list, description="Issue labels")


class FeedbackResponse(BaseModel):
    """Response from feedback submission."""

    issue_url: str = Field(description="URL to the created GitHub issue")
    issue_number: int = Field(description="GitHub issue number")
    title: str = Field(description="Issue title as created")


# Resolve forward references (CodeFix references SimilarIssue which is defined later)
CodeFix.model_rebuild()
