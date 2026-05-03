"""Tests for repository management."""

from unittest.mock import MagicMock, patch

import git.exc
import pytest
from git.exc import GitCommandError

from jenkins_job_insight.repository import (
    RepositoryManager,
    _is_commit_sha,
    repo_name_from_url,
)


class TestRepositoryManager:
    """Tests for the RepositoryManager class."""

    def test_init_empty_temp_dirs(self) -> None:
        """Test that RepositoryManager initializes with empty temp_dirs list."""
        manager = RepositoryManager()
        assert manager.temp_dirs == []

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_creates_temp_directory(self, mock_repo: MagicMock) -> None:
        """Test that clone creates a temporary directory."""
        manager = RepositoryManager()
        repo_url = "https://github.com/example/repo"

        result = manager.clone(repo_url)

        assert result.exists()
        assert len(manager.temp_dirs) == 1
        assert manager.temp_dirs[0] == result
        mock_repo.clone_from.assert_called_once()

        # Cleanup
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_custom_depth(self, mock_repo: MagicMock) -> None:
        """Test that clone passes correct depth parameter."""
        manager = RepositoryManager()
        repo_url = "https://github.com/example/repo"
        depth = 100

        manager.clone(repo_url, depth=depth)

        mock_repo.clone_from.assert_called_once()
        call_kwargs = mock_repo.clone_from.call_args
        assert call_kwargs[1]["depth"] == depth

        # Cleanup
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_multiple_repos(self, mock_repo: MagicMock) -> None:
        """Test cloning multiple repositories."""
        manager = RepositoryManager()

        path1 = manager.clone("https://github.com/example/repo1")
        path2 = manager.clone("https://github.com/example/repo2")

        assert len(manager.temp_dirs) == 2
        assert path1 != path2
        assert path1 in manager.temp_dirs
        assert path2 in manager.temp_dirs
        assert mock_repo.clone_from.call_count == 2

        # Cleanup
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_cleanup_removes_directories(self, _mock_repo: MagicMock) -> None:
        """Test that cleanup removes all temporary directories."""
        manager = RepositoryManager()

        path1 = manager.clone("https://github.com/example/repo1")
        path2 = manager.clone("https://github.com/example/repo2")

        # Directories should exist before cleanup
        assert path1.exists()
        assert path2.exists()

        manager.cleanup()

        # Directories should be removed after cleanup
        assert not path1.exists()
        assert not path2.exists()
        assert manager.temp_dirs == []

    @patch("jenkins_job_insight.repository.Repo")
    def test_cleanup_handles_missing_directory(self, _mock_repo: MagicMock) -> None:
        """Test that cleanup handles already deleted directories gracefully."""
        manager = RepositoryManager()

        path = manager.clone("https://github.com/example/repo")

        # Manually delete the directory
        path.rmdir()

        # Cleanup should not raise an error
        manager.cleanup()
        assert manager.temp_dirs == []

    @patch("jenkins_job_insight.repository.Repo")
    def test_context_manager_enter(self, _mock_repo: MagicMock) -> None:
        """Test context manager __enter__ returns self."""
        manager = RepositoryManager()

        with manager as ctx:
            assert ctx is manager

    @patch("jenkins_job_insight.repository.Repo")
    def test_context_manager_cleanup_on_exit(self, _mock_repo: MagicMock) -> None:
        """Test context manager cleans up on exit."""
        path = None
        with RepositoryManager() as manager:
            path = manager.clone("https://github.com/example/repo")
            assert path.exists()

        # Directory should be cleaned up after context manager exits
        assert not path.exists()

    @patch("jenkins_job_insight.repository.Repo")
    def test_context_manager_cleanup_on_exception(self, _mock_repo: MagicMock) -> None:
        """Test context manager cleans up even when exception occurs."""
        path = None
        try:
            with RepositoryManager() as manager:
                path = manager.clone("https://github.com/example/repo")
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Directory should be cleaned up after exception
        assert path is not None
        assert not path.exists()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_uses_jenkins_insight_prefix(self, _mock_repo: MagicMock) -> None:
        """Test that cloned directories are under jenkins-insight base path."""
        manager = RepositoryManager()

        path = manager.clone("https://github.com/example/repo")

        # The path should be under the jenkins-insight base directory
        assert "jenkins-insight" in str(path)
        assert path.parent.name == "jenkins-insight"

        # Cleanup
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_rejects_file_url(self, _mock_repo: MagicMock) -> None:
        """Test that clone rejects file:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone("file:///etc/passwd")

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_rejects_ssh_url(self, _mock_repo: MagicMock) -> None:
        """Test that clone rejects ssh:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone("ssh://git@github.com/org/repo")

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_real_failure_handling(self, mock_repo: MagicMock) -> None:
        """Test that clone raises error for invalid repository."""
        mock_repo.clone_from.side_effect = git.exc.GitCommandError("clone", 128)
        manager = RepositoryManager()

        with pytest.raises(git.exc.GitCommandError):
            manager.clone("https://invalid-url-that-does-not-exist.example.com/repo")

        # Even on failure, cleanup should work
        manager.cleanup()


