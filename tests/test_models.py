"""Tests for Pydantic models."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from jenkins_job_insight.models import (
    AdditionalRepo,
    AiConfigEntry,
    AnalysisDetail,
    AnalysisResult,
    AnalyzeRequest,
    CodeFix,
    CreateIssueRequest,
    CreateIssueResponse,
    FailureAnalysis,
    JiraMatch,
    JobStatus,
    OverrideClassificationRequest,
    PeerDebate,
    PeerRound,
    PreviewIssueRequest,
    PreviewIssueResponse,
    ProductBugReport,
    SimilarIssue,
)


class TestAnalyzeRequest:
    """Tests for the AnalyzeRequest model."""

    def test_analyze_request_creation(self) -> None:
        """Test creating a valid AnalyzeRequest."""
        request = AnalyzeRequest(
            job_name="test",
            build_number=123,
            tests_repo_url="https://github.com/example/repo",
        )
        assert request.job_name == "test"
        assert request.build_number == 123
        assert request.tests_repo_url == "https://github.com/example/repo"

    def test_analyze_request_without_tests_repo_url(self) -> None:
        """Test creating AnalyzeRequest without tests_repo_url (now optional)."""
        request = AnalyzeRequest(
            job_name="test",
            build_number=123,
        )
        assert request.job_name == "test"
        assert request.build_number == 123
        assert request.tests_repo_url is None

    def test_analyze_request_accepts_any_tests_repo_url_string(self) -> None:
        """Test that tests_repo_url accepts any string (no URL validation)."""
        request = AnalyzeRequest(
            job_name="test",
            build_number=123,
            tests_repo_url="https://github.com/org/repo:develop",
        )
        assert request.tests_repo_url == "https://github.com/org/repo:develop"

        # Also verify a non-URL string is accepted
        request2 = AnalyzeRequest(
            job_name="test",
            build_number=123,
            tests_repo_url="not-a-valid-url",
        )
        assert request2.tests_repo_url == "not-a-valid-url"

    def test_wait_for_completion_defaults(self) -> None:
        """Test wait_for_completion fields have correct defaults."""
        request = AnalyzeRequest(job_name="test", build_number=1)
        assert request.wait_for_completion is True
        assert request.poll_interval_minutes == 2
        assert request.max_wait_minutes == 0

    def test_wait_for_completion_custom_values(self) -> None:
        """Test overriding wait_for_completion fields."""
        request = AnalyzeRequest(
            job_name="test",
            build_number=1,
            wait_for_completion=False,
            poll_interval_minutes=5,
            max_wait_minutes=60,
        )
        assert request.wait_for_completion is False
        assert request.poll_interval_minutes == 5
        assert request.max_wait_minutes == 60

    def test_poll_interval_rejects_zero(self) -> None:
        """Test that poll_interval_minutes rejects zero."""
        with pytest.raises(ValidationError):
            AnalyzeRequest(job_name="test", build_number=1, poll_interval_minutes=0)

    @pytest.mark.parametrize("field", ["poll_interval_minutes", "max_wait_minutes"])
    def test_wait_fields_reject_negative(self, field: str) -> None:
        """Test that poll_interval_minutes and max_wait_minutes reject negative values."""
        with pytest.raises(ValidationError):
            AnalyzeRequest(job_name="test", build_number=1, **{field: -1})

    def test_max_wait_minutes_accepts_zero(self) -> None:
        """Test that max_wait_minutes=0 is valid (means no limit)."""
        request = AnalyzeRequest(job_name="test", build_number=1, max_wait_minutes=0)
        assert request.max_wait_minutes == 0


class TestAnalysisResultWaitingStatus:
    """Tests for 'waiting' status support in AnalysisResult and JobStatus."""

    def test_analysis_result_accepts_waiting_status(self) -> None:
        """Test that AnalysisResult accepts 'waiting' as a valid status."""
        result = AnalysisResult(
            job_id="test-id",
            status="waiting",
            summary="Waiting for Jenkins job to complete",
        )
        assert result.status == "waiting"

    def test_job_status_accepts_waiting_status(self) -> None:
        """Test that JobStatus accepts 'waiting' as a valid status."""
        status = JobStatus(
            job_id="test-id",
            status="waiting",
            created_at=datetime.now(),
        )
        assert status.status == "waiting"


class TestProductBugReport:
    """Tests for the ProductBugReport model."""

    def test_creation_with_defaults(self) -> None:
        """Test creating with all defaults."""
        report = ProductBugReport()
        assert report.title == ""
        assert report.severity == ""
        assert report.component == ""

    def test_creation_with_values(self) -> None:
        """Test creating with all fields."""
        report = ProductBugReport(
            title="Bug title",
            severity="high",
            component="auth",
            description="Something is broken",
            evidence="Stack trace here",
        )
        assert report.title == "Bug title"
        assert report.severity == "high"


class TestCodeFix:
    """Tests for the CodeFix model."""

    def test_creation_with_defaults(self) -> None:
        """Test creating with all defaults."""
        fix = CodeFix()
        assert fix.file == ""
        assert fix.line == ""
        assert fix.change == ""
        assert fix.original_code is None
        assert fix.suggested_code is None

    def test_creation_with_values(self) -> None:
        """Test creating with all fields."""
        fix = CodeFix(file="src/main.py", line="42", change="Fix the bug")
        assert fix.file == "src/main.py"
        assert fix.line == "42"

    def test_creation_with_code_fields(self) -> None:
        """Test creating with original_code and suggested_code."""
        fix = CodeFix(
            file="src/main.py",
            line="42",
            change="Replace broken assertion",
            original_code="assert x == 1",
            suggested_code="assert x == 2",
        )
        assert fix.original_code == "assert x == 1"
        assert fix.suggested_code == "assert x == 2"

    def test_code_fields_optional_backward_compatible(self) -> None:
        """Test that existing data without code fields still works."""
        data = {"file": "test.py", "line": "10", "change": "fix it"}
        fix = CodeFix(**data)
        assert fix.original_code is None
        assert fix.suggested_code is None

    def test_serialization_includes_code_fields(self) -> None:
        """Test that code fields appear in serialized output when set."""
        fix = CodeFix(
            file="a.py",
            line="1",
            change="fix",
            original_code="old",
            suggested_code="new",
        )
        data = fix.model_dump()
        assert data["original_code"] == "old"
        assert data["suggested_code"] == "new"

    def test_serialization_with_none_code_fields(self) -> None:
        """Test that None code fields are serialized as None."""
        fix = CodeFix(file="a.py", line="1", change="fix")
        data = fix.model_dump()
        assert data["original_code"] is None
        assert data["suggested_code"] is None


class TestAnalysisDetail:
    """Tests for the AnalysisDetail model."""

    def test_creation_with_defaults(self) -> None:
        """Test creating with all defaults."""
        detail = AnalysisDetail()
        assert detail.classification == ""
        assert detail.affected_tests == []
        assert detail.details == ""
        assert detail.code_fix is False
        assert detail.product_bug_report is False

    def test_creation_with_code_fix(self) -> None:
        """Test creating with a code fix."""
        detail = AnalysisDetail(
            classification="CODE ISSUE",
            details="Missing import",
            code_fix=CodeFix(file="test.py", line="1", change="Add import"),
        )
        assert detail.code_fix
        assert detail.code_fix.file == "test.py"
        assert not detail.product_bug_report

    def test_creation_with_bug_report(self) -> None:
        """Test creating with a product bug report."""
        detail = AnalysisDetail(
            classification="PRODUCT BUG",
            details="API broken",
            product_bug_report=ProductBugReport(title="API bug", severity="high"),
        )
        assert detail.product_bug_report
        assert detail.product_bug_report.title == "API bug"
        assert not detail.code_fix


class TestFailureAnalysis:
    """Tests for the FailureAnalysis model."""

    def test_failure_analysis_creation(self) -> None:
        """Test creating a valid FailureAnalysis."""
        analysis = FailureAnalysis(
            test_name="test_example",
            error="AssertionError: Expected True, got False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="The test assertion is wrong",
            ),
        )
        assert analysis.test_name == "test_example"
        assert analysis.error == "AssertionError: Expected True, got False"
        assert analysis.analysis.classification == "CODE ISSUE"

    def test_failure_analysis_with_full_detail(self) -> None:
        """Test FailureAnalysis with full AnalysisDetail content."""
        analysis = FailureAnalysis(
            test_name="test_login",
            error="HTTP 500 Internal Server Error",
            analysis=AnalysisDetail(
                classification="PRODUCT BUG",
                affected_tests=["test_login"],
                details="The authentication service is failing with a 500 error.",
                product_bug_report=ProductBugReport(
                    title="Authentication fails with valid credentials",
                    severity="high",
                    component="auth",
                    description="Users cannot log in",
                    evidence="HTTP 500 response",
                ),
            ),
        )
        assert analysis.test_name == "test_login"
        assert analysis.analysis.classification == "PRODUCT BUG"
        assert analysis.analysis.product_bug_report
        assert (
            analysis.analysis.product_bug_report.title
            == "Authentication fails with valid credentials"
        )

    def test_failure_analysis_required_fields(self) -> None:
        """Test that all required fields must be provided."""
        with pytest.raises(ValidationError):
            FailureAnalysis(
                test_name="test_example",
                # missing error and analysis
            )

        with pytest.raises(ValidationError):
            FailureAnalysis(
                test_name="test_example",
                error="Error message",
                # missing analysis
            )


class TestAnalysisResult:
    """Tests for the AnalysisResult model."""

    def test_analysis_result_creation(self) -> None:
        """Test creating a valid AnalysisResult."""
        result = AnalysisResult(
            job_id="job-123",
            jenkins_url="https://jenkins.example.com/job/test/123/",
            status="completed",
            summary="Analysis complete",
            failures=[],
        )
        assert result.job_id == "job-123"
        assert result.status == "completed"
        assert result.failures == []

    def test_analysis_result_with_failures(
        self, sample_failure_analysis: FailureAnalysis
    ) -> None:
        """Test AnalysisResult with failure list."""
        result = AnalysisResult(
            job_id="job-123",
            jenkins_url="https://jenkins.example.com/job/test/123/",
            status="completed",
            summary="1 failure found",
            failures=[sample_failure_analysis],
        )
        assert len(result.failures) == 1
        assert result.failures[0].test_name == sample_failure_analysis.test_name

    @pytest.mark.parametrize("status", ["pending", "running", "completed", "failed"])
    def test_analysis_result_valid_statuses(self, status: str) -> None:
        """Test that valid status values are accepted."""
        result = AnalysisResult(
            job_id="job-123",
            jenkins_url="https://jenkins.example.com/job/test/123/",
            status=status,
            summary="Summary",
        )
        assert result.status == status

    def test_analysis_result_invalid_status(self) -> None:
        """Test that invalid status raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AnalysisResult(
                job_id="job-123",
                jenkins_url="https://jenkins.example.com/job/test/123/",
                status="invalid",
                summary="Summary",
            )
        errors = exc_info.value.errors()
        assert any("status" in str(e) for e in errors)

    def test_analysis_result_default_failures(self) -> None:
        """Test that failures defaults to empty list."""
        result = AnalysisResult(
            job_id="job-123",
            jenkins_url="https://jenkins.example.com/job/test/123/",
            status="completed",
            summary="Summary",
        )
        assert result.failures == []


