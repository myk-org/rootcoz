"""Tests for the JJI CLI client."""

import json
from unittest.mock import patch

import httpx
import pytest

from jenkins_job_insight.cli.client import JJIClient, JJIError
from tests.conftest import (
    CLI_TEST_BASE_URL as BASE_URL,
    make_test_client as _make_client,
)


class TestJJIError:
    def test_error_stores_status_and_detail(self):
        err = JJIError(status_code=404, detail="Job not found")
        assert err.status_code == 404
        assert err.detail == "Job not found"
        assert "404" in str(err)
        assert "Job not found" in str(err)

    def test_error_without_detail(self):
        err = JJIError(status_code=500)
        assert err.status_code == 500
        assert err.detail == ""


class TestJJIClientHealth:
    def test_health(self):
        client = _make_client(
            lambda request: httpx.Response(200, json={"status": "healthy"})
        )
        result = client.health()
        assert result == {"status": "healthy"}

    def test_health_server_down(self):
        def raise_connect_error(request):
            raise httpx.ConnectError("Connection refused")

        client = _make_client(raise_connect_error)
        with pytest.raises(JJIError) as exc_info:
            client.health()
        assert exc_info.value.status_code == 0


class TestJJIClientResults:
    def test_list_results(self):
        sample_results = [
            {
                "job_id": "abc-123",
                "status": "completed",
                "jenkins_url": "https://jenkins.example.com/job/test/1/",
            },
            {
                "job_id": "def-456",
                "status": "running",
                "jenkins_url": "https://jenkins.example.com/job/test/2/",
            },
        ]
        client = _make_client(lambda request: httpx.Response(200, json=sample_results))
        result = client.list_results(limit=50)
        assert len(result) == 2
        assert result[0]["job_id"] == "abc-123"

    def test_get_result(self):
        sample = {
            "job_id": "abc-123",
            "status": "completed",
            "result": {"summary": "All good"},
        }
        client = _make_client(lambda request: httpx.Response(200, json=sample))
        result = client.get_result("abc-123")
        assert result["job_id"] == "abc-123"

    def test_get_result_not_found(self):
        client = _make_client(
            lambda request: httpx.Response(404, json={"detail": "Job not found"})
        )
        with pytest.raises(JJIError) as exc_info:
            client.get_result("nonexistent")
        assert exc_info.value.status_code == 404

    def test_delete_job(self):
        client = _make_client(
            lambda request: httpx.Response(
                200, json={"status": "deleted", "job_id": "abc-123"}
            )
        )
        result = client.delete_job("abc-123")
        assert result["status"] == "deleted"

    def test_delete_jobs_bulk(self):
        def handler(request):
            assert request.method == "DELETE"
            assert request.url.path == "/api/results/bulk"
            body = json.loads(request.content)
            assert body["job_ids"] == ["abc", "def"]
            return httpx.Response(
                200, json={"deleted": ["abc", "def"], "failed": [], "total": 2}
            )

        client = _make_client(handler)
        result = client.delete_jobs_bulk(["abc", "def"])
        assert result["deleted"] == ["abc", "def"]
        assert result["total"] == 2


class TestJJIClientDashboard:
    def test_dashboard(self):
        """Test dashboard returns all jobs."""

        def dashboard_handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/dashboard"
            return httpx.Response(
                200, json=[{"job_id": "test-1", "status": "completed"}]
            )

        client = _make_client(dashboard_handler)
        result = client.dashboard()
        assert len(result) == 1
        assert result[0]["job_id"] == "test-1"


class TestJJIClientAnalyze:
    def test_analyze(self):
        response_data = {
            "status": "queued",
            "job_id": "new-job-1",
            "message": "Analysis job queued.",
        }

        def handler(request):
            body = _parse_analyze_request(request)
            assert "sync" not in body
            return httpx.Response(202, json=response_data)

        client = _make_client(handler)
        result = client.analyze("my-job", 42)
        assert result["status"] == "queued"
        assert result["job_id"] == "new-job-1"


class TestJJIClientHistory:
    def test_get_test_history(self):
        sample = {"test_name": "tests.TestA.test_one", "failures": 3, "recent_runs": []}

        def handler(request):
            assert request.method == "GET"
            assert "/history/test/tests.TestA.test_one" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_test_history("tests.TestA.test_one")
        assert result["test_name"] == "tests.TestA.test_one"

    def test_search_by_signature(self):
        sample = {"signature": "sig-abc", "total_occurrences": 5, "tests": []}

        def handler(request):
            assert request.method == "GET"
            assert "signature=sig-abc" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.search_by_signature("sig-abc")
        assert result["signature"] == "sig-abc"

    def test_get_job_stats(self):
        sample = {"job_name": "ocp-e2e", "total_builds_analyzed": 10}

        def handler(request):
            assert request.method == "GET"
            assert "/history/stats/ocp-e2e" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_job_stats("ocp-e2e")
        assert result["job_name"] == "ocp-e2e"

    def test_get_all_failures(self):
        sample = {"failures": [], "total": 0, "limit": 50, "offset": 0}

        def handler(request):
            assert request.method == "GET"
            assert "/history/failures" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_all_failures()
        assert "failures" in result


