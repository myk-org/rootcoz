"""Tests for jji CLI commands using typer test runner."""

import json
import os
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import click
import pytest
import typer.main
from typer.testing import CliRunner

from jenkins_job_insight.cli.client import JJIError
from jenkins_job_insight.cli.config import ServerConfig
from jenkins_job_insight.cli.main import app

runner = CliRunner()


def _extract_envvar_names(command_name: str) -> tuple[str, ...]:
    """Extract envvar names bound to a typer command's options.

    Derives the set directly from the Click command object so it stays
    in sync with the analyze command definition automatically.
    """
    # Resolve the underlying Click command via the typer-created Click Group
    click_group: click.Group = typer.main.get_command(app)  # type: ignore[attr-defined]
    cmd = click_group.commands.get(command_name)
    if not cmd:
        raise AssertionError(
            f"Command {command_name!r} not found in {click_group.name!r}; "
            f"available: {sorted(click_group.commands)}"
        )
    names: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option) and param.envvar:
            names.extend(
                param.envvar if isinstance(param.envvar, list) else [param.envvar]
            )
    return tuple(sorted(names))


# Environment variables that the analyze CLI options bind to via envvar=.
# Used by tests that need a clean environment without inherited CI/local values.
# Derived from the analyze command definition to avoid hard-coded duplication.
_ANALYZE_ENV_VARS = _extract_envvar_names("analyze")


def _env_without_analyze_bindings() -> dict[str, str]:
    """Return a copy of os.environ without analyze-related env vars."""
    return {k: v for k, v in os.environ.items() if k not in _ANALYZE_ENV_VARS}


_TEST_SERVER = "http://test-server:8000"

# Fake credential constants used throughout tests.
_FAKE_JENKINS_PASSWORD = "cfg-jenkins-pw"  # noqa: S105  # pragma: allowlist secret
_FAKE_JIRA_API_TOKEN = "cfg-jira-tok"  # noqa: S105
_FAKE_JIRA_PAT = "cfg-jira-pat"  # noqa: S105
_FAKE_GITHUB_TOKEN = "ghp_cfg_token"  # noqa: S105
_FAKE_GITHUB_CLI_TOKEN = "ghp_tok"  # noqa: S105
_FAKE_GITHUB_CLI_OVERRIDE = "ghp_cli_override"  # noqa: S105  # pragma: allowlist secret


@pytest.fixture
def mock_client():
    """Provide a mocked JJIClient for all CLI tests.

    Sets JJI_SERVER so the main_callback does not exit early,
    patches _get_client so no real HTTP calls are made,
    and stubs get_server_config to prevent local config.toml from
    injecting unexpected defaults.
    """
    with (
        patch.dict(
            os.environ,
            {
                **{k: v for k, v in os.environ.items() if not k.startswith("JJI_")},
                "JJI_SERVER": _TEST_SERVER,
            },
            clear=True,
        ),
        patch(
            "jenkins_job_insight.cli.main.get_server_config",
            return_value=ServerConfig(url=_TEST_SERVER),
        ),
        patch("jenkins_job_insight.cli.main._get_client") as mock_get,
    ):
        client = MagicMock()
        mock_get.return_value = client
        yield client