class TestJobStatus:
    """Tests for the JobStatus model."""

    def test_job_status_creation(self) -> None:
        """Test creating a valid JobStatus."""
        now = datetime.now()
        status = JobStatus(
            job_id="job-123",
            status="running",
            created_at=now,
        )
        assert status.job_id == "job-123"
        assert status.status == "running"
        assert status.created_at == now

    @pytest.mark.parametrize(
        "status_val", ["pending", "running", "completed", "failed"]
    )
    def test_job_status_valid_statuses(self, status_val: str) -> None:
        """Test that valid status values are accepted."""
        status = JobStatus(
            job_id="job-123",
            status=status_val,
            created_at=datetime.now(),
        )
        assert status.status == status_val

    def test_job_status_invalid_status(self) -> None:
        """Test that invalid status raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            JobStatus(
                job_id="job-123",
                status="invalid",
                created_at=datetime.now(),
            )
        errors = exc_info.value.errors()
        assert any("status" in str(e) for e in errors)


class TestJiraMatch:
    """Tests for the JiraMatch model."""

    def test_creation_with_required_fields(self) -> None:
        """Test creating with only required fields."""
        match = JiraMatch(key="PROJ-123", summary="Bug title")
        assert match.key == "PROJ-123"
        assert match.summary == "Bug title"
        assert match.status == ""
        assert match.priority == ""
        assert match.url == ""
        assert match.score == 0.0

    def test_creation_with_all_fields(self) -> None:
        """Test creating with all fields."""
        match = JiraMatch(
            key="PROJ-456",
            summary="Login fails",
            status="Open",
            priority="High",
            url="https://jira.example.com/browse/PROJ-456",
            score=0.85,
        )
        assert match.key == "PROJ-456"
        assert match.status == "Open"
        assert match.score == 0.85


class TestProductBugReportJiraFields:
    """Tests for Jira-related fields on ProductBugReport."""

    def test_defaults_to_empty_lists(self) -> None:
        """Jira fields default to empty lists for backward compatibility."""
        report = ProductBugReport()
        assert report.jira_search_keywords == []
        assert report.jira_matches == []

    def test_with_search_keywords(self) -> None:
        """Test creating with search keywords."""
        report = ProductBugReport(
            title="Bug",
            jira_search_keywords=["login", "auth"],
        )
        assert report.jira_search_keywords == ["login", "auth"]

    def test_with_jira_matches(self) -> None:
        """Test creating with Jira matches."""
        matches = [
            JiraMatch(key="PROJ-1", summary="Match 1"),
            JiraMatch(key="PROJ-2", summary="Match 2"),
        ]
        report = ProductBugReport(
            title="Bug",
            jira_matches=matches,
        )
        assert len(report.jira_matches) == 2
        assert report.jira_matches[0].key == "PROJ-1"

    def test_serialization_includes_jira_fields(self) -> None:
        """Test that Jira fields are included in serialization."""
        report = ProductBugReport(
            title="Bug",
            jira_search_keywords=["kw1"],
            jira_matches=[JiraMatch(key="PROJ-1", summary="Match")],
        )
        data = report.model_dump()
        assert "jira_search_keywords" in data
        assert "jira_matches" in data
        assert len(data["jira_matches"]) == 1


class TestPreviewIssueRequest:
    """Tests for the PreviewIssueRequest model."""

    def test_valid_request(self) -> None:
        """Test creating a valid PreviewIssueRequest."""
        req = PreviewIssueRequest(test_name="tests.TestFoo.test_bar")
        assert req.test_name == "tests.TestFoo.test_bar"
        assert req.child_job_name == ""
        assert req.child_build_number == 0

    def test_child_job_name_with_zero_build_number_allowed(self) -> None:
        """Test that child_job_name with build_number=0 is allowed (match any build)."""
        req = PreviewIssueRequest(
            test_name="tests.TestFoo.test_bar",
            child_job_name="child-job",
            child_build_number=0,
        )
        assert req.child_job_name == "child-job"
        assert req.child_build_number == 0


class TestCreateIssueRequest:
    """Tests for the CreateIssueRequest model."""

    def test_valid_request(self) -> None:
        """Test creating a valid CreateIssueRequest."""
        req = CreateIssueRequest(
            test_name="tests.TestFoo.test_bar",
            title="Bug: login fails",
            body="## Details\nLogin returns 500",
        )
        assert req.title == "Bug: login fails"
        assert req.body == "## Details\nLogin returns 500"

    def test_title_required(self) -> None:
        """Test that empty title is rejected."""
        with pytest.raises(ValueError):
            CreateIssueRequest(
                test_name="tests.TestFoo.test_bar",
                title="",
                body="some body",
            )

    def test_whitespace_only_title_rejected(self) -> None:
        """Test that whitespace-only title is rejected."""
        with pytest.raises(ValueError):
            CreateIssueRequest(
                test_name="tests.TestFoo.test_bar",
                title="   ",
                body="some body",
            )


class TestOverrideClassificationRequest:
    """Tests for the OverrideClassificationRequest model."""

    def test_valid_code_issue(self) -> None:
        """Test CODE ISSUE classification."""
        req = OverrideClassificationRequest(
            test_name="tests.TestFoo.test_bar",
            classification="CODE ISSUE",
        )
        assert req.classification == "CODE ISSUE"

    def test_valid_product_bug(self) -> None:
        """Test PRODUCT BUG classification."""
        req = OverrideClassificationRequest(
            test_name="tests.TestFoo.test_bar",
            classification="PRODUCT BUG",
        )
        assert req.classification == "PRODUCT BUG"

    def test_invalid_classification(self) -> None:
        """Test that invalid classification is rejected."""
        with pytest.raises(ValueError):
            OverrideClassificationRequest(
                test_name="tests.TestFoo.test_bar",
                classification="UNKNOWN",
            )


class TestSimilarIssue:
    """Tests for the SimilarIssue model."""

    def test_defaults(self) -> None:
        """Test default values."""
        issue = SimilarIssue()
        assert issue.number is None
        assert issue.key == ""
        assert issue.title == ""
        assert issue.url == ""
        assert issue.status == ""

    def test_github_style(self) -> None:
        """Test GitHub-style similar issue."""
        issue = SimilarIssue(
            number=42,
            title="Login fails",
            url="https://github.com/org/repo/issues/42",
            status="open",
        )
        assert issue.number == 42
        assert issue.title == "Login fails"

    def test_jira_style(self) -> None:
        """Test Jira-style similar issue."""
        issue = SimilarIssue(
            key="PROJ-123",
            title="DNS timeout",
            url="https://jira.example.com/browse/PROJ-123",
            status="Open",
        )
        assert issue.key == "PROJ-123"


class TestPreviewIssueResponse:
    """Tests for the PreviewIssueResponse model."""

    def test_creation(self) -> None:
        """Test creating a PreviewIssueResponse."""
        resp = PreviewIssueResponse(
            title="Bug title",
            body="## Details",
            similar_issues=[SimilarIssue(number=1, title="Similar")],
        )
        assert resp.title == "Bug title"
        assert len(resp.similar_issues) == 1

    def test_empty_similar_issues(self) -> None:
        """Test that similar_issues defaults to empty list."""
        resp = PreviewIssueResponse(title="Bug", body="Details")
        assert resp.similar_issues == []


class TestCreateIssueResponse:
    """Tests for the CreateIssueResponse model."""

    def test_creation(self) -> None:
        """Test creating a CreateIssueResponse."""
        resp = CreateIssueResponse(
            url="https://github.com/org/repo/issues/99",
            title="Bug fix",
            comment_id=42,
        )
        assert resp.url == "https://github.com/org/repo/issues/99"
        assert resp.key == ""
        assert resp.comment_id == 42

    def test_jira_response(self) -> None:
        """Test Jira-style CreateIssueResponse."""
        resp = CreateIssueResponse(
            url="https://jira.example.com/browse/PROJ-456",
            key="PROJ-456",
            title="DNS timeout",
        )
        assert resp.key == "PROJ-456"
        assert resp.comment_id == 0

    def test_number_field_exists(self) -> None:
        """Finding 3: CreateIssueResponse should have a number field."""
        resp = CreateIssueResponse(
            url="https://github.com/org/repo/issues/99",
            title="Bug fix",
            number=99,
        )
        assert resp.number == 99

    def test_number_field_defaults_to_zero(self) -> None:
        """Finding 3: number field should default to 0."""
        resp = CreateIssueResponse(
            url="https://github.com/org/repo/issues/99",
            title="Bug fix",
        )
        assert resp.number == 0


class TestAiConfigEntry:
    """Tests for the AiConfigEntry model."""

    def test_valid_entry(self) -> None:
        """Test creating a valid AiConfigEntry."""
        entry = AiConfigEntry(ai_provider="claude", ai_model="opus")
        assert entry.ai_provider == "claude"
        assert entry.ai_model == "opus"

    @pytest.mark.parametrize("provider", ["claude", "gemini", "cursor"])
    def test_valid_providers(self, provider: str) -> None:
        """Test all valid AI providers are accepted."""
        entry = AiConfigEntry(ai_provider=provider, ai_model="model-1")
        assert entry.ai_provider == provider

    def test_invalid_provider(self) -> None:
        """Test that invalid AI provider is rejected."""
        with pytest.raises(ValidationError):
            AiConfigEntry(ai_provider="openai", ai_model="gpt4")

    def test_empty_model_rejected(self) -> None:
        """Test that empty ai_model string is rejected (min_length=1)."""
        with pytest.raises(ValidationError):
            AiConfigEntry(ai_provider="claude", ai_model="")

    def test_whitespace_only_model_rejected(self) -> None:
        """Test that whitespace-only ai_model is rejected after stripping."""
        with pytest.raises(ValidationError):
            AiConfigEntry(ai_provider="claude", ai_model="   ")

    def test_model_with_surrounding_whitespace_stripped(self) -> None:
        """Test that ai_model is stripped of surrounding whitespace."""
        entry = AiConfigEntry(ai_provider="claude", ai_model="  opus  ")
        assert entry.ai_model == "opus"


class TestPeerRound:
    """Tests for the PeerRound model."""

    def test_creation(self) -> None:
        """Test creating a valid PeerRound."""
        r = PeerRound(
            round=1,
            ai_provider="claude",
            ai_model="opus",
            role="orchestrator",
            classification="CODE ISSUE",
            details="test",
        )
        assert r.round == 1
        assert r.role == "orchestrator"
        assert r.agrees_with_orchestrator is None

    def test_peer_role(self) -> None:
        """Test PeerRound with peer role and agreement set."""
        r = PeerRound(
            round=2,
            ai_provider="gemini",
            ai_model="pro",
            role="peer",
            classification="PRODUCT BUG",
            details="disagree",
            agrees_with_orchestrator=False,
        )
        assert r.role == "peer"
        assert r.agrees_with_orchestrator is False

    def test_invalid_role(self) -> None:
        """Test that invalid role is rejected."""
        with pytest.raises(ValidationError):
            PeerRound(
                round=1,
                ai_provider="claude",
                ai_model="opus",
                role="observer",
                classification="CODE ISSUE",
                details="test",
            )


class TestPeerDebate:
    """Tests for the PeerDebate model."""

    def test_creation(self) -> None:
        """Test creating a valid PeerDebate."""
        d = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[{"ai_provider": "claude", "ai_model": "opus"}],
            rounds=[],
        )
        assert d.consensus_reached is True
        assert d.rounds_used == 1
        assert d.max_rounds == 3
        assert len(d.ai_configs) == 1

    def test_ai_configs_are_ai_config_entry_instances(self) -> None:
        """Test that ai_configs items are coerced to AiConfigEntry instances."""
        d = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[{"ai_provider": "claude", "ai_model": "opus"}],
            rounds=[],
        )
        assert isinstance(d.ai_configs[0], AiConfigEntry)
        assert d.ai_configs[0].ai_provider == "claude"
        assert d.ai_configs[0].ai_model == "opus"

    def test_ai_configs_reject_invalid_provider(self) -> None:
        """Test that ai_configs rejects invalid AI provider."""
        with pytest.raises(ValidationError):
            PeerDebate(
                consensus_reached=True,
                rounds_used=1,
                max_rounds=3,
                ai_configs=[{"ai_provider": "openai", "ai_model": "gpt4"}],
                rounds=[],
            )

    def test_ai_configs_reject_empty_model(self) -> None:
        """Test that ai_configs rejects empty ai_model."""
        with pytest.raises(ValidationError):
            PeerDebate(
                consensus_reached=True,
                rounds_used=1,
                max_rounds=3,
                ai_configs=[{"ai_provider": "claude", "ai_model": ""}],
                rounds=[],
            )

    def test_ai_configs_accept_model_instances(self) -> None:
        """Test that ai_configs accepts AiConfigEntry instances directly."""
        entry = AiConfigEntry(ai_provider="gemini", ai_model="pro")
        d = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[entry],
            rounds=[],
        )
        assert d.ai_configs[0] is entry

    def test_ai_configs_extra_fields_ignored(self) -> None:
        """Test that extra fields in ai_configs dicts are silently ignored.

        The peer_analysis module passes dicts with a 'role' key that
        AiConfigEntry does not define. Pydantic v2 ignores extra fields
        by default, so this should work without error.
        """
        d = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[
                {"ai_provider": "claude", "ai_model": "opus", "role": "orchestrator"},
                {"ai_provider": "gemini", "ai_model": "pro", "role": "peer"},
            ],
            rounds=[],
        )
        assert len(d.ai_configs) == 2
        assert d.ai_configs[0].ai_provider == "claude"
        assert d.ai_configs[1].ai_provider == "gemini"

    def test_with_rounds(self) -> None:
        """Test PeerDebate with actual rounds."""
        r = PeerRound(
            round=1,
            ai_provider="claude",
            ai_model="opus",
            role="orchestrator",
            classification="CODE ISSUE",
            details="root cause",
        )
        d = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[],
            rounds=[r],
        )
        assert len(d.rounds) == 1
        assert d.rounds[0].classification == "CODE ISSUE"


class TestFailureAnalysisPeerDebate:
    """Tests for peer_debate field on FailureAnalysis."""

    def test_peer_debate_none_by_default(self) -> None:
        """Test that peer_debate is None when not provided."""
        fa = FailureAnalysis(
            test_name="t",
            error="e",
            analysis=AnalysisDetail(details="d"),
            error_signature="sig",
        )
        data = fa.model_dump(mode="json")
        assert data.get("peer_debate") is None

    def test_with_peer_debate(self) -> None:
        """Test FailureAnalysis with a peer debate attached."""
        debate = PeerDebate(
            consensus_reached=True,
            rounds_used=1,
            max_rounds=3,
            ai_configs=[],
            rounds=[],
        )
        fa = FailureAnalysis(
            test_name="t",
            error="e",
            analysis=AnalysisDetail(details="d"),
            error_signature="sig",
            peer_debate=debate,
        )
        data = fa.model_dump(mode="json")
        assert data["peer_debate"]["consensus_reached"] is True


class TestAdditionalRepo:
    """Tests for AdditionalRepo model."""

    def test_valid_additional_repo(self) -> None:
        repo = AdditionalRepo(name="infra", url="https://github.com/org/infra")
        assert repo.name == "infra"
        assert str(repo.url) == "https://github.com/org/infra"

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="  ", url="https://github.com/org/infra")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="", url="https://github.com/org/infra")

    def test_invalid_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="infra", url="not-a-url")

    def test_name_stripped(self) -> None:
        repo = AdditionalRepo(name="  infra  ", url="https://github.com/org/infra")
        assert repo.name == "infra"

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="../evil", url="https://github.com/org/repo")

    def test_slash_in_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="foo/bar", url="https://github.com/org/repo")

    def test_backslash_in_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdditionalRepo(name="foo\\bar", url="https://github.com/org/repo")

    def test_dot_prefixed_name_rejected(self) -> None:
        """Dot-prefixed names like '.hidden' must be rejected."""
        with pytest.raises(ValidationError):
            AdditionalRepo(name=".hidden", url="https://github.com/org/repo")

    def test_dot_name_rejected(self) -> None:
        """Single dot '.' name must be rejected."""
        with pytest.raises(ValidationError):
            AdditionalRepo(name=".", url="https://github.com/org/repo")

    def test_dotgit_name_rejected(self) -> None:
        """'.git' name must be rejected."""
        with pytest.raises(ValidationError):
            AdditionalRepo(name=".git", url="https://github.com/org/repo")

    def test_reserved_name_rejected(self) -> None:
        """Reserved name 'build-artifacts' must be rejected."""
        with pytest.raises(ValidationError):
            AdditionalRepo(name="build-artifacts", url="https://github.com/org/repo")

    def test_ref_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace in ref is stripped."""
        repo = AdditionalRepo(
            name="infra", url="https://github.com/org/infra", ref="  main  "
        )
        assert repo.ref == "main"

    def test_ref_whitespace_only_becomes_empty(self) -> None:
        """Whitespace-only ref is stripped to empty string."""
        repo = AdditionalRepo(
            name="infra", url="https://github.com/org/infra", ref="   "
        )
        assert repo.ref == ""

    def test_token_optional(self) -> None:
        """Token defaults to None when not provided."""
        repo = AdditionalRepo(name="infra", url="https://github.com/org/infra")
        assert repo.token is None

    def test_token_accepted(self) -> None:
        """Token field accepts a string value."""
        repo = AdditionalRepo(
            name="infra",
            url="https://github.com/org/infra",
            token="tok",  # noqa: S106
        )
        assert repo.token == "tok"  # noqa: S105

    def test_token_none_explicit(self) -> None:
        """Explicitly passing None for token is accepted."""
        repo = AdditionalRepo(
            name="infra", url="https://github.com/org/infra", token=None
        )
        assert repo.token is None

    def test_backward_compat_no_token(self) -> None:
        """AdditionalRepo created without token field is backward compatible."""
        data = {"name": "infra", "url": "https://github.com/org/infra"}
        repo = AdditionalRepo(**data)
        assert repo.token is None
        dumped = repo.model_dump(mode="json")
        assert "token" in dumped
        assert dumped["token"] is None