class TestJJIClientClassifications:
    def test_classify_test(self):
        response_data = {"id": 1}

        def handler(request):
            assert request.method == "POST"
            return httpx.Response(201, json=response_data)

        client = _make_client(handler)
        result = client.classify_test(
            test_name="tests.TestA.test_one",
            classification="FLAKY",
            reason="intermittent DNS",
            job_id="job-123",
        )
        assert result["id"] == 1

    def test_get_classifications(self):
        sample = {
            "classifications": [
                {"test_name": "tests.TestA.test_one", "classification": "FLAKY"}
            ]
        }

        def handler(request):
            assert request.method == "GET"
            assert "/history/classifications" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_classifications()
        assert "classifications" in result


class TestJJIClientComments:
    def test_get_comments(self):
        sample = {"comments": [], "reviews": {}}

        def handler(request):
            assert request.method == "GET"
            assert "/results/job-1/comments" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_comments("job-1")
        assert "comments" in result

    def test_add_comment(self):
        response_data = {"id": 42}

        def handler(request):
            assert request.method == "POST"
            return httpx.Response(201, json=response_data)

        client = _make_client(handler)
        result = client.add_comment(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            comment="Opened JIRA-123",
        )
        assert result["id"] == 42

    def test_delete_comment(self):
        def handler(request):
            assert request.method == "DELETE"
            return httpx.Response(200, json={"status": "deleted"})

        client = _make_client(handler)
        result = client.delete_comment("job-1", 42)
        assert result["status"] == "deleted"


class TestJJIClientTimeout:
    def test_timeout_raises_error(self):
        def raise_timeout(request):
            raise httpx.ReadTimeout("Read timed out")

        client = _make_client(raise_timeout)
        with pytest.raises(JJIError) as exc_info:
            client.health()
        assert exc_info.value.status_code == 0
        assert "timed out" in exc_info.value.detail.lower()


class TestJJIClientUsername:
    def test_username_sent_as_cookie(self):
        def check_cookie(request):
            assert request.headers.get("cookie") is not None
            assert "jji_username=testuser" in request.headers.get("cookie", "")
            return httpx.Response(200, json={"status": "deleted", "job_id": "abc"})

        client = _make_client(check_cookie, username="testuser")
        client.delete_job("abc")


class TestMalformedUrl:
    def test_malformed_url_error(self):
        """Malformed URLs should raise JJIError, not raw httpx exception."""
        client = JJIClient(server_url="not-a-valid-url")
        with pytest.raises(JJIError) as exc_info:
            client.health()
        assert exc_info.value.status_code == 0


class TestJJIClientBugCreation:
    def test_preview_github_issue(self):
        response_data = {
            "title": "Fix: login handler",
            "body": "## Details...",
            "similar_issues": [],
        }

        def handler(request):
            assert request.method == "POST"
            assert "/preview-github-issue" in str(request.url)
            return httpx.Response(200, json=response_data)

        client = _make_client(handler)
        result = client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
        )
        assert result["title"] == "Fix: login handler"

    def test_preview_jira_bug(self):
        response_data = {
            "title": "DNS timeout",
            "body": "h2. Summary...",
            "similar_issues": [],
        }

        def handler(request):
            assert request.method == "POST"
            assert "/preview-jira-bug" in str(request.url)
            return httpx.Response(200, json=response_data)

        client = _make_client(handler)
        result = client.preview_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
        )
        assert result["title"] == "DNS timeout"

    def test_create_github_issue(self):
        response_data = {
            "url": "https://github.com/org/repo/issues/99",
            "key": "",
            "title": "Bug: login fails",
            "comment_id": 42,
        }

        def handler(request):
            assert request.method == "POST"
            assert "/create-github-issue" in str(request.url)
            return httpx.Response(201, json=response_data)

        client = _make_client(handler)
        result = client.create_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug: login fails",
            body="Details...",
        )
        assert result["url"] == "https://github.com/org/repo/issues/99"

    def test_create_jira_bug(self):
        response_data = {
            "url": "https://jira.example.com/browse/PROJ-456",
            "key": "PROJ-456",
            "title": "DNS timeout",
            "comment_id": 43,
        }

        def handler(request):
            assert request.method == "POST"
            assert "/create-jira-bug" in str(request.url)
            return httpx.Response(201, json=response_data)

        client = _make_client(handler)
        result = client.create_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="DNS timeout",
            body="Description...",
        )
        assert result["key"] == "PROJ-456"

    def test_override_classification(self):
        def handler(request):
            assert request.method == "PUT"
            assert "/override-classification" in str(request.url)
            return httpx.Response(
                200, json={"status": "ok", "classification": "PRODUCT BUG"}
            )

        client = _make_client(handler)
        result = client.override_classification(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            classification="PRODUCT BUG",
        )
        assert result["classification"] == "PRODUCT BUG"

    def test_preview_github_issue_with_child_job(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["child_job_name"] == "child-runner"
            assert body["child_build_number"] == 5
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        result = client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            child_job_name="child-runner",
            child_build_number=5,
        )
        assert result["title"] == "Fix"