class TestCloneInto:
    """Tests for RepositoryManager.clone_into."""

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_creates_directory(self, mock_repo: MagicMock, tmp_path) -> None:
        """Test that clone_into creates the target directory and clones."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        result = manager.clone_into("https://github.com/org/repo", target, depth=1)
        assert result == target
        assert target.exists()
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_not_tracked_for_cleanup(
        self, _mock_repo: MagicMock, tmp_path
    ) -> None:
        """Test that clone_into does NOT add directory to temp_dirs."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into("https://github.com/org/repo", target)
        assert target not in manager.temp_dirs

    def test_clone_into_rejects_file_url(self, tmp_path) -> None:
        """Test that clone_into rejects file:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone_into("file:///etc/passwd", tmp_path / "repo")

    def test_clone_into_rejects_ssh_url(self, tmp_path) -> None:
        """Test that clone_into rejects ssh:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone_into("ssh://git@github.com/org/repo", tmp_path / "repo")


class TestCloneWithSslRetry:
    """Tests for SSL certificate retry logic."""

    def test_clone_into_retries_on_ssl_error(self, tmp_path) -> None:
        """clone_into retries with GIT_SSL_NO_VERIFY on cert failure."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="server verification failed: certificate signer not trusted",
                ),
                MagicMock(),
            ]
            target = tmp_path / "repo"
            target.mkdir()
            manager.clone_into("https://example.com/repo", target)
            assert mock_clone.call_count == 2
            _, kwargs = mock_clone.call_args
            assert kwargs.get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"

    def test_clone_into_does_not_retry_on_other_errors(self, tmp_path) -> None:
        """Non-SSL errors are not retried."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = GitCommandError(
                "git clone", 128, stderr="repository not found"
            )
            with pytest.raises(GitCommandError):
                manager.clone_into("https://example.com/repo", tmp_path / "repo")
            assert mock_clone.call_count == 1

    def test_clone_into_succeeds_without_retry(self, tmp_path) -> None:
        """Successful clone doesn't trigger retry."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.return_value = MagicMock()
            target = tmp_path / "repo"
            target.mkdir()
            manager.clone_into("https://example.com/repo", target)
            assert mock_clone.call_count == 1

    def test_clone_retries_on_ssl_error(self, tmp_path) -> None:
        """clone() retries with GIT_SSL_NO_VERIFY on SSL cert failure."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="SSL certificate problem: unable to get local issuer certificate",
                ),
                MagicMock(),
            ]
            manager.clone("https://example.com/repo")
            assert mock_clone.call_count == 2
            _, kwargs = mock_clone.call_args
            assert kwargs.get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"
        manager.cleanup()

    def test_clone_does_not_retry_on_other_errors(self) -> None:
        """clone() non-SSL errors are not retried."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = GitCommandError(
                "git clone", 128, stderr="repository not found"
            )
            with pytest.raises(GitCommandError):
                manager.clone("https://example.com/repo")
            assert mock_clone.call_count == 1
        manager.cleanup()

    def test_clone_retries_on_certificate_keyword(self) -> None:
        """clone() retries when stderr contains 'certificate'."""
        manager = RepositoryManager()
        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="fatal: unable to access: certificate verify failed",
                ),
                MagicMock(),
            ]
            manager.clone("https://example.com/repo")
            assert mock_clone.call_count == 2
        manager.cleanup()


class TestCreateWorkspace:
    """Tests for RepositoryManager.create_workspace."""

    def test_create_workspace_returns_path(self) -> None:
        """create_workspace returns a Path under base_path."""
        manager = RepositoryManager()
        workspace = manager.create_workspace()
        assert workspace.exists()
        assert workspace.parent == manager.base_path
        assert "workspace-" in workspace.name
        manager.cleanup()

    def test_create_workspace_tracked_for_cleanup(self) -> None:
        """create_workspace adds path to temp_dirs."""
        manager = RepositoryManager()
        workspace = manager.create_workspace()
        assert workspace in manager.temp_dirs
        manager.cleanup()
        assert not workspace.exists()


class TestRepoNameFromUrl:
    """Tests for the repo_name_from_url utility function."""

    def test_https_url_with_git_suffix(self) -> None:
        """Extract repo name from HTTPS URL ending in .git."""
        assert repo_name_from_url("https://github.com/org/my-repo.git") == "my-repo"

    def test_https_url_without_git_suffix(self) -> None:
        """Extract repo name from HTTPS URL without .git."""
        assert repo_name_from_url("https://github.com/org/my-repo") == "my-repo"

    def test_url_with_trailing_slash(self) -> None:
        """Strip trailing slash before extracting."""
        assert repo_name_from_url("https://github.com/org/my-repo/") == "my-repo"

    def test_url_with_trailing_slash_and_git(self) -> None:
        """Strip trailing slash and .git."""
        assert repo_name_from_url("https://github.com/org/my-repo.git/") == "my-repo"

    def test_git_protocol_url(self) -> None:
        """Extract from git:// URL."""
        assert repo_name_from_url("git://github.com/org/my-repo.git") == "my-repo"

    def test_preserves_git_in_middle(self) -> None:
        """Preserve .git when it appears in the middle of the name."""
        assert (
            repo_name_from_url("https://github.com/org/my.git-tools.git")
            == "my.git-tools"
        )

    def test_preserves_git_in_middle_no_suffix(self) -> None:
        """Preserve .git in middle when there is no trailing .git suffix."""
        assert (
            repo_name_from_url("https://github.com/org/my.git-tools") == "my.git-tools"
        )

    def test_pydantic_httpurl(self) -> None:
        """Works with pydantic HttpUrl objects."""
        from pydantic import HttpUrl

        url = HttpUrl("https://github.com/org/my-repo.git")
        assert repo_name_from_url(url) == "my-repo"


