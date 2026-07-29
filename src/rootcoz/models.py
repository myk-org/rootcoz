"""Pydantic request and response models."""

from datetime import datetime
from typing import Annotated, Literal, TypeVar
from uuid import uuid4

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    HttpUrl,
    Strict,
    field_validator,
    model_serializer,
    model_validator,
)

from rootcoz.repository import RESERVED_REPO_NAMES
from rootcoz.prow_validation import (
    normalize_gcs_bucket,
    normalize_gcs_prefix,
    normalize_prow_url,
    validate_gcs_prefix_suffix,
    validate_prow_build_id,
    validate_prow_job_name,
)

_SYSTEM_TAGS: set[str] = {"re-analyze"}

_TUrl = TypeVar("_TUrl", bound=HttpUrl | str | None)


def _apply_build_url_aliases(
    build_url: _TUrl,
    jenkins_url: _TUrl,
) -> tuple[_TUrl, _TUrl]:
    """Keep build_url and deprecated jenkins_url alias in sync."""
    url = build_url or jenkins_url
    if url:
        return url, url
    return build_url, jenkins_url


def _uuid_str() -> str:
    """Return a new UUID4 as a string.  Shared default-factory."""
    return str(uuid4())


def _normalize_tags_list(tags: object) -> list[str]:
    """Strip, lowercase, deduplicate, remove blanks and reserved system tags."""
    if not isinstance(tags, (list, tuple, set)):
        raise ValueError("tags must be a list")

    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        t = tag.strip().lower()
        if t and t not in seen and t not in _SYSTEM_TAGS:
            seen.add(t)
            result.append(t)
    return result


AiProviderName = Literal[
    "claude",
    "gemini",
    "cursor",
]


def _coerce_ai_provider(v: object) -> object:
    if not isinstance(v, str):
        return v
    from rootcoz.ai_client import normalize_provider

    return normalize_provider(v)


NormalizedAiProvider = Annotated[AiProviderName, BeforeValidator(_coerce_ai_provider)]


class AiConfigEntry(BaseModel):
    """Single AI provider/model configuration for peer analysis."""

    ai_provider: NormalizedAiProvider = Field(description="AI provider")
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
    ai_provider: NormalizedAiProvider | None = Field(
        default=None,
        description=(
            "AI provider to use: claude, gemini, or cursor "
            "(overrides env var default). CLI models use the same provider "
            "names when CLI_AGENTS is set."
        ),
    )
    ai_model: str | None = Field(
        default=None,
        description="AI model to use (overrides env var default)",
    )
    enable_jira: bool | None = Field(
        default=None,
        description="Enable Jira bug search (default: true when Jira is configured, set false to skip)",
    )
    ai_call_timeout: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="AI timeout in minutes (overrides AI_CALL_TIMEOUT env var)",
    )
    max_concurrent_ai_calls: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Max concurrent AI calls (overrides MAX_CONCURRENT_AI_CALLS env var)",
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
        description="Raw prompt to append as additional AI instructions (overrides repo-level .rootcoz/ROOTCOZ_PROMPT.md)",
    )
    issue_prompt: str | None = Field(
        default=None,
        description="Custom issue generation prompt. Overrides .rootcoz/ROOTCOZ_ISSUE_PROMPT.md from the test repo.",
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


class _JenkinsParamsMixin(BaseModel):
    """Shared Jenkins connection and polling fields."""

    @field_validator("job_name", mode="before", check_fields=False)
    @classmethod
    def _strip_job_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("job_name cannot be blank")
        return v

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
        description="Jenkins API request timeout in seconds (overrides JENKINS_TIMEOUT env var)",
    )
    jenkins_artifacts_max_size_mb: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description="Maximum Jenkins artifacts size in MB (overrides JENKINS_ARTIFACTS_MAX_SIZE_MB env var)",
    )
    get_job_artifacts: bool | None = Field(
        default=None,
        description="Download all build artifacts for AI context (default: true, overrides GET_JOB_ARTIFACTS env var)",
    )