class TestJJIClientCapabilities:
    def test_capabilities(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/capabilities"
            return httpx.Response(
                200, json={"github_issues_enabled": True, "jira_issues_enabled": False}
            )

        client = _make_client(handler)
        result = client.capabilities()
        assert result["github_issues_enabled"] is True
        assert result["jira_issues_enabled"] is False


class TestJJIClientMentionableUsers:
    def test_get_mentionable_users(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/users/mentionable"
            return httpx.Response(200, json={"usernames": ["alice", "bob", "charlie"]})

        client = _make_client(handler)
        result = client.get_mentionable_users()
        assert result["usernames"] == ["alice", "bob", "charlie"]

    def test_get_mentionable_users_empty(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/users/mentionable"
            return httpx.Response(200, json={"usernames": []})

        client = _make_client(handler)
        result = client.get_mentionable_users()
        assert result["usernames"] == []


class TestJJIClientMentions:
    def test_get_mentions(self):
        payload = {
            "mentions": [{"id": 1, "comment": "@alice hi", "username": "bob"}],
            "total": 1,
            "unread_count": 1,
        }

        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/users/mentions"
            assert request.url.params["limit"] == "50"
            return httpx.Response(200, json=payload)

        client = _make_client(handler, username="alice")
        result = client.get_mentions(limit=50)
        assert result["total"] == 1
        assert result["mentions"][0]["comment"] == "@alice hi"

    def test_get_mentions_unread_only(self):
        def handler(request):
            assert request.url.params.get("unread_only") == "true"
            return httpx.Response(
                200, json={"mentions": [], "total": 0, "unread_count": 0}
            )

        client = _make_client(handler, username="alice")
        client.get_mentions(unread_only=True)

    def test_mark_mentions_read(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/users/mentions/read"
            import json

            body = json.loads(request.content)
            assert body["comment_ids"] == [10, 20]
            return httpx.Response(200, json={"ok": True})

        client = _make_client(handler, username="alice")
        result = client.mark_mentions_read([10, 20])
        assert result["ok"] is True

    def test_mark_all_mentions_read(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/users/mentions/read-all"
            return httpx.Response(200, json={"marked_read": 5})

        client = _make_client(handler, username="alice")
        result = client.mark_all_mentions_read()
        assert result["marked_read"] == 5


class TestJJIClientAiConfigs:
    def test_get_ai_configs(self):
        sample = [
            {"ai_provider": "claude", "ai_model": "opus-4"},
            {"ai_provider": "gemini", "ai_model": "2.5-pro"},
        ]

        def handler(request):
            assert request.method == "GET"
            assert "/ai-configs" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_ai_configs()
        assert len(result) == 2
        assert result[0]["ai_provider"] == "claude"

    def test_get_ai_configs_empty(self):
        def handler(request):
            return httpx.Response(200, json=[])

        client = _make_client(handler)
        result = client.get_ai_configs()
        assert result == []


class TestJJIClientPreviewWithAiConfig:
    def test_preview_github_issue_with_ai_config(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["ai_provider"] == "claude"
            assert body["ai_model"] == "opus-4"
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        result = client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            ai_provider="claude",
            ai_model="opus-4",
        )
        assert result["title"] == "Fix"

    def test_preview_jira_bug_with_ai_config(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["ai_provider"] == "gemini"
            assert body["ai_model"] == "2.5-pro"
            return httpx.Response(
                200,
                json={"title": "Bug", "body": "Desc", "similar_issues": []},
            )

        client = _make_client(handler)
        result = client.preview_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            ai_provider="gemini",
            ai_model="2.5-pro",
        )
        assert result["title"] == "Bug"

    def test_preview_github_issue_without_ai_config_omits_fields(self):
        def handler(request):
            body = json.loads(request.content)
            assert "ai_provider" not in body
            assert "ai_model" not in body
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
        )


class TestJJIClientIssueTokens:
    def test_preview_github_issue_with_tokens(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/preview-github-issue" in str(request.url)
            body = json.loads(request.content)
            assert body["github_token"] == "ghp_test123"  # noqa: S105
            assert "jira_token" not in body
            assert "jira_email" not in body
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        result = client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            github_token="ghp_test123",  # noqa: S106
        )
        assert result["title"] == "Fix"

    def test_create_github_issue_with_tokens(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/create-github-issue" in str(request.url)
            body = json.loads(request.content)
            assert body["github_token"] == "ghp_test123"  # noqa: S105
            assert "jira_token" not in body
            assert "jira_email" not in body
            return httpx.Response(
                201,
                json={
                    "url": "https://github.com/org/repo/issues/99",
                    "key": "",
                    "title": "Bug",
                    "comment_id": 1,
                },
            )

        client = _make_client(handler)
        result = client.create_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug",
            body="Details",
            github_token="ghp_test123",  # noqa: S106
        )
        assert result["url"] == "https://github.com/org/repo/issues/99"

    def test_preview_jira_bug_with_tokens(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/preview-jira-bug" in str(request.url)
            body = json.loads(request.content)
            assert "github_token" not in body
            assert body["jira_token"] == "jira-tok-test"  # noqa: S105
            assert body["jira_email"] == "test@example.com"
            return httpx.Response(
                200,
                json={"title": "Bug", "body": "Desc", "similar_issues": []},
            )

        client = _make_client(handler)
        result = client.preview_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            jira_token="jira-tok-test",  # noqa: S106
            jira_email="test@example.com",
        )
        assert result["title"] == "Bug"

    def test_create_jira_bug_with_tokens(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/create-jira-bug" in str(request.url)
            body = json.loads(request.content)
            assert "github_token" not in body
            assert body["jira_token"] == "jira-tok-test"  # noqa: S105
            assert body["jira_email"] == "test@example.com"
            return httpx.Response(
                201,
                json={
                    "url": "https://jira.example.com/browse/PROJ-1",
                    "key": "PROJ-1",
                    "title": "Bug",
                    "comment_id": 1,
                },
            )

        client = _make_client(handler)
        result = client.create_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug",
            body="Details",
            jira_token="jira-tok-test",  # noqa: S106
            jira_email="test@example.com",
        )
        assert result["key"] == "PROJ-1"

    def test_get_issue_prompt(self):
        def handler(request):
            assert request.method == "GET"
            assert "/results/job-1/issue-prompt" in str(request.url)
            return httpx.Response(200, json={"prompt": "Include product version"})

        client = _make_client(handler)
        result = client.get_issue_prompt(job_id="job-1")
        assert result["prompt"] == "Include product version"

    def test_preview_github_issue_with_issue_prompt(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["issue_prompt"] == "Include CNV version"
            return httpx.Response(
                200, json={"title": "T", "body": "B", "similar_issues": []}
            )

        client = _make_client(handler)
        result = client.preview_github_issue(
            job_id="job-1",
            test_name="test_one",
            issue_prompt="Include CNV version",
        )
        assert result["title"] == "T"

    def test_preview_jira_bug_with_issue_prompt(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["issue_prompt"] == "Include OCP version"
            return httpx.Response(
                200, json={"title": "T", "body": "B", "similar_issues": []}
            )

        client = _make_client(handler)
        result = client.preview_jira_bug(
            job_id="job-1",
            test_name="test_one",
            issue_prompt="Include OCP version",
        )
        assert result["title"] == "T"

    def test_preview_github_issue_without_tokens_omits_fields(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/preview-github-issue" in str(request.url)
            body = json.loads(request.content)
            assert "github_token" not in body
            assert "jira_token" not in body
            assert "jira_email" not in body
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
        )

    def test_create_jira_bug_includes_jira_security_level(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/create-jira-bug" in str(request.url)
            body = json.loads(request.content)
            assert body["jira_security_level"] == "Restricted"
            return httpx.Response(
                201,
                json={
                    "url": "https://jira.example.com/browse/PROJ-2",
                    "key": "PROJ-2",
                    "title": "Bug",
                    "comment_id": 1,
                },
            )

        client = _make_client(handler)
        result = client.create_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug",
            body="Details",
            jira_token="jira-tok-test",  # noqa: S106
            jira_email="test@example.com",
            jira_security_level="Restricted",
        )
        assert result["key"] == "PROJ-2"


class TestJJIClientCrossCredentialLeakage:
    """Verify that GitHub methods never include Jira credentials and vice versa."""

    def test_preview_github_issue_excludes_jira_credentials(self):
        def handler(request):
            body = json.loads(request.content)
            assert "jira_token" not in body, (
                "jira_token leaked into GitHub preview payload"
            )
            assert "jira_email" not in body, (
                "jira_email leaked into GitHub preview payload"
            )
            assert body["github_token"] == "ghp_test"  # noqa: S105
            return httpx.Response(
                200,
                json={"title": "Fix", "body": "Body", "similar_issues": []},
            )

        client = _make_client(handler)
        client.preview_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            github_token="ghp_test",  # noqa: S106
        )

    def test_create_github_issue_excludes_jira_credentials(self):
        def handler(request):
            body = json.loads(request.content)
            assert "jira_token" not in body, (
                "jira_token leaked into GitHub create payload"
            )
            assert "jira_email" not in body, (
                "jira_email leaked into GitHub create payload"
            )
            return httpx.Response(
                201,
                json={
                    "url": "https://github.com/org/repo/issues/1",
                    "key": "",
                    "title": "Bug",
                    "comment_id": 1,
                },
            )

        client = _make_client(handler)
        client.create_github_issue(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug",
            body="Details",
            github_token="ghp_test",  # noqa: S106
        )

    def test_preview_jira_bug_excludes_github_credentials(self):
        def handler(request):
            body = json.loads(request.content)
            assert "github_token" not in body, (
                "github_token leaked into Jira preview payload"
            )
            assert body["jira_token"] == "jira-tok"  # noqa: S105
            return httpx.Response(
                200,
                json={"title": "Bug", "body": "Desc", "similar_issues": []},
            )

        client = _make_client(handler)
        client.preview_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            jira_token="jira-tok",  # noqa: S106
        )

    def test_create_jira_bug_excludes_github_credentials(self):
        def handler(request):
            body = json.loads(request.content)
            assert "github_token" not in body, (
                "github_token leaked into Jira create payload"
            )
            return httpx.Response(
                201,
                json={
                    "url": "https://jira.example.com/browse/PROJ-1",
                    "key": "PROJ-1",
                    "title": "Bug",
                    "comment_id": 1,
                },
            )

        client = _make_client(handler)
        client.create_jira_bug(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            title="Bug",
            body="Details",
            jira_token="jira-tok",  # noqa: S106
        )


class TestJJIClientReview:
    def test_get_review_status(self):
        sample = {"total_failures": 5, "reviewed_count": 3, "comment_count": 7}

        def handler(request):
            assert request.method == "GET"
            assert "/results/job-1/review-status" in str(request.url)
            return httpx.Response(200, json=sample)

        client = _make_client(handler)
        result = client.get_review_status("job-1")
        assert result["total_failures"] == 5
        assert result["reviewed_count"] == 3
        assert result["comment_count"] == 7

    def test_set_reviewed(self):
        def handler(request):
            assert request.method == "PUT"
            assert "/results/job-1/reviewed" in str(request.url)
            body = json.loads(request.content)
            assert body["test_name"] == "tests.TestA.test_one"
            assert body["reviewed"] is True
            return httpx.Response(200, json={"status": "ok", "reviewed_by": "alice"})

        client = _make_client(handler, username="alice")
        result = client.set_reviewed(
            job_id="job-1", test_name="tests.TestA.test_one", reviewed=True
        )
        assert result["status"] == "ok"
        assert result["reviewed_by"] == "alice"

    def test_set_reviewed_with_child_job(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["test_name"] == "tests.TestA.test_one"
            assert body["reviewed"] is False
            assert body["child_job_name"] == "child-runner"
            assert body["child_build_number"] == 5
            return httpx.Response(200, json={"status": "ok", "reviewed_by": "bob"})

        client = _make_client(handler)
        result = client.set_reviewed(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            reviewed=False,
            child_job_name="child-runner",
            child_build_number=5,
        )
        assert result["status"] == "ok"

    def test_enrich_comments(self):
        def handler(request):
            assert request.method == "POST"
            assert "/results/job-1/enrich-comments" in str(request.url)
            return httpx.Response(200, json={"enriched": 3})

        client = _make_client(handler)
        result = client.enrich_comments("job-1")
        assert result["enriched"] == 3


class TestJJIClientExcludeJobId:
    def test_get_test_history_with_exclude(self):
        def handler(request):
            assert request.method == "GET"
            assert "exclude_job_id=job-99" in str(request.url)
            return httpx.Response(
                200, json={"test_name": "t", "failures": 0, "recent_runs": []}
            )

        client = _make_client(handler)
        client.get_test_history("t", exclude_job_id="job-99")

    def test_search_by_signature_with_exclude(self):
        def handler(request):
            assert request.method == "GET"
            assert "exclude_job_id=job-99" in str(request.url)
            return httpx.Response(
                200, json={"signature": "s", "total_occurrences": 0, "tests": []}
            )

        client = _make_client(handler)
        client.search_by_signature("s", exclude_job_id="job-99")

    def test_get_job_stats_with_exclude(self):
        def handler(request):
            assert request.method == "GET"
            assert "exclude_job_id=job-99" in str(request.url)
            return httpx.Response(
                200, json={"job_name": "j", "total_builds_analyzed": 0}
            )

        client = _make_client(handler)
        client.get_job_stats("j", exclude_job_id="job-99")


class TestJJIClientClassificationsParentJobName:
    def test_get_classifications_with_parent_job_name(self):
        def handler(request):
            assert request.method == "GET"
            assert "parent_job_name=parent-job" in str(request.url)
            return httpx.Response(200, json={"classifications": []})

        client = _make_client(handler)
        client.get_classifications(parent_job_name="parent-job")


class TestJJIClientVerifySSL:
    def test_verify_ssl_default_true(self):
        """Client should verify SSL by default."""
        with patch("jenkins_job_insight.cli.client.httpx.Client") as mock_httpx:
            JJIClient(server_url=BASE_URL)
            _, kwargs = mock_httpx.call_args
            assert kwargs.get("verify", True) is True

    def test_verify_ssl_false_passes_to_httpx(self):
        """Client should pass verify=False to httpx when verify_ssl=False."""
        with patch("jenkins_job_insight.cli.client.httpx.Client") as mock_httpx:
            JJIClient(server_url=BASE_URL, verify_ssl=False)
            _, kwargs = mock_httpx.call_args
            assert kwargs["verify"] is False

    def test_verify_ssl_true_passes_to_httpx(self):
        """Client should pass verify=True to httpx when verify_ssl=True."""
        with patch("jenkins_job_insight.cli.client.httpx.Client") as mock_httpx:
            JJIClient(server_url=BASE_URL, verify_ssl=True)
            _, kwargs = mock_httpx.call_args
            assert kwargs.get("verify", True) is True


def _parse_analyze_request(request):
    """Assert method/path and return parsed body for /analyze handlers."""
    assert request.method == "POST"
    assert request.url.path == "/analyze"
    return json.loads(request.content)


class TestJJIClientAnalyzeAdditionalRepos:
    def test_analyze_passes_additional_repos(self):
        """additional_repos is forwarded in the request body."""
        repos = [{"name": "infra", "url": "https://github.com/org/infra"}]

        def handler(request):
            body = _parse_analyze_request(request)
            assert body["additional_repos"] == repos
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze("my-job", 1, additional_repos=repos)
        assert result["status"] == "queued"


class TestJJIClientAnalyzeExtras:
    def test_analyze_with_ai_provider(self):
        def handler(request):
            body = _parse_analyze_request(request)
            assert body["ai_provider"] == "claude"
            assert body["ai_model"] == "opus"
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze(
            "my-job",
            1,
            ai_provider="claude",
            ai_model="opus",
        )
        assert result["status"] == "queued"

    def test_analyze_with_all_optional_fields(self):
        """All optional kwargs should be included in the POST body."""
        extras = {
            "jenkins_url": "https://jenkins.local",
            "jenkins_user": "admin",
            "jenkins_password": "s3cret",  # pragma: allowlist secret
            "jenkins_ssl_verify": False,
            "jenkins_timeout": 60,
            "jenkins_artifacts_max_size_mb": 50,
            "get_job_artifacts": True,
            "tests_repo_url": "https://github.com/org/tests",
            "jira_url": "https://jira.example.com",
            "jira_email": "user@example.com",
            "jira_api_token": "tok-123",  # noqa: S105
            "jira_pat": "pat-abc",  # noqa: S105
            "jira_project_key": "PROJ",
            "jira_ssl_verify": True,
            "jira_max_results": 25,
            "github_token": "ghp_abc123",  # noqa: S105
            "ai_cli_timeout": 10,
            "enable_jira": True,
            "raw_prompt": "extra instructions",
        }

        def handler(request):
            body = _parse_analyze_request(request)
            assert body["job_name"] == "my-job"
            assert body["build_number"] == 1
            for key, value in extras.items():
                assert body[key] == value, (
                    f"Expected {key}={value}, got {body.get(key)}"
                )
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze("my-job", 1, **extras)
        assert result["status"] == "queued"

    def test_analyze_with_wait_for_completion_fields(self):
        """Wait-for-completion fields should be included in the POST body."""

        def handler(request):
            body = _parse_analyze_request(request)
            assert body["wait_for_completion"] is False
            assert body["poll_interval_minutes"] == 5
            assert body["max_wait_minutes"] == 30
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze(
            "my-job",
            1,
            wait_for_completion=False,
            poll_interval_minutes=5,
            max_wait_minutes=30,
        )
        assert result["status"] == "queued"

    def test_analyze_with_peer_ai_configs(self):
        """peer_ai_configs kwargs should be included in the POST body."""

        def handler(request):
            body = _parse_analyze_request(request)
            assert body["peer_ai_configs"] == [
                {"ai_provider": "cursor", "ai_model": "gpt-5"},
                {"ai_provider": "gemini", "ai_model": "2.5-pro"},
            ]
            assert body["peer_analysis_max_rounds"] == 5
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze(
            "my-job",
            1,
            peer_ai_configs=[
                {"ai_provider": "cursor", "ai_model": "gpt-5"},
                {"ai_provider": "gemini", "ai_model": "2.5-pro"},
            ],
            peer_analysis_max_rounds=5,
        )
        assert result["status"] == "queued"

    def test_analyze_without_extras_sends_minimal_body(self):
        """Without extras, only job_name and build_number should be in the body."""

        def handler(request):
            body = _parse_analyze_request(request)
            assert body == {"job_name": "my-job", "build_number": 1}
            return httpx.Response(202, json={"status": "queued", "job_id": "x"})

        client = _make_client(handler)
        result = client.analyze("my-job", 1)
        assert result["status"] == "queued"


class TestJJIClientValidateToken:
    def test_validate_github_token(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/validate-token" in str(request.url)
            body = json.loads(request.content)
            assert body["token_type"] == "github"  # noqa: S105
            assert body["token"] == "ghp_test"  # noqa: S105
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "username": "testuser",
                    "message": "Authenticated as testuser",
                },
            )

        client = _make_client(handler)
        result = client.validate_token(token_type="github", token="ghp_test")  # noqa: S106
        assert result["valid"] is True

    def test_validate_jira_token_with_email(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/validate-token" in str(request.url)
            body = json.loads(request.content)
            assert body["token_type"] == "jira"  # noqa: S105
            assert body["token"] == "jira-tok"  # noqa: S105
            assert body["email"] == "user@example.com"
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "username": "User",
                    "message": "Authenticated as User",
                },
            )

        client = _make_client(handler)
        result = client.validate_token(
            token_type="jira",  # noqa: S106
            token="jira-tok",  # noqa: S106
            email="user@example.com",
        )
        assert result["valid"] is True

    def test_validate_token_without_email_omits_field(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/validate-token" in str(request.url)
            body = json.loads(request.content)
            assert "email" not in body
            assert body["token_type"] == "jira"  # noqa: S105
            assert body["token"] == "jira-tok"  # noqa: S105
            return httpx.Response(
                200, json={"valid": True, "username": "u", "message": "ok"}
            )

        client = _make_client(handler)
        client.validate_token(token_type="jira", token="jira-tok")  # noqa: S106


class TestJJIClientJiraProjects:
    def test_jira_projects(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/jira-projects" in str(request.url)
            return httpx.Response(200, json=[{"key": "PROJ", "name": "My Project"}])

        client = _make_client(handler)
        result = client.jira_projects()
        assert len(result) == 1
        assert result[0]["key"] == "PROJ"


class TestJJIClientJiraSecurityLevels:
    def test_jira_security_levels(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/jira-security-levels" in str(request.url)
            body = json.loads(request.content)
            assert body["project_key"] == "PROJ"
            return httpx.Response(
                200,
                json=[{"id": "10", "name": "Internal", "description": "Internal only"}],
            )

        client = _make_client(handler)
        result = client.jira_security_levels("PROJ")
        assert len(result) == 1
        assert result[0]["name"] == "Internal"

    def test_jira_security_levels_with_token(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["jira_token"] == "tok"  # noqa: S105
            assert body["jira_email"] == "u@e.com"
            return httpx.Response(200, json=[])

        client = _make_client(handler)
        result = client.jira_security_levels(
            "PROJ",
            jira_token="tok",  # noqa: S106
            jira_email="u@e.com",
        )
        assert result == []


class TestJJIClientReAnalyze:
    def test_re_analyze(self):
        response_data = {
            "status": "queued",
            "job_id": "new-reanalysis-1",
            "message": "Re-analysis job queued.",
        }

        def handler(request):
            assert request.method == "POST"
            assert "/re-analyze/old-job-1" in str(request.url)
            body = json.loads(request.content)
            assert body == {}
            return httpx.Response(202, json=response_data)

        client = _make_client(handler)
        result = client.re_analyze("old-job-1")
        assert result["status"] == "queued"
        assert result["job_id"] == "new-reanalysis-1"


class TestJJIClientPushReportPortal:
    def test_push_reportportal(self):
        response_data = {
            "pushed": 3,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        def handler(request):
            assert request.method == "POST"
            assert "/results/job-123/push-reportportal" in str(request.url)
            return httpx.Response(200, json=response_data)

        client = _make_client(handler)
        result = client.push_reportportal("job-123")
        assert result["pushed"] == 3
        assert result["launch_id"] == 42

    def test_push_reportportal_child_job(self):
        response_data = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 55,
        }

        def handler(request):
            assert request.method == "POST"
            assert "/results/job-123/push-reportportal" in str(request.url)
            assert request.url.params.get("child_job_name") == "my-child"
            assert request.url.params.get("child_build_number") == "42"
            return httpx.Response(200, json=response_data)

        client = _make_client(handler)
        result = client.push_reportportal(
            "job-123", child_job_name="my-child", child_build_number=42
        )
        assert result["pushed"] == 1

    def test_push_reportportal_error(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "Report Portal is disabled"})

        client = _make_client(handler)
        with pytest.raises(JJIError) as exc_info:
            client.push_reportportal("job-123")
        assert exc_info.value.status_code == 400


class TestJJIClientAuth:
    def test_login(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/auth/login"
            body = json.loads(request.content)
            assert body["username"] == "admin"
            assert body["api_key"] == "test-key"  # noqa: S105  # pragma: allowlist secret
            return httpx.Response(
                200,
                json={"username": "admin", "role": "admin", "is_admin": True},
            )

        client = _make_client(handler)
        result = client.login("admin", "test-key")
        assert result["username"] == "admin"
        assert result["is_admin"] is True

    def test_logout(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/auth/logout"
            return httpx.Response(200, json={"status": "logged_out"})

        client = _make_client(handler)
        result = client.logout()
        assert result["status"] == "logged_out"

    def test_auth_me(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/auth/me"
            return httpx.Response(
                200,
                json={"username": "admin", "role": "admin", "is_admin": True},
            )

        client = _make_client(handler)
        result = client.auth_me()
        assert result["username"] == "admin"

    def test_login_wrong_key(self):
        client = _make_client(
            lambda request: httpx.Response(401, json={"detail": "Invalid credentials"})
        )
        with pytest.raises(JJIError) as exc_info:
            client.login("admin", "wrong-key")
        assert exc_info.value.status_code == 401


class TestJJIClientAdminUsers:
    def test_admin_list_users(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/admin/users"
            return httpx.Response(
                200,
                json={"users": [{"username": "admin", "role": "admin"}]},
            )

        client = _make_client(handler)
        result = client.admin_list_users()
        assert "users" in result
        assert result["users"][0]["username"] == "admin"

    def test_admin_create_user(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/admin/users"
            body = json.loads(request.content)
            assert body["username"] == "newadmin"
            return httpx.Response(
                200,
                json={
                    "username": "newadmin",
                    "api_key": "not-a-real-key",  # pragma: allowlist secret
                    "role": "admin",
                },
            )

        client = _make_client(handler)
        result = client.admin_create_user("newadmin")
        assert result["username"] == "newadmin"
        assert "api_key" in result

    def test_admin_delete_user(self):
        def handler(request):
            assert request.method == "DELETE"
            assert "/api/admin/users/oldadmin" in str(request.url)
            return httpx.Response(200, json={"deleted": "oldadmin"})

        client = _make_client(handler)
        result = client.admin_delete_user("oldadmin")
        assert result["deleted"] == "oldadmin"

    def test_admin_rotate_key(self):
        def handler(request):
            assert request.method == "POST"
            assert "/api/admin/users/myuser/rotate-key" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "username": "myuser",
                    "new_api_key": "not-a-real-key",  # pragma: allowlist secret
                },
            )

        client = _make_client(handler)
        result = client.admin_rotate_key("myuser")
        assert result["username"] == "myuser"
        assert result["new_api_key"] == "not-a-real-key"  # pragma: allowlist secret

    def test_admin_change_role(self):
        def handler(request):
            assert request.url.path == "/api/admin/users/myuser/role"
            assert request.method == "PUT"
            body = json.loads(request.content)
            assert body["role"] == "admin"
            return httpx.Response(
                200,
                json={
                    "username": "myuser",
                    "role": "admin",
                    "api_key": "not-a-real-key",  # pragma: allowlist secret
                },
            )

        client = _make_client(handler, api_key="test-key")
        result = client.admin_change_role("myuser", "admin")
        assert result["role"] == "admin"
        assert result["api_key"] == "not-a-real-key"  # pragma: allowlist secret

    def test_admin_create_user_forbidden(self):
        client = _make_client(
            lambda request: httpx.Response(
                403, json={"detail": "Admin access required"}
            )
        )
        with pytest.raises(JJIError) as exc_info:
            client.admin_create_user("newadmin")
        assert exc_info.value.status_code == 403


class TestTokenUsage:
    def test_get_token_usage_no_params(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/admin/token-usage"
            return httpx.Response(
                200,
                json={"total_calls": 5, "total_cost_usd": 0.12, "breakdown": []},
            )

        client = _make_client(handler)
        result = client.get_token_usage()
        assert result["total_calls"] == 5

    def test_get_token_usage_with_filters(self):
        def handler(request):
            assert request.url.params["start_date"] == "2026-01-01"
            assert request.url.params["ai_provider"] == "claude"
            assert request.url.params["group_by"] == "model"
            # Params not provided should be stripped (None-filtering)
            assert "end_date" not in request.url.params
            return httpx.Response(
                200,
                json={"total_calls": 3, "breakdown": [{"group_key": "claude-sonnet"}]},
            )

        client = _make_client(handler)
        result = client.get_token_usage(
            start_date="2026-01-01", ai_provider="claude", group_by="model"
        )
        assert result["total_calls"] == 3
        assert len(result["breakdown"]) == 1

    def test_get_token_usage_summary(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/admin/token-usage/summary"
            return httpx.Response(
                200,
                json={"today": {"calls": 10}, "this_week": {"calls": 50}},
            )

        client = _make_client(handler)
        result = client.get_token_usage_summary()
        assert result["today"]["calls"] == 10

    def test_get_token_usage_for_job(self):
        def handler(request):
            assert request.method == "GET"
            assert "/api/admin/token-usage/job-123" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "job_id": "job-123",
                    "records": [{"call_type": "analysis", "input_tokens": 100}],
                },
            )

        client = _make_client(handler)
        result = client.get_token_usage_for_job("job-123")
        assert result["job_id"] == "job-123"
        assert len(result["records"]) == 1

    def test_get_token_usage_forbidden(self):
        client = _make_client(
            lambda request: httpx.Response(
                403, json={"detail": "Admin access required"}
            )
        )
        with pytest.raises(JJIError) as exc_info:
            client.get_token_usage()
        assert exc_info.value.status_code == 403


class TestAnalyzeCommentIntent:
    def test_analyze_comment_intent(self):
        expected = {"suggests_reviewed": True, "reason": "Bug filed"}

        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/analyze-comment-intent"
            return httpx.Response(200, json=expected)

        client = _make_client(handler)
        result = client.analyze_comment_intent(comment="Filed JIRA-123")
        assert result == expected

    def test_analyze_comment_intent_with_ai_config(self):
        def check_payload(request):
            assert request.method == "POST"
            assert request.url.path == "/api/analyze-comment-intent"
            body = json.loads(request.content)
            assert body["comment"] == "Filed JIRA-123"
            assert body["ai_provider"] == "claude"
            assert body["ai_model"] == "claude-sonnet-4-20250514"
            return httpx.Response(
                200, json={"suggests_reviewed": True, "reason": "Bug filed"}
            )

        client = _make_client(check_payload)
        result = client.analyze_comment_intent(
            comment="Filed JIRA-123",
            ai_provider="claude",
            ai_model="claude-sonnet-4-20250514",
        )
        assert result["suggests_reviewed"] is True

    def test_analyze_comment_intent_without_ai_config(self):
        def check_payload(request):
            assert request.method == "POST"
            assert request.url.path == "/api/analyze-comment-intent"
            body = json.loads(request.content)
            assert "ai_provider" not in body
            assert "ai_model" not in body
            return httpx.Response(200, json={"suggests_reviewed": False, "reason": ""})

        client = _make_client(check_payload)
        client.analyze_comment_intent(comment="test")

    def test_analyze_comment_intent_with_job_id(self):
        def check_payload(request):
            assert request.method == "POST"
            assert request.url.path == "/api/analyze-comment-intent"
            body = json.loads(request.content)
            assert body["comment"] == "Fixed in PR #42"
            assert body["job_id"] == "job-abc-123"
            return httpx.Response(
                200, json={"suggests_reviewed": True, "reason": "PR reference"}
            )

        client = _make_client(check_payload)
        result = client.analyze_comment_intent(
            comment="Fixed in PR #42", job_id="job-abc-123"
        )
        assert result["suggests_reviewed"] is True

    def test_analyze_comment_intent_failure(self):
        client = _make_client(
            lambda request: httpx.Response(500, json={"detail": "Internal error"})
        )
        with pytest.raises(JJIError) as exc_info:
            client.analyze_comment_intent(comment="test")
        assert exc_info.value.status_code == 500


class TestJJIClientAiModels:
    def test_list_ai_models_empty_string_provider_omits_query_param(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/ai-models"
            assert request.url.params.get("provider") is None
            return httpx.Response(200, json={"providers": {}})

        client = _make_client(handler)
        result = client.list_ai_models(provider="")
        assert result == {"providers": {}}

    def test_list_ai_models_no_provider(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/ai-models"
            # No provider param
            assert request.url.params.get("provider") is None
            return httpx.Response(
                200,
                json={
                    "providers": {
                        "claude": [
                            {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}
                        ],
                        "cursor": [],
                    }
                },
            )

        client = _make_client(handler)
        result = client.list_ai_models()
        assert "providers" in result
        assert len(result["providers"]["claude"]) == 1

    def test_list_ai_models_with_provider(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/ai-models"
            assert request.url.params["provider"] == "claude"
            return httpx.Response(
                200,
                json={
                    "provider": "claude",
                    "models": [{"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}],
                },
            )

        client = _make_client(handler)
        result = client.list_ai_models(provider="claude")
        assert result["provider"] == "claude"
        assert len(result["models"]) == 1

    def test_list_ai_models_empty(self):
        def handler(request):
            return httpx.Response(200, json={"provider": "cursor", "models": []})

        client = _make_client(handler)
        result = client.list_ai_models(provider="cursor")
        assert result["models"] == []


class TestJJIClientApiKeyHeader:
    def test_api_key_sent_as_bearer_header(self):
        def check_header(request):
            auth = request.headers.get("authorization", "")
            assert auth == "Bearer test-api-key"  # noqa: S105
            return httpx.Response(
                200,
                json={"username": "admin", "role": "admin", "is_admin": True},
            )

        client = _make_client(check_header, api_key="test-api-key")  # noqa: S106
        client.auth_me()

    def test_no_api_key_no_auth_header(self):
        def check_no_auth(request):
            auth = request.headers.get("authorization", "")
            assert auth == ""
            return httpx.Response(200, json={"status": "ok"})

        client = _make_client(check_no_auth)
        client.health()