class TestDeriveTestRepoName:
    """Tests for derive_test_repo_name: name collision detection."""

    def test_no_collision(self) -> None:
        """When no additional repos, returns derived name from URL."""
        from jenkins_job_insight.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/my-tests.git", [])
        assert name == "my-tests"

    def test_collision_falls_back(self) -> None:
        """When derived name collides with an additional repo, returns 'tests-repo-1'."""
        from jenkins_job_insight.repository import derive_test_repo_name
        from jenkins_job_insight.models import AdditionalRepo

        additional = [
            AdditionalRepo.model_validate(
                {"name": "my-tests", "url": "https://github.com/org/my-tests"}
            ),
        ]
        name = derive_test_repo_name("https://github.com/org/my-tests.git", additional)
        assert name == "tests-repo-1"

    def test_no_collision_different_name(self) -> None:
        """No collision when additional repo has a different name."""
        from jenkins_job_insight.repository import derive_test_repo_name
        from jenkins_job_insight.models import AdditionalRepo

        additional = [
            AdditionalRepo.model_validate(
                {"name": "infra", "url": "https://github.com/org/infra"}
            ),
        ]
        name = derive_test_repo_name("https://github.com/org/my-tests.git", additional)
        assert name == "my-tests"

    def test_empty_additional_repos(self) -> None:
        """Empty additional repos list causes no collision."""
        from jenkins_job_insight.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/repo", [])
        assert name == "repo"

    def test_none_additional_repos(self) -> None:
        """None additional repos list causes no collision."""
        from jenkins_job_insight.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/repo", None)
        assert name == "repo"

    def test_fallback_also_avoids_collision(self) -> None:
        """Fallback name avoids collision with 'tests-repo-1' too."""
        from jenkins_job_insight.repository import derive_test_repo_name
        from jenkins_job_insight.models import AdditionalRepo

        repos = [
            AdditionalRepo.model_validate(
                {"name": "my-repo", "url": "https://github.com/org/my-repo"}
            ),
            AdditionalRepo.model_validate(
                {"name": "tests-repo-1", "url": "https://github.com/org/other"}
            ),
        ]
        result = derive_test_repo_name("https://github.com/org/my-repo", repos)
        assert result != "my-repo"
        assert result != "tests-repo-1"
        assert result.startswith("tests-repo-")

    def test_reserved_name_falls_back(self) -> None:
        """Test repo URL with reserved basename gets a fallback name."""
        from jenkins_job_insight.repository import derive_test_repo_name

        result = derive_test_repo_name("https://github.com/org/build-artifacts", [])
        assert result != "build-artifacts"
        assert result.startswith("tests-repo-")

    def test_reserved_name_falls_back_with_none_additional(self) -> None:
        """Reserved name is blocked even when additional_repos is None."""
        from jenkins_job_insight.repository import derive_test_repo_name

        result = derive_test_repo_name("https://github.com/org/build-artifacts", None)
        assert result != "build-artifacts"
        assert result.startswith("tests-repo-")

    def test_uuid_fallback_logs_warning(self) -> None:
        """When all 99 numeric candidates are taken, UUID fallback logs a warning."""
        from jenkins_job_insight.repository import derive_test_repo_name
        from jenkins_job_insight.models import AdditionalRepo

        # Create additional repos that collide with the base name and all 99 numeric fallbacks
        repos = [
            AdditionalRepo.model_validate(
                {"name": "my-repo", "url": "https://github.com/org/my-repo"}
            ),
        ]
        for i in range(1, 100):
            repos.append(
                AdditionalRepo.model_validate(
                    {"name": f"tests-repo-{i}", "url": f"https://github.com/org/r{i}"}
                ),
            )

        with patch("jenkins_job_insight.repository.logger") as mock_logger:
            result = derive_test_repo_name("https://github.com/org/my-repo", repos)

        assert result.startswith("tests-repo-")
        # Should NOT match tests-repo-N where N is 1..99
        assert result not in {f"tests-repo-{i}" for i in range(1, 100)}
        mock_logger.warning.assert_called_once()
        assert "exhausted" in mock_logger.warning.call_args[0][0].lower()