class _NameTagsMixin(BaseModel):
    """Shared name and tags fields with tag normalization."""

    name: str | None = Field(
        default=None,
        max_length=500,
        description="Custom display name for this analysis job (overrides auto-generated name)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="User tags for categorization (e.g. 'regression', 'flaky')",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, v: list) -> list[str]:
        return _normalize_tags_list(v)


class AnalyzeRequest(_JenkinsParamsMixin, _NameTagsMixin, BaseAnalysisRequest):
    """Request payload for analysis endpoint."""

    job_name: str = Field(
        description="Jenkins job name (can include folders like 'folder/job-name')"
    )
    build_number: int = Field(description="Build number to analyze")


class FailedTest(BaseModel):
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
        default="", description="Root cause: CODE ISSUE, PRODUCT BUG, or INFRASTRUCTURE"
    )
    pattern: str = Field(
        default="",
        description="Failure pattern: NEW, REGRESSION, FLAKY, INTERMITTENT, KNOWN_BUG, or PERSISTENT",
    )
    affected_tests: list[str] = Field(
        default_factory=list, description="List of affected test names"
    )
    details: str = Field(default="", description="Detailed analysis text")
    artifacts_evidence: str = Field(
        default="",
        description=(
            "Evidence from build artifacts supporting the analysis: verbatim text "
            "lines and/or image observations from read (not a vague summary)"
        ),
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
    pattern: str = ""
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

    id: str = Field(
        default_factory=_uuid_str,
        description="Stable UUID for referencing this failure",
    )
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

    id: str = Field(
        default_factory=_uuid_str,
        description="Stable UUID for referencing this child job analysis",
    )
    job_name: str = Field(description="Name of the child job")
    build_number: int = Field(description="Build number of the child job")
    build_url: str | None = Field(
        default=None, description="URL of the child job build"
    )
    jenkins_url: str | None = Field(
        default=None,
        description="Deprecated alias for build_url",
        json_schema_extra={"deprecated": True},
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

    @model_validator(mode="after")
    def _sync_build_url_aliases(self) -> "ChildJobAnalysis":
        self.build_url, self.jenkins_url = _apply_build_url_aliases(
            self.build_url, self.jenkins_url
        )
        return self


class TokenUsageEntry(BaseModel):
    """Token usage for a single AI call."""

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
    """Complete analysis result for a CI job."""

    job_id: str = Field(description="Unique identifier for the analysis job")
    job_name: str = Field(default="", description="CI job name")
    build_number: int = Field(default=0, description="CI build number (Jenkins)")
    build_id: str = Field(
        default="",
        description="Prow build ID as numeric string (exceeds JS MAX_SAFE_INTEGER)",
    )
    build_url: HttpUrl | None = Field(
        default=None,
        description="URL of the analyzed CI build",
    )
    jenkins_url: HttpUrl | None = Field(
        default=None,
        description="Deprecated alias for build_url",
        json_schema_extra={"deprecated": True},
    )
    status: Literal[
        "pending", "waiting", "running", "completed", "failed", "aborted"
    ] = Field(description="Current status of the analysis")
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

    @model_validator(mode="after")
    def _sync_build_url_aliases(self) -> "AnalysisResult":
        self.build_url, self.jenkins_url = _apply_build_url_aliases(
            self.build_url, self.jenkins_url
        )
        return self


class JobStatus(BaseModel):
    """Status information for a queued analysis job."""

    job_id: str = Field(description="Unique identifier for the analysis job")
    status: Literal[
        "pending", "waiting", "running", "completed", "failed", "aborted"
    ] = Field(description="Current status of the analysis")
    created_at: datetime = Field(description="Timestamp when the job was created")


class UnifiedAnalyzeRequest(_JenkinsParamsMixin, _NameTagsMixin, BaseAnalysisRequest):
    """Unified request payload for all analysis types."""

    type: Literal["jenkins", "file", "raw", "prow"] = Field(
        description="Analysis type: jenkins (CI job), file (JUnit XML), raw (failure list), or prow (Prow CI job)"
    )

    # Jenkins-specific fields (required when type="jenkins", optional otherwise)
    job_name: str | None = Field(
        default=None,
        description="Jenkins job name (required for type=jenkins)",
    )
    build_number: int | None = Field(
        default=None,
        description="Build number to analyze (required for type=jenkins)",
    )

    # File-specific fields (required when type="file")
    raw_xml: Annotated[str, Field(max_length=50_000_000)] | None = Field(
        default=None,
        description="Raw JUnit XML content (required for type=file)",
    )

    # Raw-specific fields (required when type="raw")
    failures: list[FailedTest] | None = Field(
        default=None,
        description="Raw test failures to analyze (required for type=raw)",
    )

    # Prow-specific fields (required when type="prow")
    prow_job_name: str | None = Field(
        default=None,
        description="Prow job name (required for type=prow)",
    )
    build_id: str | None = Field(
        default=None,
        description="Prow build ID, numeric string (required for type=prow)",
    )
    prow_url: str = Field(
        default="",
        description=(
            "Prow Deck URL (overrides PROW_URL env var / Server Settings default)"
        ),
    )
    gcs_bucket: str = Field(
        default="",
        description=(
            "GCS bucket for Prow artifacts (overrides GCS_BUCKET env var / "
            "Server Settings default)"
        ),
    )
    gcs_prefix: str = Field(
        default="",
        description=(
            "GCS object prefix for the build (e.g. 'logs/job/build' or 'pr-logs/pull/org_repo/pr/job/build'). "
            "When empty, auto-resolves via prowjob.json or pr-logs/directory pointer."
        ),
    )

    @field_validator("prow_job_name", mode="before")
    @classmethod
    def _validate_prow_job_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_prow_job_name(v)

    @field_validator("build_id", mode="before")
    @classmethod
    def _validate_build_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_prow_build_id(v)

    @field_validator("gcs_bucket", mode="before")
    @classmethod
    def _validate_gcs_bucket(cls, v: object) -> str:
        return normalize_gcs_bucket(v)

    @field_validator("gcs_prefix", mode="before")
    @classmethod
    def _validate_gcs_prefix(cls, v: object) -> str:
        return normalize_gcs_prefix(v)

    @field_validator("prow_url", mode="before")
    @classmethod
    def _validate_prow_url(cls, v: object) -> str:
        return normalize_prow_url(v)

    @model_validator(mode="after")
    def _validate_by_type(self) -> "UnifiedAnalyzeRequest":
        """Validate required fields based on analysis type."""
        if self.type == "jenkins":
            if not self.job_name:
                raise ValueError("job_name is required for type=jenkins")
            if self.build_number is None:
                raise ValueError("build_number is required for type=jenkins")
        elif self.type == "file":
            if not self.raw_xml:
                raise ValueError("raw_xml is required for type=file")
            if self.failures is not None:
                raise ValueError(
                    "failures cannot be provided for type=file (use type=raw)"
                )
        elif self.type == "raw":
            if not self.failures:
                raise ValueError("failures is required for type=raw")
            if self.raw_xml is not None:
                raise ValueError(
                    "raw_xml cannot be provided for type=raw (use type=file)"
                )
        elif self.type == "prow":
            if not self.prow_job_name:
                raise ValueError("prow_job_name is required for type=prow")
            if not self.build_id:
                raise ValueError("build_id is required for type=prow")
            if self.gcs_prefix:
                validate_gcs_prefix_suffix(
                    self.gcs_prefix, self.prow_job_name, self.build_id
                )
        return self


class ReAnalyzeRequest(_JenkinsParamsMixin, _NameTagsMixin, BaseAnalysisRequest):
    """Override fields for ``POST /re-analyze/{job_id}``."""


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

    @field_validator("jira_project_key", "jira_security_level")
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
PatternLiteral = Literal[
    "NEW", "REGRESSION", "FLAKY", "INTERMITTENT", "KNOWN_BUG", "PERSISTENT"
]
# Legacy alias — kept for backward compatibility with existing data.
# New code should use PatternLiteral instead.
HistoryClassificationLiteral = Literal[
    "FLAKY", "REGRESSION", "INFRASTRUCTURE", "KNOWN_BUG", "INTERMITTENT"
]


class OverrideClassificationRequest(_ChildJobFieldsValidator):
    """Request body for overriding a failure's classification (root cause axis)."""

    test_name: str
    classification: OverrideClassificationLiteral


class OverridePatternRequest(_ChildJobFieldsValidator):
    """Request body for overriding a failure's pattern axis."""

    test_name: str
    pattern: PatternLiteral


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


class SetTrackedInRequest(_ChildJobFieldsValidator):
    """Request body for setting/clearing the tracked-in URL on a failure."""

    test_name: str = Field(description="Full test name")
    url: str = Field(default="", description="Tracking issue URL (empty to clear)")
    type: str = Field(
        default="",
        description="Tracker type: 'jira', 'github', or '' to clear",
    )

    @field_validator("type")
    @classmethod
    def validate_tracked_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v and v not in ("jira", "github"):
            raise ValueError("type must be 'jira', 'github', or empty")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if v:
            from urllib.parse import urlparse

            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https", ""):
                raise ValueError("URL must use http or https scheme")
            if not parsed.scheme:
                raise ValueError("URL must include http:// or https://")
        return v


class ClassifyTestRequest(BaseModel):
    """Request body for classifying a test pattern (e.g., FLAKY, REGRESSION).

    Despite the field name ``classification``, the value is actually a pattern
    label.  The name is kept for backward compatibility with existing API
    consumers (AI history analysis, CLI ``classify`` command).
    """

    test_name: str
    classification: PatternLiteral
    reason: str = ""
    job_name: str = ""
    references: str = ""
    job_id: str
    child_build_number: Annotated[int, Field(ge=0)] = 0
    source: str = ""

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


class ReAnalyzeFailureRequest(BaseModel):
    """Optional overrides for per-failure re-analysis.

    Fields mirror BaseAnalysisRequest but are all optional since
    unset values fall back to the parent job's stored settings.
    """

    ai_provider: str | None = None
    ai_model: str | None = None
    ai_call_timeout: int | None = None
    raw_prompt: str | None = None
    peer_ai_configs: list[dict] | None = None
    peer_analysis_max_rounds: int | None = None
    tests_repo_url: str | None = None
    enable_jira: bool | None = None
    jira_url: str | None = None
    jira_project_key: str | None = None
    additional_repos: list[dict] | None = None


class ReportPortalPushResult(BaseModel):
    """Result from pushing classifications to Report Portal."""

    pushed: int = Field(description="Number of items successfully updated")
    unmatched: list[str] = Field(
        default_factory=list,
        description="RP item names that could not be matched to rootcoz failures or mapped to a defect type",
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


class ChatMessageRequest(BaseModel):
    """Request to send a chat message about an analyzed job."""

    message: str = Field(
        description="User's message text",
        min_length=1,
        max_length=50000,
    )

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Message cannot be blank")
        return v

    ai_provider: str | None = Field(
        default=None,
        description="AI provider to use for response (defaults to job's provider)",
    )
    ai_model: str | None = Field(
        default=None,
        description="AI model to use for response (defaults to job's model)",
    )


class ChatMessageResponse(BaseModel):
    """A single chat message."""

    id: int
    job_id: str
    role: str  # 'user' or 'assistant'
    content: str
    username: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    status: str = "completed"
    created_at: str


class ChatHistoryResponse(BaseModel):
    """Chat history for a job."""

    messages: list[ChatMessageResponse]
    total: int


CanViewReportsFlag = Annotated[
    bool,
    Strict(),
    Field(
        description=(
            "Stored DB flag for /api/reports/* access. Orthogonal to role: "
            "admins always have effective access even when this is false "
            "(so demotion does not leave an accidental grant)."
        ),
    ),
]


class AdminCreateUserRequest(BaseModel):
    """Admin POST /api/admin/users/create body."""

    username: str
    role: str = "reviewer"
    can_view_reports: CanViewReportsFlag = False


class SetCanViewReportsRequest(BaseModel):
    """Admin PUT /api/admin/users/{username}/can-view-reports body."""

    can_view_reports: CanViewReportsFlag


# Resolve forward references (CodeFix references SimilarIssue which is defined later)
CodeFix.model_rebuild()