class TestHealthCommand:
    def test_health(self, mock_client):
        mock_client.health.return_value = {"status": "healthy"}
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 0
        assert "healthy" in result.output

    def test_health_json(self, mock_client):
        mock_client.health.return_value = {"status": "healthy"}
        result = runner.invoke(app, ["--json", "health"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["status"] == "healthy"


class TestResultsCommands:
    def test_results_list(self, mock_client):
        mock_client.list_results.return_value = [
            {
                "job_id": "abc-123",
                "status": "completed",
                "jenkins_url": "https://jenkins.example.com/job/test/1/",
                "created_at": "2026-03-18",
            },
        ]
        result = runner.invoke(app, ["results", "list"])
        assert result.exit_code == 0
        assert "abc-123" in result.output

    def test_results_show(self, mock_client):
        mock_client.get_result.return_value = {
            "job_id": "abc-123",
            "status": "completed",
            "result": {"summary": "1 failure analyzed"},
        }
        result = runner.invoke(app, ["results", "show", "abc-123"])
        assert result.exit_code == 0
        assert "abc-123" in result.output

    def test_results_delete(self, mock_client):
        mock_client.delete_job.return_value = {"status": "deleted", "job_id": "abc-123"}
        result = runner.invoke(app, ["results", "delete", "abc-123"])
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_results_delete_bulk(self, mock_client):
        mock_client.delete_jobs_bulk.return_value = {
            "deleted": ["abc", "def"],
            "failed": [],
            "total": 2,
        }
        result = runner.invoke(app, ["results", "delete", "abc", "def"])
        assert result.exit_code == 0
        assert "Deleted 2 of 2 jobs" in result.output
        mock_client.delete_jobs_bulk.assert_called_once_with(["abc", "def"])

    def test_results_delete_bulk_with_failures(self, mock_client):
        mock_client.delete_jobs_bulk.return_value = {
            "deleted": ["abc"],
            "failed": [{"job_id": "def", "reason": "not found"}],
            "total": 2,
        }
        result = runner.invoke(app, ["results", "delete", "abc", "def"])
        assert result.exit_code == 0
        assert "Deleted 1 of 2 jobs" in result.output
        assert "Failed: def" in result.output

    def test_results_delete_all(self, mock_client):
        mock_client.dashboard.return_value = [
            {"job_id": "a"},
            {"job_id": "b"},
            {"job_id": "c"},
        ]
        mock_client.delete_jobs_bulk.return_value = {
            "deleted": ["a", "b", "c"],
            "failed": [],
            "total": 3,
        }
        result = runner.invoke(app, ["results", "delete", "--all", "--confirm"])
        assert result.exit_code == 0
        assert "Deleted 3 of 3 jobs" in result.output

    def test_results_delete_all_empty(self, mock_client):
        mock_client.dashboard.return_value = []
        result = runner.invoke(app, ["results", "delete", "--all", "--confirm"])
        assert result.exit_code == 0
        assert "No jobs to delete" in result.output


class TestReviewStatusCommand:
    def test_review_status(self, mock_client):
        mock_client.get_review_status.return_value = {
            "total_failures": 5,
            "reviewed_count": 3,
            "comment_count": 2,
        }
        result = runner.invoke(app, ["results", "review-status", "job-1"])
        assert result.exit_code == 0
        mock_client.get_review_status.assert_called_once_with("job-1")

    def test_review_status_json(self, mock_client):
        mock_client.get_review_status.return_value = {
            "total_failures": 5,
            "reviewed_count": 3,
            "comment_count": 2,
        }
        result = runner.invoke(app, ["--json", "results", "review-status", "job-1"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["total_failures"] == 5


class TestSetReviewedCommand:
    def test_set_reviewed(self, mock_client):
        mock_client.set_reviewed.return_value = {
            "status": "ok",
            "reviewed_by": "alice",
        }
        result = runner.invoke(
            app,
            [
                "results",
                "set-reviewed",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--reviewed",
            ],
        )
        assert result.exit_code == 0
        assert "reviewed" in result.output.lower()
        mock_client.set_reviewed.assert_called_once_with(
            job_id="job-1",
            test_name="tests.TestA.test_one",
            reviewed=True,
            child_job_name="",
            child_build_number=0,
        )

    def test_set_not_reviewed(self, mock_client):
        mock_client.set_reviewed.return_value = {
            "status": "ok",
            "reviewed_by": "alice",
        }
        result = runner.invoke(
            app,
            [
                "results",
                "set-reviewed",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--not-reviewed",
            ],
        )
        assert result.exit_code == 0
        assert "not reviewed" in result.output.lower()
        kwargs = mock_client.set_reviewed.call_args[1]
        assert kwargs["reviewed"] is False

    def test_set_reviewed_json(self, mock_client):
        mock_client.set_reviewed.return_value = {
            "status": "ok",
            "reviewed_by": "alice",
        }
        result = runner.invoke(
            app,
            [
                "--json",
                "results",
                "set-reviewed",
                "job-1",
                "--test",
                "test_foo",
                "--reviewed",
            ],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["status"] == "ok"

    def test_set_reviewed_with_child(self, mock_client):
        mock_client.set_reviewed.return_value = {
            "status": "ok",
            "reviewed_by": "bob",
        }
        result = runner.invoke(
            app,
            [
                "results",
                "set-reviewed",
                "job-1",
                "--test",
                "test_foo",
                "--reviewed",
                "--child-job",
                "child-runner",
                "--child-build",
                "5",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.set_reviewed.call_args[1]
        assert kwargs["child_job_name"] == "child-runner"
        assert kwargs["child_build_number"] == 5


class TestEnrichCommentsCommand:
    def test_enrich_comments(self, mock_client):
        mock_client.enrich_comments.return_value = {"enriched": 3}
        result = runner.invoke(app, ["results", "enrich-comments", "job-1"])
        assert result.exit_code == 0
        assert "3" in result.output
        mock_client.enrich_comments.assert_called_once_with("job-1")

    def test_enrich_comments_json(self, mock_client):
        mock_client.enrich_comments.return_value = {"enriched": 3}
        result = runner.invoke(app, ["--json", "results", "enrich-comments", "job-1"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["enriched"] == 3


class TestDashboardCommand:
    def test_dashboard_default(self, mock_client):
        mock_client.dashboard.return_value = [
            {
                "job_id": "abc-123",
                "job_name": "test-job",
                "status": "completed",
                "failure_count": 5,
                "reviewed_count": 3,
                "comment_count": 2,
                "created_at": "2024-01-15T10:00:00",
            }
        ]
        result = runner.invoke(app, ["results", "dashboard"])
        assert result.exit_code == 0
        assert "test-job" in result.output

    def test_dashboard_json(self, mock_client):
        mock_client.dashboard.return_value = []
        result = runner.invoke(app, ["results", "dashboard", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed == []
        mock_client.dashboard.assert_called_once()

    def test_dashboard_called_without_args(self, mock_client):
        mock_client.dashboard.return_value = []
        result = runner.invoke(app, ["results", "dashboard"])
        assert result.exit_code == 0
        mock_client.dashboard.assert_called_once_with()


class TestAnalyzeCommand:
    def test_analyze_async(self, mock_client):
        mock_client.analyze.return_value = {
            "status": "queued",
            "job_id": "new-1",
            "message": "Analysis job queued.",
        }
        result = runner.invoke(
            app, ["analyze", "--job-name", "my-job", "--build-number", "42"]
        )
        assert result.exit_code == 0
        assert "queued" in result.output.lower() or "new-1" in result.output


class TestStatusCommand:
    def test_status(self, mock_client):
        mock_client.get_result.return_value = {
            "job_id": "abc-123",
            "status": "running",
        }
        result = runner.invoke(app, ["status", "abc-123"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()


class TestHistoryCommands:
    def test_history_test(self, mock_client):
        mock_client.get_test_history.return_value = {
            "test_name": "tests.TestA.test_one",
            "failures": 3,
            "failure_rate": 0.6,
            "last_classification": "PRODUCT BUG",
            "recent_runs": [],
            "comments": [],
        }
        result = runner.invoke(app, ["history", "test", "tests.TestA.test_one"])
        assert result.exit_code == 0
        assert "tests.TestA.test_one" in result.output

    def test_history_search(self, mock_client):
        mock_client.search_by_signature.return_value = {
            "signature": "sig-abc",
            "total_occurrences": 5,
            "unique_tests": 2,
            "tests": [{"test_name": "tests.TestA.test_one", "occurrences": 3}],
        }
        result = runner.invoke(app, ["history", "search", "--signature", "sig-abc"])
        assert result.exit_code == 0
        assert "sig-abc" in result.output or "tests.TestA" in result.output

    def test_history_stats(self, mock_client):
        mock_client.get_job_stats.return_value = {
            "job_name": "ocp-e2e",
            "total_builds_analyzed": 10,
            "builds_with_failures": 3,
        }
        result = runner.invoke(app, ["history", "stats", "ocp-e2e"])
        assert result.exit_code == 0
        assert "ocp-e2e" in result.output

    def test_history_failures(self, mock_client):
        mock_client.get_all_failures.return_value = {
            "failures": [
                {
                    "test_name": "tests.TestA.test_one",
                    "classification": "PRODUCT BUG",
                    "job_name": "ocp-e2e",
                },
            ],
            "total": 1,
            "limit": 50,
            "offset": 0,
        }
        result = runner.invoke(app, ["history", "failures"])
        assert result.exit_code == 0
        assert "tests.TestA" in result.output


class TestClassifyCommand:
    def test_classify(self, mock_client):
        mock_client.classify_test.return_value = {"id": 1}
        result = runner.invoke(
            app,
            [
                "classify",
                "tests.TestA.test_one",
                "--type",
                "FLAKY",
                "--reason",
                "intermittent DNS",
                "--job-id",
                "job-123",
            ],
        )
        assert result.exit_code == 0
        assert "1" in result.output or "classified" in result.output.lower()


class TestClassificationsCommand:
    def test_classifications_list(self, mock_client):
        mock_client.get_classifications.return_value = {
            "classifications": [
                {
                    "test_name": "tests.TestA.test_one",
                    "classification": "FLAKY",
                    "reason": "DNS",
                },
            ],
        }
        result = runner.invoke(app, ["classifications", "list"])
        assert result.exit_code == 0
        assert "FLAKY" in result.output


class TestCommentsCommands:
    def test_comments_list(self, mock_client):
        mock_client.get_comments.return_value = {
            "comments": [
                {
                    "id": 1,
                    "test_name": "tests.TestA.test_one",
                    "comment": "Fixed in PR #42",
                    "username": "alice",
                    "created_at": "2026-03-18",
                },
            ],
            "reviews": {},
        }
        result = runner.invoke(app, ["comments", "list", "job-1"])
        assert result.exit_code == 0
        assert "Fixed in PR #42" in result.output or "tests.TestA" in result.output

    def test_comments_add(self, mock_client):
        mock_client.add_comment.return_value = {"id": 42}
        result = runner.invoke(
            app,
            [
                "comments",
                "add",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "-m",
                "Opened JIRA-123",
            ],
        )
        assert result.exit_code == 0
        assert "42" in result.output or "added" in result.output.lower()

    def test_comments_delete(self, mock_client):
        mock_client.delete_comment.return_value = {"status": "deleted"}
        result = runner.invoke(app, ["comments", "delete", "job-1", "42"])
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()


class TestServerFlag:
    def test_custom_server(self):
        with (
            patch("jenkins_job_insight.cli.main._get_client") as mock_get,
            patch("jenkins_job_insight.cli.main._state", {}) as mock_state,
        ):
            client = MagicMock()
            client.health.return_value = {"status": "healthy"}
            mock_get.return_value = client
            result = runner.invoke(app, ["--server", "http://custom:9000", "health"])
            assert result.exit_code == 0
            mock_get.assert_called_once()
            # Verify the server URL was stored in state by the callback
            assert mock_state.get("server_url") == "http://custom:9000"

    def test_missing_server(self):
        """CLI exits with error when no server is configured."""
        env = {k: v for k, v in os.environ.items() if k != "JJI_SERVER"}
        with (
            patch("jenkins_job_insight.cli.main._state", {}),
            patch.dict(os.environ, env, clear=True),
            patch("jenkins_job_insight.cli.main.get_server_config", return_value=None),
        ):
            result = runner.invoke(app, ["health"])
            assert result.exit_code == 1
            assert "No server specified" in result.output


class TestErrorHandling:
    def test_connection_error(self, mock_client):
        mock_client.health.side_effect = JJIError(
            status_code=0, detail="Connection refused"
        )
        result = runner.invoke(app, ["health"])
        assert result.exit_code != 0
        assert "Connection" in result.output or "Error" in result.output

    def test_http_error(self, mock_client):
        mock_client.get_result.side_effect = JJIError(
            status_code=404, detail="Job not found"
        )
        result = runner.invoke(app, ["results", "show", "nonexistent"])
        assert result.exit_code != 0
        assert "404" in result.output or "not found" in result.output.lower()

    def test_401_error_hints_about_api_key_flag(self, mock_client):
        """_handle_error should hint about --api-key when server returns 401."""
        mock_client.delete_job.side_effect = JJIError(
            status_code=401, detail="Please register a username first"
        )
        result = runner.invoke(app, ["results", "delete", "job-123"])
        assert result.exit_code == 1
        assert "--api-key" in result.output
        assert "JJI_API_KEY" in result.output

    def test_403_error_hints_about_admin_access(self, mock_client):
        """_handle_error should hint about admin access when server returns 403."""
        mock_client.delete_job.side_effect = JJIError(
            status_code=403, detail="Admin access required"
        )
        result = runner.invoke(app, ["results", "delete", "job-123"])
        assert result.exit_code == 1
        assert "admin access" in result.output.lower()
        assert "--api-key" in result.output

    def test_403_allow_list_hints_about_allow_list(self, mock_client):
        """_handle_error should hint about allow list when detail mentions it."""
        mock_client.add_comment.side_effect = JJIError(
            status_code=403,
            detail="User not allowed. Contact an administrator to be added to the allow list.",
        )
        result = runner.invoke(
            app,
            ["comments", "add", "job-1", "--test", "test_foo", "-m", "my comment"],
        )
        assert result.exit_code == 1
        assert "allow list" in result.output.lower()
        # Should NOT hint about --api-key for allow list errors
        assert "--api-key" not in result.output


class TestNullFieldHandling:
    def test_history_test_with_null_failure_rate(self, mock_client):
        """history test should handle None failure_rate without crashing."""
        mock_client.get_test_history.return_value = {
            "test_name": "tests.TestFoo.test_bar",
            "total_runs": 10,
            "failures": 3,
            "passes": None,
            "failure_rate": None,
            "first_seen": "2026-03-01",
            "last_seen": "2026-03-17",
            "last_classification": "PRODUCT BUG",
            "classifications": {"PRODUCT BUG": 3},
            "recent_runs": [],
            "consecutive_failures": 3,
            "note": "estimated",
        }
        result = runner.invoke(app, ["history", "test", "tests.TestFoo.test_bar"])
        assert result.exit_code == 0
        assert "test_bar" in result.output or "TestFoo" in result.output

    def test_history_stats_with_null_failure_rate(self, mock_client):
        """history stats should handle None overall_failure_rate without crashing."""
        mock_client.get_job_stats.return_value = {
            "job_name": "ocp-e2e",
            "total_builds_analyzed": 0,
            "builds_with_failures": 0,
            "overall_failure_rate": None,
            "most_common_failures": [],
        }
        result = runner.invoke(app, ["history", "stats", "ocp-e2e"])
        assert result.exit_code == 0
        assert "ocp-e2e" in result.output


class TestClassifyWithChildContext:
    def test_classify_with_child_context(self, mock_client):
        """classify should accept --child-job and --child-build."""
        mock_client.classify_test.return_value = {"id": 1}
        result = runner.invoke(
            app,
            [
                "classify",
                "test_foo",
                "--type",
                "REGRESSION",
                "--reason",
                "test",
                "--job-id",
                "job-1",
                "--child-job",
                "child-runner",
                "--child-build",
                "14",
            ],
        )
        assert result.exit_code == 0
        mock_client.classify_test.assert_called_once()
        kwargs = mock_client.classify_test.call_args.kwargs
        assert kwargs["child_build_number"] == 14


class TestJsonOnMutationCommands:
    def test_delete_result_json_mode(self, mock_client):
        """results delete --json should print raw API response."""
        mock_client.delete_job.return_value = {"status": "deleted", "job_id": "job-1"}
        result = runner.invoke(app, ["--json", "results", "delete", "job-1"])
        assert result.exit_code == 0
        assert '"status"' in result.output
        assert '"deleted"' in result.output

    def test_classify_json_mode(self, mock_client):
        """classify --json should print raw API response."""
        mock_client.classify_test.return_value = {"id": 42}
        result = runner.invoke(
            app,
            [
                "--json",
                "classify",
                "test_foo",
                "--type",
                "REGRESSION",
                "--job-id",
                "j1",
            ],
        )
        assert result.exit_code == 0
        assert '"id"' in result.output

    def test_comments_add_json_mode(self, mock_client):
        """comments add --json should print raw API response."""
        mock_client.add_comment.return_value = {"id": 10}
        result = runner.invoke(
            app,
            ["--json", "comments", "add", "job-1", "--test", "test_foo", "-m", "msg"],
        )
        assert result.exit_code == 0
        assert '"id"' in result.output


class TestAnalyzeFlags:
    def test_analyze_with_provider_and_model(self, mock_client):
        """analyze should accept --provider and --model flags."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "27",
                "--provider",
                "claude",
                "--model",
                "opus-4",
                "--jira",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["ai_provider"] == "claude"
        assert kwargs["ai_model"] == "opus-4"


class TestClassifyForwardsChildJob:
    def test_classify_forwards_child_job_name(self, mock_client):
        """classify should forward --child-job as job_name to the client."""
        mock_client.classify_test.return_value = {"id": 1}
        result = runner.invoke(
            app,
            [
                "classify",
                "test_foo",
                "--type",
                "REGRESSION",
                "--job-id",
                "j1",
                "--child-job",
                "child-runner",
                "--child-build",
                "14",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.classify_test.call_args[1]
        assert kwargs["job_name"] == "child-runner"  # NOT empty string
        assert kwargs["child_build_number"] == 14


class TestAnalyzeJiraField:
    def test_analyze_jira_flag_correct_field(self, mock_client):
        """--jira should send enable_jira=True, not jira_enabled=True."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app, ["analyze", "--job-name", "my-job", "--build-number", "27", "--jira"]
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "enable_jira" in kwargs
        assert kwargs["enable_jira"] is True

    def test_analyze_no_jira_flag_sends_false(self, mock_client):
        """--no-jira should send enable_jira=False."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "27", "--no-jira"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["enable_jira"] is False

    def test_analyze_jira_omitted_not_in_extras(self, mock_client):
        """When neither --jira nor --no-jira is given, enable_jira is not sent."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "27"],
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "enable_jira" not in kwargs


class TestHistoryExcludeJobId:
    def test_history_test_exclude_job_id(self, mock_client):
        mock_client.get_test_history.return_value = {
            "test_name": "t",
            "failures": 0,
            "failure_rate": None,
            "last_classification": None,
            "recent_runs": [],
            "comments": [],
        }
        result = runner.invoke(
            app,
            ["history", "test", "t", "--exclude-job-id", "job-99"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.get_test_history.call_args[1]
        assert kwargs["exclude_job_id"] == "job-99"

    def test_history_search_exclude_job_id(self, mock_client):
        mock_client.search_by_signature.return_value = {
            "signature": "s",
            "total_occurrences": 0,
            "unique_tests": 0,
            "tests": [],
        }
        result = runner.invoke(
            app,
            ["history", "search", "--signature", "s", "--exclude-job-id", "job-99"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.search_by_signature.call_args[1]
        assert kwargs["exclude_job_id"] == "job-99"

    def test_history_stats_exclude_job_id(self, mock_client):
        mock_client.get_job_stats.return_value = {
            "job_name": "j",
            "total_builds_analyzed": 0,
            "builds_with_failures": 0,
        }
        result = runner.invoke(
            app,
            ["history", "stats", "j", "--exclude-job-id", "job-99"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.get_job_stats.call_args[1]
        assert kwargs["exclude_job_id"] == "job-99"


class TestClassificationsParentJobName:
    def test_classifications_list_parent_job_name(self, mock_client):
        mock_client.get_classifications.return_value = {"classifications": []}
        result = runner.invoke(
            app,
            ["classifications", "list", "--parent-job-name", "parent-job"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.get_classifications.call_args[1]
        assert kwargs["parent_job_name"] == "parent-job"


class TestAnalyzeAllOptions:
    """Verify that all AnalyzeRequest fields are forwarded via CLI options."""

    _ANALYZE_RESPONSE = MappingProxyType({"status": "queued", "job_id": "j1"})

    @pytest.mark.parametrize(
        "cli_flag,cli_value,body_key,expected_value",
        [
            (
                "--jenkins-url",
                "https://jenkins.local",
                "jenkins_url",
                "https://jenkins.local",
            ),
            ("--jenkins-user", "admin", "jenkins_user", "admin"),
            (
                "--jenkins-password",
                _FAKE_JENKINS_PASSWORD,
                "jenkins_password",
                _FAKE_JENKINS_PASSWORD,
            ),
            (
                "--tests-repo-url",
                "https://github.com/org/tests",
                "tests_repo_url",
                "https://github.com/org/tests",
            ),
            (
                "--jira-url",
                "https://jira.example.com",
                "jira_url",
                "https://jira.example.com",
            ),
            ("--jira-email", "user@example.com", "jira_email", "user@example.com"),
            (
                "--jira-api-token",
                _FAKE_JIRA_API_TOKEN,
                "jira_api_token",
                _FAKE_JIRA_API_TOKEN,
            ),
            ("--jira-pat", _FAKE_JIRA_PAT, "jira_pat", _FAKE_JIRA_PAT),
            ("--jira-project-key", "PROJ", "jira_project_key", "PROJ"),
            (
                "--github-token",
                _FAKE_GITHUB_CLI_TOKEN,
                "github_token",
                _FAKE_GITHUB_CLI_TOKEN,
            ),
            ("--raw-prompt", "extra instructions", "raw_prompt", "extra instructions"),
        ],
    )
    def test_string_options(
        self, mock_client, cli_flag, cli_value, body_key, expected_value
    ):
        """String options should be forwarded to client.analyze as kwargs."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                cli_flag,
                cli_value,
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs[body_key] == expected_value

    @pytest.mark.parametrize(
        "cli_flag,cli_value,body_key,expected_value",
        [
            ("--jira-max-results", "25", "jira_max_results", 25),
            ("--ai-cli-timeout", "10", "ai_cli_timeout", 10),
            (
                "--jenkins-artifacts-max-size-mb",
                "50",
                "jenkins_artifacts_max_size_mb",
                50,
            ),
            ("--jenkins-timeout", "60", "jenkins_timeout", 60),
        ],
    )
    def test_int_options(
        self, mock_client, cli_flag, cli_value, body_key, expected_value
    ):
        """Integer options should be forwarded to client.analyze as kwargs."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                cli_flag,
                cli_value,
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs[body_key] == expected_value

    @pytest.mark.parametrize(
        "cli_flags,body_key,expected_value",
        [
            (["--jenkins-ssl-verify"], "jenkins_ssl_verify", True),
            (["--no-jenkins-ssl-verify"], "jenkins_ssl_verify", False),
            (["--jira-ssl-verify"], "jira_ssl_verify", True),
            (["--no-jira-ssl-verify"], "jira_ssl_verify", False),
            (["--get-job-artifacts"], "get_job_artifacts", True),
            (["--no-get-job-artifacts"], "get_job_artifacts", False),
        ],
    )
    def test_bool_options(self, mock_client, cli_flags, body_key, expected_value):
        """Boolean flag options should be forwarded to client.analyze as kwargs."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app, ["analyze", "--job-name", "my-job", "--build-number", "1", *cli_flags]
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs[body_key] == expected_value

    def test_max_concurrent_option(self, mock_client):
        """--max-concurrent should be forwarded as max_concurrent_ai_calls."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--max-concurrent",
                "5",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["max_concurrent_ai_calls"] == 5

    def test_max_concurrent_zero_not_forwarded(self, mock_client):
        """--max-concurrent 0 (default) should not forward the field."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "1"],
        )
        assert result.exit_code == 0, result.output
        kwargs = mock_client.analyze.call_args[1]
        assert "max_concurrent_ai_calls" not in kwargs

    def test_no_optional_fields_when_not_provided(self, mock_client):
        """When no optional flags are given and no env vars set, extras should be empty."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app, ["analyze", "--job-name", "my-job", "--build-number", "1"]
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        # Only job_name and build_number should be present as required options.
        assert "jenkins_url" not in kwargs
        assert "jira_url" not in kwargs
        assert "github_token" not in kwargs
        assert "jenkins_ssl_verify" not in kwargs

    def test_multiple_options_combined(self, mock_client):
        """Multiple options should all be forwarded together."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--provider",
                "gemini",
                "--jenkins-url",
                "https://jenkins.local",
                "--jira-url",
                "https://jira.local",
                "--jira-max-results",
                "50",
                "--no-jenkins-ssl-verify",
                "--github-token",
                _FAKE_GITHUB_CLI_TOKEN,
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["ai_provider"] == "gemini"
        assert kwargs["jenkins_url"] == "https://jenkins.local"
        assert kwargs["jira_url"] == "https://jira.local"
        assert kwargs["jira_max_results"] == 50
        assert kwargs["jenkins_ssl_verify"] is False
        assert kwargs["github_token"] == _FAKE_GITHUB_CLI_TOKEN


class TestCapabilitiesCommand:
    def test_capabilities(self, mock_client):
        mock_client.capabilities.return_value = {
            "github_issues_enabled": True,
            "jira_issues_enabled": False,
        }
        result = runner.invoke(app, ["capabilities", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["github_issues_enabled"] is True
        assert data["jira_issues_enabled"] is False
        mock_client.capabilities.assert_called_once()


class TestAiConfigsCommand:
    def test_ai_configs(self, mock_client):
        mock_client.get_ai_configs.return_value = [
            {"ai_provider": "claude", "ai_model": "opus-4"},
            {"ai_provider": "gemini", "ai_model": "2.5-pro"},
        ]
        result = runner.invoke(app, ["ai-configs"])
        assert result.exit_code == 0
        assert "claude" in result.output
        assert "opus-4" in result.output

    def test_ai_configs_json(self, mock_client):
        mock_client.get_ai_configs.return_value = [
            {"ai_provider": "claude", "ai_model": "opus-4"},
        ]
        result = runner.invoke(app, ["ai-configs", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed[0]["ai_provider"] == "claude"

    def test_ai_configs_empty(self, mock_client):
        mock_client.get_ai_configs.return_value = []
        result = runner.invoke(app, ["ai-configs"])
        assert result.exit_code == 0
        assert "No AI configurations found" in result.output


class TestAiModelsCommand:
    def test_ai_models_with_provider(self, mock_client):
        mock_client.list_ai_models.return_value = {
            "provider": "claude",
            "models": [
                {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"},
                {"id": "claude-opus-4-6", "name": "Claude Opus 4 6"},
            ],
        }
        result = runner.invoke(app, ["ai-models", "--provider", "claude"])
        assert result.exit_code == 0
        mock_client.list_ai_models.assert_called_once_with(provider="claude")
        assert "claude-sonnet-4" in result.output
        assert "claude-opus-4-6" in result.output

    def test_ai_models_no_provider(self, mock_client):
        mock_client.list_ai_models.return_value = {
            "providers": {
                "claude": [{"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}],
                "cursor": [],
                "gemini": [{"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"}],
            }
        }
        result = runner.invoke(app, ["ai-models"])
        assert result.exit_code == 0
        mock_client.list_ai_models.assert_called_once_with(provider="")
        assert "claude" in result.output
        assert "gemini" in result.output

    def test_ai_models_json(self, mock_client):
        mock_client.list_ai_models.return_value = {
            "provider": "claude",
            "models": [{"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}],
        }
        result = runner.invoke(app, ["ai-models", "--provider", "claude", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["provider"] == "claude"
        assert len(parsed["models"]) == 1

    def test_ai_models_empty_provider(self, mock_client):
        mock_client.list_ai_models.return_value = {
            "provider": "cursor",
            "models": [],
        }
        result = runner.invoke(app, ["ai-models", "--provider", "cursor"])
        assert result.exit_code == 0
        assert "No models found" in result.output

    def test_ai_models_empty_all(self, mock_client):
        mock_client.list_ai_models.return_value = {"providers": {}}
        result = runner.invoke(app, ["ai-models"])
        assert result.exit_code == 0
        assert "No models found" in result.output

    def test_ai_models_error(self, mock_client):
        from jenkins_job_insight.cli.client import JJIError

        mock_client.list_ai_models.side_effect = JJIError(
            status_code=500, detail="Server error"
        )
        result = runner.invoke(app, ["ai-models"])
        assert result.exit_code == 1
        assert "Error" in result.output


class TestMentionableUsersCommand:
    def test_mentionable_users(self, mock_client):
        mock_client.get_mentionable_users.return_value = {
            "usernames": ["alice", "bob", "charlie"]
        }
        result = runner.invoke(app, ["mentionable-users"])
        assert result.exit_code == 0
        assert "alice" in result.output
        assert "bob" in result.output
        assert "charlie" in result.output
        mock_client.get_mentionable_users.assert_called_once()

    def test_mentionable_users_json(self, mock_client):
        mock_client.get_mentionable_users.return_value = {"usernames": ["alice", "bob"]}
        result = runner.invoke(app, ["--json", "mentionable-users"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["usernames"] == ["alice", "bob"]

    def test_mentionable_users_empty(self, mock_client):
        mock_client.get_mentionable_users.return_value = {"usernames": []}
        result = runner.invoke(app, ["mentionable-users"])
        assert result.exit_code == 0
        assert "No mentionable users found" in result.output


class TestMentionsCommand:
    def test_mentions_command(self, mock_client):
        mock_client.get_mentions.return_value = {
            "mentions": [
                {
                    "id": 1,
                    "job_id": "job-1",
                    "test_name": "test_one",
                    "comment": "Hey @testuser",
                    "username": "bob",
                    "is_read": False,
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "total": 1,
            "unread_count": 1,
        }
        result = runner.invoke(app, ["mentions"])
        assert result.exit_code == 0
        assert "1 mention(s)" in result.output
        assert "1 unread" in result.output
        mock_client.get_mentions.assert_called_once_with(
            limit=50, offset=0, unread_only=False
        )

    def test_mentions_unread_flag(self, mock_client):
        mock_client.get_mentions.return_value = {
            "mentions": [],
            "total": 0,
            "unread_count": 0,
        }
        result = runner.invoke(app, ["mentions", "--unread"])
        assert result.exit_code == 0
        mock_client.get_mentions.assert_called_once_with(
            limit=50, offset=0, unread_only=True
        )

    def test_mentions_empty(self, mock_client):
        mock_client.get_mentions.return_value = {
            "mentions": [],
            "total": 0,
            "unread_count": 0,
        }
        result = runner.invoke(app, ["mentions"])
        assert result.exit_code == 0
        assert "No mentions found" in result.output

    def test_mentions_json_output(self, mock_client):
        payload = {
            "mentions": [{"id": 1, "comment": "@user"}],
            "total": 1,
            "unread_count": 0,
        }
        mock_client.get_mentions.return_value = payload
        result = runner.invoke(app, ["--json", "mentions"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["total"] == 1

    def test_mentions_with_offset(self, mock_client):
        mock_client.get_mentions.return_value = {
            "mentions": [],
            "total": 0,
            "unread_count": 0,
        }
        result = runner.invoke(app, ["mentions", "--offset", "10"])
        assert result.exit_code == 0
        mock_client.get_mentions.assert_called_once_with(
            limit=50, offset=10, unread_only=False
        )


class TestMentionsMarkReadCommand:
    def test_mentions_mark_read_command(self, mock_client):
        mock_client.mark_mentions_read.return_value = {"ok": True}
        result = runner.invoke(app, ["mentions-mark-read", "--ids", "1,2,3"])
        assert result.exit_code == 0
        mock_client.mark_mentions_read.assert_called_once_with([1, 2, 3])

    def test_mentions_mark_read_invalid_ids(self, mock_client):
        result = runner.invoke(app, ["mentions-mark-read", "--ids", "1,foo"])
        assert result.exit_code == 1
        assert "invalid ID" in result.output

    def test_mentions_mark_read_negative_id(self, mock_client):
        result = runner.invoke(app, ["mentions-mark-read", "--ids", "-1"])
        assert result.exit_code == 1
        assert "invalid ID" in result.output

    def test_mentions_mark_read_json(self, mock_client):
        mock_client.mark_mentions_read.return_value = {"ok": True}
        result = runner.invoke(app, ["--json", "mentions-mark-read", "--ids", "5"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True


class TestMentionsMarkAllReadCommand:
    def test_mentions_mark_all_read_command(self, mock_client):
        mock_client.mark_all_mentions_read.return_value = {"marked_read": 3}
        result = runner.invoke(app, ["mentions-mark-all-read"])
        assert result.exit_code == 0

    def test_mentions_mark_all_read_json(self, mock_client):
        mock_client.mark_all_mentions_read.return_value = {"marked_read": 3}
        result = runner.invoke(app, ["--json", "mentions-mark-all-read"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["marked_read"] == 3


class TestPreviewIssueCommand:
    def test_preview_github(self, mock_client):
        mock_client.preview_github_issue.return_value = {
            "title": "Fix: login handler",
            "body": "## Details...",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
            ],
        )
        assert result.exit_code == 0
        assert "Fix: login handler" in result.output

    def test_preview_jira(self, mock_client):
        mock_client.preview_jira_bug.return_value = {
            "title": "DNS timeout",
            "body": "h2. Summary...",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "jira",
            ],
        )
        assert result.exit_code == 0
        assert "DNS timeout" in result.output

    def test_preview_invalid_type(self, mock_client):
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "invalid",
            ],
        )
        assert result.exit_code == 1

    def test_preview_with_ai_config(self, mock_client):
        mock_client.preview_github_issue.return_value = {
            "title": "Fix: login handler",
            "body": "## Details...",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
                "--ai-provider",
                "claude",
                "--ai-model",
                "opus-4",
            ],
        )
        assert result.exit_code == 0
        assert "Fix: login handler" in result.output
        kwargs = mock_client.preview_github_issue.call_args[1]
        assert kwargs["ai_provider"] == "claude"
        assert kwargs["ai_model"] == "opus-4"

    def test_preview_with_similar_issues(self, mock_client):
        mock_client.preview_github_issue.return_value = {
            "title": "Fix: login handler",
            "body": "## Details...",
            "similar_issues": [
                {
                    "number": 42,
                    "key": "",
                    "title": "Similar bug",
                    "url": "https://github.com/org/repo/issues/42",
                }
            ],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
            ],
        )
        assert result.exit_code == 0
        assert "Similar issues (1)" in result.output

    def test_preview_with_user_tokens(self, mock_client):
        mock_client.preview_github_issue.return_value = {
            "title": "Fix: login handler",
            "body": "## Details...",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
                "--github-token",
                "ghp_tok",  # noqa: S106
                "--jira-token",
                "jira-tok",  # noqa: S106
                "--jira-email",
                "user@example.com",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.preview_github_issue.call_args[1]
        assert kwargs["github_token"] == "ghp_tok"  # noqa: S105
        assert "jira_token" not in kwargs
        assert "jira_email" not in kwargs

    def test_preview_jira_with_user_tokens(self, mock_client):
        mock_client.preview_jira_bug.return_value = {
            "title": "DNS timeout",
            "body": "h2. Summary...",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "jira",
                "--github-token",
                "ghp_tok",  # noqa: S106
                "--jira-token",
                "jira-tok",  # noqa: S106
                "--jira-email",
                "user@example.com",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.preview_jira_bug.call_args[1]
        assert "github_token" not in kwargs
        assert kwargs["jira_token"] == "jira-tok"  # noqa: S105
        assert kwargs["jira_email"] == "user@example.com"

    def test_preview_with_issue_prompt(self, mock_client):
        mock_client.preview_github_issue.return_value = {
            "title": "Fix",
            "body": "Body",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
                "--issue-prompt",
                "Include product version",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.preview_github_issue.call_args[1]
        assert kwargs["issue_prompt"] == "Include product version"

    def test_preview_jira_with_issue_prompt(self, mock_client):
        mock_client.preview_jira_bug.return_value = {
            "title": "Bug",
            "body": "Desc",
            "similar_issues": [],
        }
        result = runner.invoke(
            app,
            [
                "preview-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "jira",
                "--issue-prompt",
                "Include OCP version",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.preview_jira_bug.call_args[1]
        assert kwargs["issue_prompt"] == "Include OCP version"


class TestGetIssuePromptCommand:
    def test_get_issue_prompt_with_content(self, mock_client):
        mock_client.get_issue_prompt.return_value = {
            "prompt": "Include product version"
        }
        result = runner.invoke(app, ["get-issue-prompt", "job-1"])
        assert result.exit_code == 0
        assert "Include product version" in result.output
        mock_client.get_issue_prompt.assert_called_once_with(job_id="job-1")

    def test_get_issue_prompt_empty(self, mock_client):
        mock_client.get_issue_prompt.return_value = {"prompt": ""}
        result = runner.invoke(app, ["get-issue-prompt", "job-1"])
        assert result.exit_code == 0
        assert "No issue prompt found" in result.output
        mock_client.get_issue_prompt.assert_called_once_with(job_id="job-1")

    def test_get_issue_prompt_json(self, mock_client):
        expected = {"prompt": "Include product version"}
        mock_client.get_issue_prompt.return_value = expected
        result = runner.invoke(app, ["get-issue-prompt", "job-1", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed == expected
        mock_client.get_issue_prompt.assert_called_once_with(job_id="job-1")


class TestCreateIssueCommand:
    def test_create_github(self, mock_client):
        mock_client.create_github_issue.return_value = {
            "url": "https://github.com/org/repo/issues/99",
            "key": "",
            "title": "Bug fix",
            "comment_id": 42,
        }
        result = runner.invoke(
            app,
            [
                "create-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
                "--title",
                "Bug fix",
                "--body",
                "Details...",
            ],
        )
        assert result.exit_code == 0
        assert "github.com" in result.output or "99" in result.output

    def test_create_jira(self, mock_client):
        mock_client.create_jira_bug.return_value = {
            "url": "https://jira.example.com/browse/PROJ-456",
            "key": "PROJ-456",
            "title": "DNS timeout",
            "comment_id": 43,
        }
        result = runner.invoke(
            app,
            [
                "create-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "jira",
                "--title",
                "DNS timeout",
                "--body",
                "Description...",
            ],
        )
        assert result.exit_code == 0
        assert "PROJ-456" in result.output

    def test_create_invalid_type(self, mock_client):
        result = runner.invoke(
            app,
            [
                "create-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "invalid",
                "--title",
                "X",
                "--body",
                "Y",
            ],
        )
        assert result.exit_code == 1

    def test_create_with_user_tokens(self, mock_client):
        mock_client.create_github_issue.return_value = {
            "url": "https://github.com/org/repo/issues/99",
            "key": "",
            "title": "Bug fix",
            "comment_id": 42,
        }
        result = runner.invoke(
            app,
            [
                "create-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "github",
                "--title",
                "Bug fix",
                "--body",
                "Details...",
                "--github-token",
                "ghp_tok",  # noqa: S106
                "--jira-token",
                "jira-tok",  # noqa: S106
                "--jira-email",
                "user@example.com",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.create_github_issue.call_args[1]
        assert kwargs["github_token"] == "ghp_tok"  # noqa: S105
        assert "jira_token" not in kwargs
        assert "jira_email" not in kwargs

    def test_create_jira_with_user_tokens(self, mock_client):
        mock_client.create_jira_bug.return_value = {
            "url": "https://jira.example.com/browse/PROJ-456",
            "key": "PROJ-456",
            "title": "DNS timeout",
            "comment_id": 43,
        }
        result = runner.invoke(
            app,
            [
                "create-issue",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--type",
                "jira",
                "--title",
                "DNS timeout",
                "--body",
                "Description...",
                "--github-token",
                "ghp_tok",  # noqa: S106
                "--jira-token",
                "jira-tok",  # noqa: S106
                "--jira-email",
                "user@example.com",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.create_jira_bug.call_args[1]
        assert "github_token" not in kwargs
        assert kwargs["jira_token"] == "jira-tok"  # noqa: S105
        assert kwargs["jira_email"] == "user@example.com"


class TestOverrideClassificationCommand:
    def test_override(self, mock_client):
        mock_client.override_classification.return_value = {
            "status": "ok",
            "classification": "PRODUCT BUG",
        }
        result = runner.invoke(
            app,
            [
                "override-classification",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--classification",
                "PRODUCT BUG",
            ],
        )
        assert result.exit_code == 0
        assert "PRODUCT BUG" in result.output

    def test_override_json_mode(self, mock_client):
        mock_client.override_classification.return_value = {
            "status": "ok",
            "classification": "CODE ISSUE",
        }
        result = runner.invoke(
            app,
            [
                "--json",
                "override-classification",
                "job-1",
                "--test",
                "tests.TestA.test_one",
                "--classification",
                "CODE ISSUE",
            ],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["classification"] == "CODE ISSUE"


class TestStatusJsonFull:
    def test_status_json_returns_full_response(self, mock_client):
        """status --json should return full API response, not trimmed."""
        mock_client.get_result.return_value = {
            "job_id": "j1",
            "status": "completed",
            "result": {"summary": "5 failures"},
            "jenkins_url": "http://jenkins/job/1",
        }
        result = runner.invoke(app, ["--json", "status", "j1"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "result" in output  # Full response, not just job_id+status
        assert "jenkins_url" in output


class TestNoVerifySSL:
    def test_no_verify_ssl_flag_accepted(self, mock_client):
        """--no-verify-ssl flag should be accepted without error."""
        mock_client.health.return_value = {"status": "healthy"}
        result = runner.invoke(app, ["--no-verify-ssl", "health"])
        assert result.exit_code == 0
        assert "healthy" in result.output

    def test_no_verify_ssl_passes_to_client(self):
        """--no-verify-ssl should cause _get_client to create client with verify_ssl=False."""
        with (
            patch.dict(
                os.environ, {"JJI_SERVER": _TEST_SERVER, "JJI_USERNAME": ""}, clear=True
            ),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=ServerConfig(url=_TEST_SERVER),
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(app, ["--no-verify-ssl", "health"])
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER, username="", verify_ssl=False, api_key=""
            )

    def test_without_no_verify_ssl_flag(self):
        """Without --no-verify-ssl, client should be created with verify_ssl=True."""
        with (
            patch("jenkins_job_insight.cli.main._state", {}),
            patch.dict(
                os.environ, {"JJI_SERVER": _TEST_SERVER, "JJI_USERNAME": ""}, clear=True
            ),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=ServerConfig(url=_TEST_SERVER),
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(app, ["health"])
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER, username="", verify_ssl=True, api_key=""
            )

    def test_no_verify_ssl_env_var(self):
        """JJI_NO_VERIFY_SSL env var should enable SSL skip."""
        with (
            patch.dict(
                os.environ,
                {
                    "JJI_SERVER": _TEST_SERVER,
                    "JJI_NO_VERIFY_SSL": "true",
                    "JJI_USERNAME": "",
                },
                clear=True,
            ),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=ServerConfig(url=_TEST_SERVER),
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(app, ["health"])
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER, username="", verify_ssl=False, api_key=""
            )


class TestConfigCompletion:
    def test_completion_zsh(self):
        result = runner.invoke(app, ["config", "completion", "zsh"])
        assert result.exit_code == 0
        assert "~/.zshrc" in result.output
        assert "--show-completion" in result.output

    def test_completion_bash(self):
        result = runner.invoke(app, ["config", "completion", "bash"])
        assert result.exit_code == 0
        assert "~/.bashrc" in result.output
        assert "--show-completion" in result.output

    def test_completion_default_is_zsh(self):
        result = runner.invoke(app, ["config", "completion"])
        assert result.exit_code == 0
        assert "~/.zshrc" in result.output

    def test_completion_unsupported_shell(self):
        result = runner.invoke(app, ["config", "completion", "fish"])
        assert result.exit_code == 1
        assert "Unsupported shell" in result.output


class TestJsonPerCommand:
    def test_json_flag_after_subcommand(self, mock_client):
        """--json should work when placed after the subcommand."""
        mock_client.health.return_value = {"status": "healthy"}
        result = runner.invoke(app, ["health", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["status"] == "healthy"

    def test_json_flag_after_nested_subcommand(self, mock_client):
        """--json should work after a nested subcommand like 'results list'."""
        mock_client.list_results.return_value = [
            {
                "job_id": "abc-123",
                "status": "completed",
                "jenkins_url": "https://jenkins.example.com/job/test/1/",
                "created_at": "2026-03-18",
            },
        ]
        result = runner.invoke(app, ["results", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed[0]["job_id"] == "abc-123"


class TestAnalyzeConfigDefaults:
    """Config file values are used as defaults when CLI flags are not provided."""

    _ANALYZE_RESPONSE = MappingProxyType({"status": "queued", "job_id": "j1"})

    _FULL_CONFIG = ServerConfig(
        url="http://localhost:8000",
        username="dev-user",
        no_verify_ssl=False,
        jenkins_url="https://jenkins.cfg.local",
        jenkins_user="cfg-jenkins-user",
        jenkins_password=_FAKE_JENKINS_PASSWORD,
        jenkins_ssl_verify=False,
        jenkins_timeout=45,
        tests_repo_url="https://github.com/cfg/tests",
        ai_provider="gemini",
        ai_model="2.5-pro",
        ai_cli_timeout=20,
        max_concurrent_ai_calls=5,
        jira_url="https://jira.cfg.local",
        jira_email="cfg@example.com",
        jira_api_token=_FAKE_JIRA_API_TOKEN,
        jira_pat=_FAKE_JIRA_PAT,
        jira_project_key="CFG",
        jira_ssl_verify=False,
        jira_max_results=40,
        enable_jira=True,
        github_token=_FAKE_GITHUB_TOKEN,
    )

    def _invoke_analyze(self, cli_args: list[str], cfg: ServerConfig | None = None):
        """Invoke the analyze command with the given config and CLI args."""
        config = cfg or self._FULL_CONFIG
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=config,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(app, cli_args)
            return result, client

    def test_config_string_fields_used_as_defaults(self):
        """String config fields are sent when CLI flags are absent."""
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["jenkins_url"] == "https://jenkins.cfg.local"
        assert kwargs["jenkins_user"] == "cfg-jenkins-user"
        assert kwargs["jenkins_password"] == _FAKE_JENKINS_PASSWORD
        assert kwargs["tests_repo_url"] == "https://github.com/cfg/tests"
        assert kwargs["ai_provider"] == "gemini"
        assert kwargs["ai_model"] == "2.5-pro"
        assert kwargs["jira_url"] == "https://jira.cfg.local"
        assert kwargs["jira_email"] == "cfg@example.com"
        assert kwargs["jira_api_token"] == _FAKE_JIRA_API_TOKEN
        assert kwargs["jira_pat"] == _FAKE_JIRA_PAT
        assert kwargs["jira_project_key"] == "CFG"
        assert kwargs["github_token"] == _FAKE_GITHUB_TOKEN

    def test_config_integer_fields_used_as_defaults(self):
        """Integer config fields are sent when CLI flags are absent."""
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["ai_cli_timeout"] == 20
        assert kwargs["max_concurrent_ai_calls"] == 5
        assert kwargs["jira_max_results"] == 40
        assert kwargs["jenkins_timeout"] == 45

    def test_config_boolean_fields_used_as_defaults(self):
        """Boolean config fields are sent when CLI flags are absent."""
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["enable_jira"] is True
        assert kwargs["jenkins_ssl_verify"] is False
        assert kwargs["jira_ssl_verify"] is False

    def test_cli_flags_override_config_string_fields(self):
        """CLI flags take precedence over config values."""
        result, client = self._invoke_analyze(
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--provider",
                "claude",
                "--model",
                "opus-4",
                "--jenkins-url",
                "https://jenkins.cli.local",
                "--github-token",
                _FAKE_GITHUB_CLI_OVERRIDE,
            ]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["ai_provider"] == "claude"
        assert kwargs["ai_model"] == "opus-4"
        assert kwargs["jenkins_url"] == "https://jenkins.cli.local"
        assert kwargs["github_token"] == _FAKE_GITHUB_CLI_OVERRIDE
        # Non-overridden fields still come from config.
        assert kwargs["jenkins_user"] == "cfg-jenkins-user"
        assert kwargs["jira_url"] == "https://jira.cfg.local"

    def test_cli_flags_override_config_int_fields(self):
        """CLI int flags override config int values."""
        result, client = self._invoke_analyze(
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--ai-cli-timeout",
                "99",
                "--jira-max-results",
                "5",
            ]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["ai_cli_timeout"] == 99
        assert kwargs["jira_max_results"] == 5

    def test_cli_flags_override_config_bool_fields(self):
        """CLI boolean flags override config boolean values."""
        result, client = self._invoke_analyze(
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--jenkins-ssl-verify",
                "--jira-ssl-verify",
            ]
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["jenkins_ssl_verify"] is True
        assert kwargs["jira_ssl_verify"] is True

    def test_no_config_means_no_defaults(self):
        """When no config is available, analyze sends only CLI-provided fields."""
        cfg = ServerConfig(url="http://localhost:8000")
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"], cfg=cfg
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert "jenkins_url" not in kwargs
        assert "ai_provider" not in kwargs
        assert "enable_jira" not in kwargs
        assert "github_token" not in kwargs

    def test_config_wait_for_completion_used_as_default(self):
        """wait_for_completion from config is sent when CLI flag is absent."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            wait_for_completion=True,
        )
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"], cfg=cfg
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["wait_for_completion"] is True

    def test_config_poll_interval_used_as_default(self):
        """poll_interval_minutes from config is sent when CLI flag is absent."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            poll_interval_minutes=7,
        )
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"], cfg=cfg
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["poll_interval_minutes"] == 7

    def test_config_max_wait_used_as_default(self):
        """max_wait_minutes from config is sent when CLI flag is absent."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            max_wait_minutes=90,
        )
        result, client = self._invoke_analyze(
            ["analyze", "--job-name", "my-job", "--build-number", "1"], cfg=cfg
        )
        assert result.exit_code == 0
        kwargs = client.analyze.call_args[1]
        assert kwargs["max_wait_minutes"] == 90


class TestAnalyzePeerFlags:
    """Tests for --peers and --peer-analysis-max-rounds CLI flags."""

    _ANALYZE_RESPONSE = MappingProxyType({"status": "queued", "job_id": "j1"})

    def test_peers_flag_parsed_and_sent(self, mock_client):
        """--peers should parse and send peer_ai_configs list in request body."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--peers",
                "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["peer_ai_configs"] == [
            {"ai_provider": "cursor", "ai_model": "gpt-5.4-xhigh"},
            {"ai_provider": "gemini", "ai_model": "gemini-2.5-pro"},
        ]

    def test_peers_flag_invalid_format_exits(self, mock_client):
        """--peers with invalid format should print error and exit 1."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--peers",
                "invalid-no-colon",
            ],
        )
        assert result.exit_code == 1
        assert "invalid" in result.output.lower() or "Error" in result.output

    def test_peer_analysis_max_rounds_sent(self, mock_client):
        """--peer-analysis-max-rounds should send peer_analysis_max_rounds in body."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--peers",
                "cursor:gpt-5,gemini:2.5-pro",
                "--peer-analysis-max-rounds",
                "5",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["peer_analysis_max_rounds"] == 5

    def test_no_peers_flag_omits_peer_ai_configs(self, mock_client):
        """When --peers is not given and config has no peers, peer_ai_configs is not sent."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "peer_ai_configs" not in kwargs

    def test_whitespace_only_peers_config_omits_peer_ai_configs(self):
        """Whitespace-only config peers should be treated as 'unset', not 'disable'."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peers="   ",
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert "peer_ai_configs" not in kwargs

    def test_peers_from_config_used_as_default(self):
        """Config peers should be used when --peers is not given on CLI."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peers="cursor:gpt-5,gemini:2.5-pro",
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["peer_ai_configs"] == [
                {"ai_provider": "cursor", "ai_model": "gpt-5"},
                {"ai_provider": "gemini", "ai_model": "2.5-pro"},
            ]

    def test_cli_peers_overrides_config_peers(self):
        """CLI --peers should override config peers value."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peers="cursor:gpt-5,gemini:2.5-pro",
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--job-name",
                    "my-job",
                    "--build-number",
                    "1",
                    "--peers",
                    "claude:opus-4",
                ],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["peer_ai_configs"] == [
                {"ai_provider": "claude", "ai_model": "opus-4"},
            ]

    def test_whitespace_only_cli_peers_falls_through_to_config(self):
        """CLI --peers with only whitespace should fall through to config default."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peers="cursor:gpt-5,gemini:2.5-pro",
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--job-name",
                    "my-job",
                    "--build-number",
                    "1",
                    "--peers",
                    "   ",
                ],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            # Whitespace-only --peers should not block config fallback
            assert kwargs["peer_ai_configs"] == [
                {"ai_provider": "cursor", "ai_model": "gpt-5"},
                {"ai_provider": "gemini", "ai_model": "2.5-pro"},
            ]

    def test_peer_analysis_max_rounds_from_config(self):
        """Config peer_analysis_max_rounds should be used when CLI flag is absent."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peer_analysis_max_rounds=7,
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["peer_analysis_max_rounds"] == 7

    def test_peer_analysis_max_rounds_cli_too_low(self, mock_client):
        """--peer-analysis-max-rounds below 1 should exit with error."""
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--peer-analysis-max-rounds",
                "0",
            ],
        )
        assert result.exit_code == 1
        assert "must be between 1 and 10" in result.output

    def test_peer_analysis_max_rounds_cli_too_high(self, mock_client):
        """--peer-analysis-max-rounds above 10 should exit with error."""
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--peer-analysis-max-rounds",
                "11",
            ],
        )
        assert result.exit_code == 1
        assert "must be between 1 and 10" in result.output

    def test_peer_analysis_max_rounds_config_too_low(self):
        """Config peer_analysis_max_rounds below 1 should exit with error."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peer_analysis_max_rounds=-1,
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 1
            assert "must be between 1 and 10" in result.output

    def test_peer_analysis_max_rounds_config_too_high(self):
        """Config peer_analysis_max_rounds above 10 should exit with error."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            peer_analysis_max_rounds=11,
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 1
            assert "must be between 1 and 10" in result.output


class TestAnalyzeAdditionalReposFlags:
    """Tests for --additional-repos CLI flag."""

    _ANALYZE_RESPONSE = MappingProxyType({"status": "queued", "job_id": "j1"})

    def test_additional_repos_flag_parsed_and_sent(self, mock_client):
        """--additional-repos should parse and send additional_repos list."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--additional-repos",
                "infra:https://github.com/org/infra,product:https://github.com/org/product",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["additional_repos"] == [
            {"name": "infra", "url": "https://github.com/org/infra", "ref": ""},
            {"name": "product", "url": "https://github.com/org/product", "ref": ""},
        ]

    def test_additional_repos_flag_invalid_format_exits(self, mock_client):
        """--additional-repos with invalid format should print error and exit 1."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--additional-repos",
                "invalid-no-colon",
            ],
        )
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_no_additional_repos_flag_omits_field(self, mock_client):
        """When --additional-repos is not given, additional_repos is not sent."""
        mock_client.analyze.return_value = self._ANALYZE_RESPONSE
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "additional_repos" not in kwargs

    def test_additional_repos_from_config(self):
        """Config additional_repos should be used when CLI flag is absent."""
        cfg = ServerConfig(
            url="http://localhost:8000",
            additional_repos="infra:https://github.com/org/infra",
        )
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = self._ANALYZE_RESPONSE
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["additional_repos"] == [
                {"name": "infra", "url": "https://github.com/org/infra", "ref": ""},
            ]


class TestAnalyzeWaitFlags:
    """Tests for --wait/--no-wait, --poll-interval, --max-wait CLI flags."""

    def test_wait_flag(self, mock_client):
        """--wait should send wait_for_completion=True."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "1", "--wait"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["wait_for_completion"] is True

    def test_no_wait_flag(self, mock_client):
        """--no-wait should send wait_for_completion=False."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "1", "--no-wait"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["wait_for_completion"] is False

    def test_poll_interval_flag(self, mock_client):
        """--poll-interval should send poll_interval_minutes."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--poll-interval",
                "5",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["poll_interval_minutes"] == 5

    def test_max_wait_flag(self, mock_client):
        """--max-wait should send max_wait_minutes."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            [
                "analyze",
                "--job-name",
                "my-job",
                "--build-number",
                "1",
                "--max-wait",
                "30",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["max_wait_minutes"] == 30

    def test_wait_omitted_not_in_extras(self, mock_client):
        """When --wait/--no-wait is not given, wait_for_completion is not sent."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "wait_for_completion" not in kwargs


class TestAnalyzeForceFlag:
    """Tests for --force/--no-force CLI flag."""

    def test_force_flag(self, mock_client):
        """--force should send force=True."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "1", "--force"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["force"] is True

    def test_no_force_flag(self, mock_client):
        """--no-force should send force=False."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        result = runner.invoke(
            app,
            ["analyze", "--job-name", "my-job", "--build-number", "1", "--no-force"],
        )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert kwargs["force"] is False

    def test_force_omitted_not_in_extras(self, mock_client):
        """When --force/--no-force is not given, force is not sent."""
        mock_client.analyze.return_value = {"status": "queued", "job_id": "j1"}
        with patch.dict(os.environ, _env_without_analyze_bindings(), clear=True):
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
        assert result.exit_code == 0
        kwargs = mock_client.analyze.call_args[1]
        assert "force" not in kwargs

    def test_force_from_config(self, mock_client):
        """Config force=true is used as default when CLI flag is absent."""
        cfg = ServerConfig(url=_TEST_SERVER, force=True)
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = {"status": "queued", "job_id": "j1"}
            mock_fn.return_value = client
            result = runner.invoke(
                app,
                ["analyze", "--job-name", "my-job", "--build-number", "1"],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["force"] is True

    def test_cli_force_overrides_config(self, mock_client):
        """CLI --no-force overrides config force=true."""
        cfg = ServerConfig(url=_TEST_SERVER, force=True)
        with (
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            client = MagicMock()
            client.analyze.return_value = {"status": "queued", "job_id": "j1"}
            mock_fn.return_value = client
            result = runner.invoke(
                app,
                [
                    "analyze",
                    "--job-name",
                    "my-job",
                    "--build-number",
                    "1",
                    "--no-force",
                ],
            )
            assert result.exit_code == 0
            kwargs = client.analyze.call_args[1]
            assert kwargs["force"] is False


class TestValidateTokenCommand:
    def test_validate_token_valid(self, mock_client):
        mock_client.validate_token.return_value = {
            "valid": True,
            "username": "testuser",
            "message": "Authenticated as testuser",
        }
        result = runner.invoke(app, ["validate-token", "github", "--token", "ghp_test"])  # noqa: S106
        assert result.exit_code == 0
        assert "Valid" in result.output
        mock_client.validate_token.assert_called_once_with(
            token_type="github",  # noqa: S106
            token="ghp_test",  # noqa: S105, S106
            email="",
        )

    def test_validate_token_invalid(self, mock_client):
        mock_client.validate_token.return_value = {
            "valid": False,
            "username": "",
            "message": "Invalid token (HTTP 401)",
        }
        result = runner.invoke(app, ["validate-token", "github", "--token", "bad"])  # noqa: S106
        assert result.exit_code == 1
        assert "Invalid" in result.output

    def test_validate_token_json(self, mock_client):
        mock_client.validate_token.return_value = {
            "valid": True,
            "username": "testuser",
            "message": "Authenticated as testuser",
        }
        result = runner.invoke(
            app,
            ["--json", "validate-token", "github", "--token", "ghp_test"],  # noqa: S106
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["valid"] is True

    def test_validate_token_with_email(self, mock_client):
        mock_client.validate_token.return_value = {
            "valid": True,
            "username": "User",
            "message": "Authenticated as User",
        }
        result = runner.invoke(
            app,
            [
                "validate-token",
                "jira",
                "--token",
                "jira-tok",  # noqa: S106
                "--email",
                "user@example.com",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_client.validate_token.call_args[1]
        assert kwargs["token_type"] == "jira"  # noqa: S105
        assert kwargs["token"] == "jira-tok"  # noqa: S105
        assert kwargs["email"] == "user@example.com"


class TestJiraProjectsCommand:
    def test_jira_projects(self, mock_client):
        mock_client.jira_projects.return_value = [
            {"key": "PROJ", "name": "My Project"},
        ]
        result = runner.invoke(app, ["jira-projects"])
        assert result.exit_code == 0
        assert "PROJ" in result.output

    def test_jira_projects_json(self, mock_client):
        mock_client.jira_projects.return_value = [
            {"key": "PROJ", "name": "My Project"},
        ]
        result = runner.invoke(app, ["--json", "jira-projects"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["key"] == "PROJ"

    def test_jira_projects_empty(self, mock_client):
        mock_client.jira_projects.return_value = []
        result = runner.invoke(app, ["jira-projects"])
        assert result.exit_code == 0


class TestJiraSecurityLevelsCommand:
    def test_jira_security_levels(self, mock_client):
        mock_client.jira_security_levels.return_value = [
            {"id": "10", "name": "Internal", "description": "Internal only"},
        ]
        result = runner.invoke(app, ["jira-security-levels", "PROJ"])
        assert result.exit_code == 0
        assert "Internal" in result.output

    def test_jira_security_levels_json(self, mock_client):
        mock_client.jira_security_levels.return_value = [
            {"id": "10", "name": "Internal", "description": "Internal only"},
        ]
        result = runner.invoke(app, ["--json", "jira-security-levels", "PROJ"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "Internal"

    def test_jira_security_levels_empty(self, mock_client):
        mock_client.jira_security_levels.return_value = []
        result = runner.invoke(app, ["jira-security-levels", "PROJ"])
        assert result.exit_code == 0
        assert "No security levels found" in result.output


class TestReAnalyzeCommand:
    def test_re_analyze(self, mock_client):
        mock_client.re_analyze.return_value = {
            "status": "queued",
            "job_id": "new-1",
            "result_url": "/results/new-1",
        }
        result = runner.invoke(app, ["re-analyze", "old-job-1"])
        assert result.exit_code == 0
        assert "new-1" in result.output
        assert "queued" in result.output.lower()
        mock_client.re_analyze.assert_called_once_with("old-job-1")

    def test_re_analyze_json(self, mock_client):
        mock_client.re_analyze.return_value = {
            "status": "queued",
            "job_id": "new-1",
            "result_url": "/results/new-1",
        }
        result = runner.invoke(app, ["--json", "re-analyze", "old-job-1"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["status"] == "queued"
        assert parsed["job_id"] == "new-1"

    def test_re_analyze_error(self, mock_client):
        mock_client.re_analyze.side_effect = JJIError(
            status_code=404, detail="Job not found"
        )
        result = runner.invoke(app, ["re-analyze", "nonexistent"])
        assert result.exit_code != 0
        assert "404" in result.output or "not found" in result.output.lower()


class TestPushRpCommand:
    def test_push_rp(self, mock_client):
        mock_client.push_reportportal.return_value = {
            "pushed": 3,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }
        result = runner.invoke(app, ["push-rp", "job-123"])
        assert result.exit_code == 0
        assert "3" in result.output
        mock_client.push_reportportal.assert_called_once_with(
            "job-123",
            child_job_name=None,
            child_build_number=None,
        )

    def test_push_rp_json(self, mock_client):
        mock_client.push_reportportal.return_value = {
            "pushed": 3,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }
        result = runner.invoke(app, ["--json", "push-rp", "job-123"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["pushed"] == 3
        assert parsed["launch_id"] == 42

    def test_push_rp_error(self, mock_client):
        mock_client.push_reportportal.side_effect = JJIError(
            status_code=400, detail="Report Portal is disabled"
        )
        result = runner.invoke(app, ["push-rp", "job-123"])
        assert result.exit_code != 0
        assert "400" in result.output or "disabled" in result.output.lower()

    def test_push_rp_with_errors(self, mock_client):
        mock_client.push_reportportal.return_value = {
            "pushed": 1,
            "unmatched": ["test_unmatched"],
            "errors": ["Failed to update RP item 99"],
            "launch_id": 42,
        }
        result = runner.invoke(app, ["push-rp", "job-123"])
        assert result.exit_code == 0
        assert "1" in result.output
        assert "test_unmatched" in result.output
        assert "Errors: 1" in result.output

    def test_push_rp_child_job_flags(self, mock_client):
        mock_client.push_reportportal.return_value = {
            "pushed": 2,
            "unmatched": [],
            "errors": [],
            "launch_id": 55,
        }
        result = runner.invoke(
            app,
            [
                "push-rp",
                "job-123",
                "--child-job-name",
                "my-child",
                "--child-build-number",
                "42",
            ],
        )
        assert result.exit_code == 0
        assert "2" in result.output
        mock_client.push_reportportal.assert_called_once_with(
            "job-123",
            child_job_name="my-child",
            child_build_number=42,
        )


class TestAuthLoginCommand:
    def test_auth_login(self, mock_client):
        mock_client.login.return_value = {
            "username": "admin",
            "role": "admin",
            "is_admin": True,
        }
        result = runner.invoke(
            app,
            ["auth", "login", "--username", "admin", "--api-key", "test-key"],  # noqa: S106
        )
        assert result.exit_code == 0
        assert "Logged in as admin" in result.output
        assert "admin: True" in result.output
        mock_client.login.assert_called_once_with("admin", "test-key")

    def test_auth_login_json(self, mock_client):
        mock_client.login.return_value = {
            "username": "admin",
            "role": "admin",
            "is_admin": True,
        }
        result = runner.invoke(
            app,
            ["--json", "auth", "login", "--username", "admin", "--api-key", "key"],  # noqa: S106
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["is_admin"] is True

    def test_auth_login_error(self, mock_client):
        mock_client.login.side_effect = JJIError(status_code=401, detail="Invalid")
        result = runner.invoke(
            app,
            ["auth", "login", "--username", "admin", "--api-key", "bad"],  # noqa: S106
        )
        assert result.exit_code == 1
        assert "Error" in result.output


class TestAuthLogoutCommand:
    def test_auth_logout(self, mock_client):
        mock_client.logout.return_value = {"status": "logged_out"}
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0

    def test_auth_logout_json(self, mock_client):
        mock_client.logout.return_value = {"status": "logged_out"}
        result = runner.invoke(app, ["--json", "auth", "logout"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["status"] == "logged_out"


class TestAuthWhoamiCommand:
    def test_auth_whoami(self, mock_client):
        mock_client.auth_me.return_value = {
            "username": "admin",
            "role": "admin",
            "is_admin": True,
        }
        result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "admin" in result.output

    def test_auth_whoami_json(self, mock_client):
        mock_client.auth_me.return_value = {
            "username": "testuser",
            "role": "user",
            "is_admin": False,
        }
        result = runner.invoke(app, ["--json", "auth", "whoami"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["username"] == "testuser"
        assert parsed["is_admin"] is False


class TestAdminUsersListCommand:
    def test_admin_users_list(self, mock_client):
        mock_client.admin_list_users.return_value = {
            "users": [
                {
                    "username": "admin",
                    "role": "admin",
                    "created_at": "2026-01-01",
                    "last_seen": "2026-04-18",
                },
            ]
        }
        result = runner.invoke(app, ["admin", "users", "list"])
        assert result.exit_code == 0
        assert "admin" in result.output

    def test_admin_users_list_json(self, mock_client):
        mock_client.admin_list_users.return_value = {
            "users": [{"username": "admin", "role": "admin"}]
        }
        result = runner.invoke(app, ["--json", "admin", "users", "list"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "users" in parsed


class TestAdminUsersCreateCommand:
    def test_admin_users_create(self, mock_client):
        mock_client.admin_create_user.return_value = {
            "username": "newadmin",
            "api_key": "not-a-real-key",  # pragma: allowlist secret
            "role": "admin",
        }
        result = runner.invoke(app, ["admin", "users", "create", "newadmin"])
        assert result.exit_code == 0
        assert "Created admin user: newadmin" in result.output
        assert "not-a-real-key" in result.output
        assert "Save this API key" in result.output
        mock_client.admin_create_user.assert_called_once_with("newadmin")

    def test_admin_users_create_json(self, mock_client):
        mock_client.admin_create_user.return_value = {
            "username": "newadmin",
            "api_key": "not-a-real-key",  # pragma: allowlist secret
            "role": "admin",
        }
        result = runner.invoke(app, ["--json", "admin", "users", "create", "newadmin"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["username"] == "newadmin"
        assert parsed["api_key"] == "not-a-real-key"  # pragma: allowlist secret

    def test_admin_users_create_error(self, mock_client):
        mock_client.admin_create_user.side_effect = JJIError(
            status_code=403, detail="Admin access required"
        )
        result = runner.invoke(app, ["admin", "users", "create", "newadmin"])
        assert result.exit_code == 1


class TestAdminUsersDeleteCommand:
    def test_admin_users_delete_with_force(self, mock_client):
        mock_client.admin_delete_user.return_value = {"deleted": "oldadmin"}
        result = runner.invoke(app, ["admin", "users", "delete", "oldadmin", "--force"])
        assert result.exit_code == 0
        assert "Deleted admin user: oldadmin" in result.output
        mock_client.admin_delete_user.assert_called_once_with("oldadmin")

    def test_admin_users_delete_with_confirm(self, mock_client):
        mock_client.admin_delete_user.return_value = {"deleted": "oldadmin"}
        result = runner.invoke(
            app, ["admin", "users", "delete", "oldadmin"], input="y\n"
        )
        assert result.exit_code == 0
        assert "Deleted admin user: oldadmin" in result.output

    def test_admin_users_delete_aborted(self, mock_client):
        result = runner.invoke(
            app, ["admin", "users", "delete", "oldadmin"], input="n\n"
        )
        assert result.exit_code != 0  # Abort

    def test_admin_users_delete_json(self, mock_client):
        mock_client.admin_delete_user.return_value = {"deleted": "oldadmin"}
        result = runner.invoke(
            app, ["--json", "admin", "users", "delete", "oldadmin", "--force"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["deleted"] == "oldadmin"


class TestAdminUsersRotateKeyCommand:
    def test_admin_users_rotate_key(self, mock_client):
        mock_client.admin_rotate_key.return_value = {
            "username": "myuser",
            "new_api_key": "not-a-real-key",  # pragma: allowlist secret
        }
        result = runner.invoke(app, ["admin", "users", "rotate-key", "myuser"])
        assert result.exit_code == 0
        assert "Rotated API key for: myuser" in result.output
        assert "not-a-real-key" in result.output
        assert "Save this API key" in result.output
        mock_client.admin_rotate_key.assert_called_once_with("myuser")

    def test_admin_users_rotate_key_json(self, mock_client):
        mock_client.admin_rotate_key.return_value = {
            "username": "myuser",
            "new_api_key": "not-a-real-key",  # pragma: allowlist secret
        }
        result = runner.invoke(
            app, ["--json", "admin", "users", "rotate-key", "myuser"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["new_api_key"] == "not-a-real-key"  # pragma: allowlist secret

    def test_admin_users_rotate_key_error(self, mock_client):
        mock_client.admin_rotate_key.side_effect = JJIError(
            status_code=404, detail="User not found"
        )
        result = runner.invoke(app, ["admin", "users", "rotate-key", "nonexistent"])
        assert result.exit_code == 1


class TestAdminUsersChangeRole:
    def test_change_role_promote(self, mock_client):
        mock_client.admin_change_role.return_value = {
            "username": "myuser",
            "role": "admin",
            "api_key": "not-a-real-key",  # pragma: allowlist secret
        }
        result = runner.invoke(
            app, ["admin", "users", "change-role", "myuser", "admin"]
        )
        assert result.exit_code == 0
        assert "admin" in result.output
        assert "not-a-real-key" in result.output

    def test_change_role_demote(self, mock_client):
        mock_client.admin_change_role.return_value = {
            "username": "myuser",
            "role": "user",
        }
        result = runner.invoke(app, ["admin", "users", "change-role", "myuser", "user"])
        assert result.exit_code == 0
        assert "user" in result.output
        assert "API Key" not in result.output

    def test_change_role_json(self, mock_client):
        mock_client.admin_change_role.return_value = {
            "username": "myuser",
            "role": "admin",
            "api_key": "not-a-real-key",  # pragma: allowlist secret
        }
        result = runner.invoke(
            app, ["--json", "admin", "users", "change-role", "myuser", "admin"]
        )
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["role"] == "admin"


class TestTokenUsageCommand:
    def test_token_usage_summary_default(self, mock_client):
        """No flags → summary mode."""
        mock_client.get_token_usage_summary.return_value = {
            "today": {"calls": 10, "tokens": 5000, "cost_usd": 0.05},
            "this_week": {"calls": 50, "tokens": 25000, "cost_usd": 0.25},
            "this_month": {"calls": 200, "tokens": 100000, "cost_usd": 1.00},
            "top_models": [
                {"model": "claude-sonnet", "calls": 100, "cost_usd": 0.50},
            ],
        }
        result = runner.invoke(app, ["admin", "token-usage"])
        assert result.exit_code == 0
        assert "Today" in result.output
        assert "This Week" in result.output
        assert "This Month" in result.output
        assert "claude-sonnet" in result.output
        mock_client.get_token_usage_summary.assert_called_once()

    def test_token_usage_summary_json(self, mock_client):
        mock_client.get_token_usage_summary.return_value = {
            "today": {"calls": 10},
        }
        result = runner.invoke(app, ["--json", "admin", "token-usage"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["today"]["calls"] == 10

    def test_token_usage_with_group_by(self, mock_client):
        """--group-by triggers filtered mode, not summary."""
        mock_client.get_token_usage.return_value = {
            "total_calls": 5,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_cache_read_tokens": 200,
            "total_cache_write_tokens": 100,
            "total_cost_usd": 0.10,
            "total_duration_ms": 5000,
            "breakdown": [
                {
                    "group_key": "claude",
                    "call_count": 3,
                    "cost_usd": 0.06,
                    "avg_duration_ms": 800,
                },
                {
                    "group_key": "gemini",
                    "call_count": 2,
                    "cost_usd": 0.04,
                    "avg_duration_ms": 600,
                },
            ],
        }
        result = runner.invoke(app, ["admin", "token-usage", "--group-by", "provider"])
        assert result.exit_code == 0
        assert "Total calls: 5" in result.output
        assert "claude" in result.output
        assert "gemini" in result.output
        mock_client.get_token_usage.assert_called_once_with(
            start_date=None,
            end_date=None,
            ai_provider=None,
            ai_model=None,
            call_type=None,
            group_by="provider",
        )

    def test_token_usage_period_today(self, mock_client):
        """--period today sets start_date and uses filtered mode."""
        mock_client.get_token_usage.return_value = {
            "total_calls": 2,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "breakdown": [],
        }
        result = runner.invoke(app, ["admin", "token-usage", "--period", "today"])
        assert result.exit_code == 0
        # Should have called get_token_usage (not summary) because period sets start_date
        mock_client.get_token_usage.assert_called_once()
        call_kwargs = mock_client.get_token_usage.call_args[1]
        assert call_kwargs["start_date"] is not None

    def test_token_usage_job_id(self, mock_client):
        """--job-id fetches per-job usage."""
        mock_client.get_token_usage_for_job.return_value = {
            "job_id": "abc-123",
            "records": [
                {
                    "call_type": "analysis",
                    "ai_provider": "claude",
                    "ai_model": "sonnet",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cost_usd": 0.01,
                    "duration_ms": 1200,
                },
            ],
        }
        result = runner.invoke(app, ["admin", "token-usage", "--job-id", "abc-123"])
        assert result.exit_code == 0
        assert "Job: abc-123" in result.output
        assert "analysis" in result.output
        assert "claude" in result.output
        mock_client.get_token_usage_for_job.assert_called_once_with("abc-123")

    def test_token_usage_job_id_json(self, mock_client):
        mock_client.get_token_usage_for_job.return_value = {
            "job_id": "abc-123",
            "records": [],
        }
        result = runner.invoke(
            app, ["--json", "admin", "token-usage", "--job-id", "abc-123"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["job_id"] == "abc-123"

    def test_token_usage_csv_format(self, mock_client):
        mock_client.get_token_usage.return_value = {
            "total_calls": 1,
            "breakdown": [
                {"group_key": "claude", "call_count": 1, "cost_usd": 0.01},
            ],
        }
        result = runner.invoke(
            app,
            ["admin", "token-usage", "--group-by", "provider", "--format", "csv"],
        )
        assert result.exit_code == 0
        assert "group_key" in result.output  # CSV header
        assert "claude" in result.output

    def test_token_usage_error_403(self, mock_client):
        mock_client.get_token_usage_summary.side_effect = JJIError(
            status_code=403, detail="Admin access required"
        )
        result = runner.invoke(app, ["admin", "token-usage"])
        assert result.exit_code == 1

    def test_token_usage_with_provider_filter(self, mock_client):
        """--provider triggers filtered mode."""
        mock_client.get_token_usage.return_value = {
            "total_calls": 3,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "breakdown": [],
        }
        result = runner.invoke(app, ["admin", "token-usage", "--provider", "claude"])
        assert result.exit_code == 0
        mock_client.get_token_usage.assert_called_once()
        call_kwargs = mock_client.get_token_usage.call_args[1]
        assert call_kwargs["ai_provider"] == "claude"

    def test_token_usage_job_cost_none(self, mock_client):
        """cost_usd=None should display as N/A."""
        mock_client.get_token_usage_for_job.return_value = {
            "job_id": "x",
            "records": [
                {
                    "call_type": "analysis",
                    "ai_provider": "claude",
                    "ai_model": "sonnet",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cost_usd": None,
                    "duration_ms": 500,
                },
            ],
        }
        result = runner.invoke(app, ["admin", "token-usage", "--job-id", "x"])
        assert result.exit_code == 0
        assert "N/A" in result.output


class TestAnalyzeCommentIntentCommand:
    def test_analyze_comment_intent(self, mock_client):
        mock_client.analyze_comment_intent.return_value = {
            "suggests_reviewed": True,
            "reason": "Bug filed with link",
        }
        result = runner.invoke(
            app, ["analyze-comment-intent", "Filed JIRA-123 for this"]
        )
        assert result.exit_code == 0
        assert "True" in result.output
        assert "Bug filed with link" in result.output

    def test_analyze_comment_intent_not_reviewed(self, mock_client):
        mock_client.analyze_comment_intent.return_value = {
            "suggests_reviewed": False,
            "reason": "",
        }
        result = runner.invoke(
            app, ["analyze-comment-intent", "Can someone check the logs?"]
        )
        assert result.exit_code == 0
        assert "False" in result.output

    def test_analyze_comment_intent_json(self, mock_client):
        expected = {"suggests_reviewed": True, "reason": "Fix merged"}
        mock_client.analyze_comment_intent.return_value = expected
        result = runner.invoke(
            app, ["--json", "analyze-comment-intent", "Fixed in commit abc123"]
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == expected

    def test_analyze_comment_intent_with_ai_options(self, mock_client):
        mock_client.analyze_comment_intent.return_value = {
            "suggests_reviewed": True,
            "reason": "Bug filed",
        }
        result = runner.invoke(
            app,
            [
                "analyze-comment-intent",
                "Filed JIRA-123",
                "--ai-provider",
                "claude",
                "--ai-model",
                "claude-sonnet-4-20250514",
            ],
        )
        assert result.exit_code == 0
        mock_client.analyze_comment_intent.assert_called_once_with(
            comment="Filed JIRA-123",
            job_id="",
            ai_provider="claude",
            ai_model="claude-sonnet-4-20250514",
        )


class TestApiKeyOption:
    def test_api_key_passed_to_client(self):
        """--api-key should cause _get_client to create client with api_key."""
        with (
            patch.dict(
                os.environ,
                {"JJI_SERVER": _TEST_SERVER, "JJI_USERNAME": ""},
                clear=True,
            ),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=ServerConfig(url=_TEST_SERVER),
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(
                app,
                ["--api-key", "my-secret-key", "health"],  # noqa: S106
            )
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER,
                username="",
                verify_ssl=True,
                api_key="my-secret-key",  # pragma: allowlist secret
            )

    def test_api_key_from_config(self):
        """api_key from config should be used when --api-key is not given."""
        cfg = ServerConfig(
            url=_TEST_SERVER,
            api_key="config-key",  # pragma: allowlist secret
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(app, ["health"])
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER,
                username="",
                verify_ssl=True,
                api_key="config-key",  # pragma: allowlist secret
            )

    def test_cli_api_key_overrides_config(self):
        """CLI --api-key should override config api_key."""
        cfg = ServerConfig(
            url=_TEST_SERVER,
            api_key="config-key",  # pragma: allowlist secret
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=cfg,
            ),
            patch("jenkins_job_insight.cli.main.JJIClient") as mock_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.health.return_value = {"status": "healthy"}
            mock_cls.return_value = mock_instance
            result = runner.invoke(
                app,
                ["--api-key", "cli-key", "health"],  # noqa: S106
            )
            assert result.exit_code == 0
            mock_cls.assert_called_once_with(
                server_url=_TEST_SERVER,
                username="",
                verify_ssl=True,
                api_key="cli-key",  # pragma: allowlist secret
            )