class TestCloneWithSslRetryCleanup:
    """Tests for Finding 2: partial clone cleanup before SSL retry."""

    def test_partial_clone_cleaned_before_retry(self, tmp_path) -> None:
        """After a failed SSL clone, the target dir is cleaned before retry."""
        manager = RepositoryManager()
        target = tmp_path / "repo"
        target.mkdir()
        # Create a partial file in the target to simulate partial clone
        partial_file = target / "partial-object"
        partial_file.write_text("partial data")

        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="server verification failed: certificate signer not trusted",
                ),
                MagicMock(),
            ]
            manager.clone_into("https://example.com/repo", target)

        # The partial file should have been removed before the retry
        assert not partial_file.exists()
        # Target dir should still exist (recreated for the retry)
        assert target.exists()


class TestRedactUrl:
    """Tests for _redact_url credential stripping."""

    def test_no_credentials(self) -> None:
        """URL without credentials is returned unchanged."""
        from jenkins_job_insight.repository import _redact_url

        assert (
            _redact_url("https://github.com/org/repo") == "https://github.com/org/repo"
        )

    def test_username_and_password_redacted(self) -> None:
        """Username and password are replaced with ***."""
        from jenkins_job_insight.repository import _redact_url

        result = _redact_url("https://user:pass@github.com/org/repo")
        assert "user" not in result
        assert "pass" not in result
        assert "***@github.com" in result

    def test_username_only_redacted(self) -> None:
        """Username-only auth is redacted."""
        from jenkins_job_insight.repository import _redact_url

        result = _redact_url("https://token@github.com/org/repo")
        assert "token" not in result
        assert "***@github.com" in result

    def test_preserves_port(self) -> None:
        """Port is preserved when credentials are redacted."""
        from jenkins_job_insight.repository import _redact_url

        result = _redact_url("https://user:pass@github.com:8443/org/repo")
        assert ":8443" in result
        assert "user" not in result

    def test_git_protocol(self) -> None:
        """git:// URLs without credentials are unchanged."""
        from jenkins_job_insight.repository import _redact_url

        assert _redact_url("git://github.com/org/repo") == "git://github.com/org/repo"