class TestAdditionalReposDuplicateNames:
    """Tests for duplicate name rejection in additional_repos."""

    def test_duplicate_names_rejected(self) -> None:
        """Duplicate additional repo names are rejected by validation."""
        with pytest.raises(ValidationError):
            AnalyzeRequest(
                job_name="test",
                build_number=1,
                additional_repos=[
                    {"name": "infra", "url": "https://github.com/org/infra"},
                    {"name": "infra", "url": "https://github.com/org/other"},
                ],
            )

    def test_unique_names_accepted(self) -> None:
        """Distinct additional repo names pass validation."""
        request = AnalyzeRequest(
            job_name="test",
            build_number=1,
            additional_repos=[
                {"name": "infra", "url": "https://github.com/org/infra"},
                {"name": "product", "url": "https://github.com/org/product"},
            ],
        )
        assert len(request.additional_repos) == 2

    def test_none_additional_repos_accepted(self) -> None:
        """None value for additional_repos passes validation."""
        request = AnalyzeRequest(job_name="test", build_number=1, additional_repos=None)
        assert request.additional_repos is None

    def test_empty_list_accepted(self) -> None:
        """Empty list for additional_repos passes validation."""
        request = AnalyzeRequest(job_name="test", build_number=1, additional_repos=[])
        assert request.additional_repos == []