class TestCloneBranchParameter:
    """Tests for passing branch parameter through clone functions."""

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_ssl_retry_passes_branch(
        self, mock_repo: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry passes branch to Repo.clone_from when non-empty."""
        from jenkins_job_insight.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry(
            "https://example.com/repo", clone_dir, depth=1, branch="develop"
        )
        mock_repo.clone_from.assert_called_once_with(
            "https://example.com/repo", clone_dir, depth=1, branch="develop"
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_ssl_retry_no_branch_when_empty(
        self, mock_repo: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry does NOT pass branch when empty string."""
        from jenkins_job_insight.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry("https://example.com/repo", clone_dir, depth=1, branch="")
        mock_repo.clone_from.assert_called_once_with(
            "https://example.com/repo", clone_dir, depth=1
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_ssl_retry_default_no_branch(
        self, mock_repo: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry without branch arg does not pass branch."""
        from jenkins_job_insight.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry("https://example.com/repo", clone_dir, depth=1)
        mock_repo.clone_from.assert_called_once_with(
            "https://example.com/repo", clone_dir, depth=1
        )

    def test_clone_with_ssl_retry_passes_branch_on_ssl_retry(self, tmp_path) -> None:
        """branch is passed in BOTH the initial and SSL retry Repo.clone_from calls."""
        from jenkins_job_insight.repository import _clone_with_ssl_retry

        with patch("jenkins_job_insight.repository.Repo.clone_from") as mock_clone:
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="SSL certificate problem",
                ),
                MagicMock(),
            ]
            target = tmp_path / "repo"
            target.mkdir()
            _clone_with_ssl_retry(
                "https://example.com/repo", target, depth=1, branch="main"
            )

            assert mock_clone.call_count == 2
            # First call should have branch
            first_call = mock_clone.call_args_list[0]
            assert first_call[1].get("branch") == "main"
            # Second (retry) call should also have branch
            second_call = mock_clone.call_args_list[1]
            assert second_call[1].get("branch") == "main"
            assert second_call[1].get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_passes_branch(self, mock_repo: MagicMock, tmp_path) -> None:
        """clone_into forwards branch to _clone_with_ssl_retry."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into(
            "https://github.com/org/repo", target, depth=1, branch="feature-x"
        )
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1, branch="feature-x"
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_no_branch_when_empty(
        self, mock_repo: MagicMock, tmp_path
    ) -> None:
        """clone_into does not pass branch when empty string."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into("https://github.com/org/repo", target, depth=1, branch="")
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_passes_branch(self, mock_repo: MagicMock) -> None:
        """clone forwards branch to _clone_with_ssl_retry."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="release-1.0")
        mock_repo.clone_from.assert_called_once()
        call_kwargs = mock_repo.clone_from.call_args[1]
        assert call_kwargs["branch"] == "release-1.0"
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_no_branch_when_empty(self, mock_repo: MagicMock) -> None:
        """clone does not pass branch when empty string."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="")
        mock_repo.clone_from.assert_called_once()
        call_kwargs = mock_repo.clone_from.call_args[1]
        assert "branch" not in call_kwargs
        manager.cleanup()


class TestInjectTokenIntoUrl:
    """Tests for _inject_token_into_url helper."""

    def test_injects_token_into_https_url(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://github.com/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        expected = "https://x-token-auth:tok123@github.com/org/repo"  # pragma: allowlist secret
        assert result == expected

    def test_replaces_existing_credentials(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://user:pass@github.com/org/repo",  # pragma: allowlist secret
            "new_token",  # pragma: allowlist secret
        )
        assert "user" not in result
        assert "pass" not in result
        assert "x-token-auth:new_token@github.com" in result  # pragma: allowlist secret

    def test_preserves_port(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://gitlab.internal:8443/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        assert (
            "x-token-auth:tok123@gitlab.internal:8443" in result
        )  # pragma: allowlist secret

    def test_ignores_non_https_url(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "git://github.com/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        assert result == "git://github.com/org/repo"

    def test_empty_token_returns_original(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url("https://github.com/org/repo", "")
        assert result == "https://github.com/org/repo"

    def test_preserves_path_and_suffix(self) -> None:
        from jenkins_job_insight.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://github.com/org/repo.git",
            "tok",  # pragma: allowlist secret
        )
        expected = "https://x-token-auth:tok@github.com/org/repo.git"  # pragma: allowlist secret
        assert result == expected


class TestIsCommitSha:
    """Tests for _is_commit_sha utility function."""

    def test_full_40_char_sha(self) -> None:
        assert _is_commit_sha("a" * 40) is True

    def test_short_7_char_sha(self) -> None:
        assert _is_commit_sha("abc1234") is True

    def test_mixed_case_sha(self) -> None:
        assert (
            _is_commit_sha(
                "aBcDeF1234567890aBcDeF1234567890aBcDeF12"  # pragma: allowlist secret
            )
            is True
        )

    def test_20_char_sha(self) -> None:
        assert (
            _is_commit_sha("1234567890abcdef1234") is True  # pragma: allowlist secret
        )

    def test_too_short_6_chars(self) -> None:
        assert _is_commit_sha("abc123") is False

    def test_too_long_41_chars(self) -> None:
        assert _is_commit_sha("a" * 41) is False

    def test_non_hex_chars(self) -> None:
        assert _is_commit_sha("xyz1234") is False

    def test_branch_name(self) -> None:
        assert _is_commit_sha("main") is False

    def test_branch_with_slash(self) -> None:
        assert _is_commit_sha("feature/my-branch") is False

    def test_tag_name(self) -> None:
        assert _is_commit_sha("v1.2.3") is False

    def test_empty_string(self) -> None:
        assert _is_commit_sha("") is False

    def test_release_branch(self) -> None:
        assert _is_commit_sha("release-1.8") is False


class TestCloneShaFlow:
    """Tests for SHA-based clone flow (full clone + checkout)."""

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_sha_uses_full_clone_and_checkout(
        self, mock_repo_cls: MagicMock
    ) -> None:
        """clone() with a SHA ref does full clone (no depth/branch) then checkout."""
        manager = RepositoryManager()
        sha = "abc1234def5678"  # pragma: allowlist secret
        mock_instance = MagicMock()
        mock_repo_cls.return_value = mock_instance

        manager.clone("https://github.com/org/repo", depth=50, branch=sha)

        # clone_from should be called WITHOUT depth and branch
        mock_repo_cls.clone_from.assert_called_once()
        call_kwargs = mock_repo_cls.clone_from.call_args[1]
        assert "depth" not in call_kwargs
        assert "branch" not in call_kwargs
        # checkout should be called on the repo instance
        mock_instance.git.checkout.assert_called_once_with(sha)
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_with_branch_uses_depth_and_branch(
        self, mock_repo_cls: MagicMock
    ) -> None:
        """clone() with a branch name uses --depth and --branch as before."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="release-1.8")

        mock_repo_cls.clone_from.assert_called_once()
        call_kwargs = mock_repo_cls.clone_from.call_args[1]
        assert call_kwargs["depth"] == 50
        assert call_kwargs["branch"] == "release-1.8"
        # No checkout call (Repo() not instantiated for checkout)
        mock_repo_cls.return_value.git.checkout.assert_not_called()
        manager.cleanup()

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_with_sha_uses_full_clone_and_checkout(
        self, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a SHA does full clone then checkout."""
        manager = RepositoryManager()
        sha = "deadbeef1234567"  # pragma: allowlist secret
        target = tmp_path / "my-repo"
        mock_instance = MagicMock()
        mock_repo_cls.return_value = mock_instance

        manager.clone_into("https://github.com/org/repo", target, depth=1, branch=sha)

        # clone_from without depth/branch
        mock_repo_cls.clone_from.assert_called_once()
        call_kwargs = mock_repo_cls.clone_from.call_args[1]
        assert "depth" not in call_kwargs
        assert "branch" not in call_kwargs
        # checkout with SHA
        mock_instance.git.checkout.assert_called_once_with(sha)

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_with_branch_uses_depth_and_branch(
        self, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a branch name preserves existing behavior."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"

        manager.clone_into(
            "https://github.com/org/repo", target, depth=1, branch="develop"
        )

        mock_repo_cls.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1, branch="develop"
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_sha_with_token_scrubs_credentials(
        self, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with SHA + token still scrubs credentials."""
        manager = RepositoryManager()
        sha = "abc1234def5678"  # pragma: allowlist secret
        target = tmp_path / "my-repo"
        mock_instance = MagicMock()
        mock_repo_cls.return_value = mock_instance

        # Create .git directory to trigger scrubbing
        target.mkdir(parents=True)
        (target / ".git").mkdir()

        with (
            patch("shutil.which", return_value="/usr/bin/git"),
            patch("jenkins_job_insight.repository.subprocess") as mock_subprocess,
        ):
            mock_run = mock_subprocess.run
            manager.clone_into(
                "https://github.com/org/repo",
                target,
                depth=1,
                branch=sha,
                token="tok123",  # noqa: S106  # pragma: allowlist secret
            )

        # Token injected in clone URL
        clone_url = mock_repo_cls.clone_from.call_args[0][0]
        assert "tok123" in clone_url  # pragma: allowlist secret
        # Credentials scrubbed
        mock_run.assert_called_once()
        scrub_cmd = mock_run.call_args[0][0]
        assert scrub_cmd[-1] == "https://github.com/org/repo"

    def test_clone_into_sha_with_ssl_retry(self, tmp_path) -> None:
        """clone_into() with SHA retries on SSL error."""
        manager = RepositoryManager()
        sha = "abc1234def5678"  # pragma: allowlist secret
        target = tmp_path / "repo"
        target.mkdir()

        mock_instance = MagicMock()
        with patch("jenkins_job_insight.repository.Repo") as mock_repo_cls:
            mock_repo_cls.return_value = mock_instance
            mock_repo_cls.clone_from = MagicMock()
            mock_clone = mock_repo_cls.clone_from
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="server verification failed: certificate signer not trusted",
                ),
                MagicMock(),
            ]
            manager.clone_into("https://example.com/repo", target, branch=sha)

            assert mock_clone.call_count == 2
            # Both calls should have no depth/branch
            for call in mock_clone.call_args_list:
                assert "depth" not in call[1]
                assert "branch" not in call[1]
            # Retry should have SSL disabled
            _, kwargs = mock_clone.call_args
            assert kwargs.get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"
            # Checkout should still happen
            mock_instance.git.checkout.assert_called_once_with(sha)


class TestCloneIntoWithToken:
    """Tests for clone_into with token parameter."""

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_with_token_injects_into_url(
        self, mock_repo: MagicMock, tmp_path
    ) -> None:
        """clone_into with token injects it into the clone URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into(
            "https://github.com/org/repo",
            target,
            depth=1,
            token="tok123",  # noqa: S106  # pragma: allowlist secret
        )
        mock_repo.clone_from.assert_called_once_with(
            "https://x-token-auth:tok123@github.com/org/repo",  # pragma: allowlist secret
            target,
            depth=1,
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_without_token(self, mock_repo: MagicMock, tmp_path) -> None:
        """clone_into without token uses original URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into("https://github.com/org/repo", target, depth=1)
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1
        )

    @patch("jenkins_job_insight.repository.Repo")
    def test_clone_into_with_none_token(self, mock_repo: MagicMock, tmp_path) -> None:
        """clone_into with None token uses original URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        manager.clone_into("https://github.com/org/repo", target, depth=1, token=None)
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/org/repo", target, depth=1
        )