class TestAdditionalReposOnRequest:
    """Tests for additional_repos field on BaseAnalysisRequest."""

    def test_additional_repos_default_none(self) -> None:
        request = AnalyzeRequest(job_name="test", build_number=1)
        assert request.additional_repos is None

    def test_additional_repos_with_valid_entries(self) -> None:
        request = AnalyzeRequest(
            job_name="test",
            build_number=1,
            additional_repos=[
                {"name": "infra", "url": "https://github.com/org/infra"},
                {"name": "product", "url": "https://github.com/org/product"},
            ],
        )
        assert request.additional_repos is not None
        assert len(request.additional_repos) == 2
        assert request.additional_repos[0].name == "infra"

    def test_additional_repos_empty_list(self) -> None:
        request = AnalyzeRequest(job_name="test", build_number=1, additional_repos=[])
        assert request.additional_repos == []


class TestBaseAnalysisRequestPeerFields:
    """Tests for peer analysis fields on BaseAnalysisRequest."""

    def test_no_peers_by_default(self) -> None:
        """Test that peer_ai_configs is None by default."""
        req = AnalyzeRequest(job_name="j", build_number=1)
        assert req.peer_ai_configs is None

    def test_default_max_rounds(self) -> None:
        """Test that peer_analysis_max_rounds defaults to 3."""
        req = AnalyzeRequest(job_name="j", build_number=1)
        assert req.peer_analysis_max_rounds == 3

    def test_with_peers(self) -> None:
        """Test AnalyzeRequest with peer AI configs."""
        req = AnalyzeRequest(
            job_name="j",
            build_number=1,
            peer_ai_configs=[
                AiConfigEntry(ai_provider="cursor", ai_model="gpt-5.4-xhigh"),
                AiConfigEntry(ai_provider="gemini", ai_model="pro"),
            ],
        )
        assert req.peer_ai_configs is not None
        assert len(req.peer_ai_configs) == 2
        assert req.peer_ai_configs[0].ai_provider == "cursor"

    def test_max_rounds_lower_bound(self) -> None:
        """Test that peer_analysis_max_rounds rejects 0."""
        with pytest.raises(ValidationError):
            AnalyzeRequest(job_name="j", build_number=1, peer_analysis_max_rounds=0)

    def test_max_rounds_upper_bound(self) -> None:
        """Test that peer_analysis_max_rounds rejects 11."""
        with pytest.raises(ValidationError):
            AnalyzeRequest(job_name="j", build_number=1, peer_analysis_max_rounds=11)

    def test_max_rounds_valid_bounds(self) -> None:
        """Test that peer_analysis_max_rounds accepts 1 and 10."""
        req1 = AnalyzeRequest(job_name="j", build_number=1, peer_analysis_max_rounds=1)
        assert req1.peer_analysis_max_rounds == 1
        req10 = AnalyzeRequest(
            job_name="j", build_number=1, peer_analysis_max_rounds=10
        )
        assert req10.peer_analysis_max_rounds == 10


class TestAnalyzeRequestForce:
    """Tests for the force field on AnalyzeRequest."""

    def test_force_defaults_to_false(self) -> None:
        """force defaults to False for backward compatibility."""
        req = AnalyzeRequest(job_name="j", build_number=1)
        assert req.force is False

    def test_force_true(self) -> None:
        """force can be set to True."""
        req = AnalyzeRequest(job_name="j", build_number=1, force=True)
        assert req.force is True

    def test_force_false_explicit(self) -> None:
        """force can be explicitly set to False."""
        req = AnalyzeRequest(job_name="j", build_number=1, force=False)
        assert req.force is False

    def test_force_in_model_fields_set_when_provided(self) -> None:
        """force appears in model_fields_set when explicitly provided."""
        req = AnalyzeRequest(job_name="j", build_number=1, force=True)
        assert "force" in req.model_fields_set

    def test_force_not_in_model_fields_set_when_omitted(self) -> None:
        """force does not appear in model_fields_set when omitted."""
        req = AnalyzeRequest(job_name="j", build_number=1)
        assert "force" not in req.model_fields_set
