"""Tests for repository management."""

import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import git.exc
import pytest
from git.exc import GitCommandError

from rootcoz.repository import (
    CloneRefUnavailableError,
    RepositoryManager,
    _clone_by_ref_type,
    _is_commit_sha,
    _is_full_commit_sha,
    repo_name_from_url,
)
from rootcoz.url_utils import sanitize_clone_url


def _run_git(cwd: Path, *args: str, stdin: str = "") -> str:
    """Run one git plumbing command in *cwd* and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _pin_protocol(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Pin the fetch protocol version via git's own config-from-env.

    Not a fixture of the image's git version, and v0 is the deterministic local
    way to produce a server that refuses an object id.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.version")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", version)


@pytest.fixture
def ref_repo(tmp_path: Path) -> dict:
    """A bare repo with a real history, a 40-hex branch name and a commit on no ref."""
    bare = tmp_path / "served.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    shas: list[str] = []
    for index in range(60):
        blob = _run_git(bare, "hash-object", "-w", "--stdin", stdin=f"line {index}\n")
        tree = _run_git(bare, "mktree", stdin=f"100644 blob {blob}\tfile-{index}.txt\n")
        parents = ["-p", shas[-1]] if shas else []
        shas.append(
            _run_git(bare, "commit-tree", tree, *parents, "-m", f"commit {index}")
        )
    _run_git(bare, "update-ref", "refs/heads/main", shas[-1])
    _run_git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    hex_branch = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    _run_git(bare, "update-ref", f"refs/heads/{hex_branch}", shas[58])
    blob = _run_git(bare, "hash-object", "-w", "--stdin", stdin="dangling\n")
    tree = _run_git(bare, "mktree", stdin=f"100644 blob {blob}\tdangling.txt\n")
    dangling = _run_git(
        bare, "commit-tree", tree, "-p", shas[-1], "-m", "on no ref at all"
    )
    return {
        "url": f"file://{bare}",
        "shas": shas,
        "hex_branch": hex_branch,
        "dangling": dangling,
    }


def _init_creating_leaf(mock_repo_cls: MagicMock):
    """Make the mocked ``Repo.init`` create what ``git init`` creates."""

    def init(directory, **_kwargs):
        (Path(directory) / ".git").mkdir(parents=True, exist_ok=True)
        return mock_repo_cls.init.return_value

    return init


def staging_of(target: Path) -> Path:
    """Where ``clone_into`` clones before renaming onto ``target``."""
    return target.parent / ".staging" / target.name


def clone_fails_then_succeeds(error: Exception):
    """A ``git clone`` stand-in that raises ``error`` once, then clones.

    An iterable ``side_effect`` yields *return values*, it does not call them, so
    the retry path needs a callable to create the staging leaf.
    """

    def clone(url, directory, **_kwargs) -> None:
        if not clone.failed:  # type: ignore[attr-defined]
            clone.failed = True  # type: ignore[attr-defined]
            raise error
        clone_into_staging(url, directory)

    clone.failed = False  # type: ignore[attr-defined]
    return clone


def clone_into_staging(url, directory, **_kwargs) -> MagicMock:
    """A ``git clone`` stand-in that creates the staging leaf it clones into.

    ``clone_into`` never pre-creates the repository directory -- only its
    ``.staging`` parent -- so every fake has to.
    """
    Path(directory).mkdir(parents=True, exist_ok=True)
    return MagicMock()


class TestRepositoryManager:
    """Tests for the RepositoryManager class."""

    def test_init_empty_temp_dirs(self) -> None:
        """Test that RepositoryManager initializes with empty temp_dirs list."""
        manager = RepositoryManager()
        assert manager.temp_dirs == []

    @patch("rootcoz.repository.Git")
    def test_clone_creates_temp_directory(self, mock_git: MagicMock) -> None:
        """Test that clone creates a temporary directory."""
        manager = RepositoryManager()
        repo_url = "https://github.com/example/repo"

        result = manager.clone(repo_url)

        assert result.exists()
        assert len(manager.temp_dirs) == 1
        assert manager.temp_dirs[0] == result
        mock_git.return_value.clone.assert_called_once()

        # Cleanup
        manager.cleanup()

    @patch("rootcoz.repository.Git")
    def test_clone_with_custom_depth(self, mock_git: MagicMock) -> None:
        """Test that clone passes correct depth parameter."""
        manager = RepositoryManager()
        repo_url = "https://github.com/example/repo"
        depth = 100

        manager.clone(repo_url, depth=depth)

        mock_git.return_value.clone.assert_called_once()
        call_kwargs = mock_git.return_value.clone.call_args
        assert call_kwargs[1]["depth"] == depth

        # Cleanup
        manager.cleanup()

    @patch("rootcoz.repository.Git")
    def test_clone_multiple_repos(self, mock_git: MagicMock) -> None:
        """Test cloning multiple repositories."""
        manager = RepositoryManager()

        path1 = manager.clone("https://github.com/example/repo1")
        path2 = manager.clone("https://github.com/example/repo2")

        assert len(manager.temp_dirs) == 2
        assert path1 != path2
        assert path1 in manager.temp_dirs
        assert path2 in manager.temp_dirs
        assert mock_git.return_value.clone.call_count == 2

        # Cleanup
        manager.cleanup()

    @patch("rootcoz.repository.Git")
    def test_cleanup_removes_directories(self, _mock_git: MagicMock) -> None:
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

    @patch("rootcoz.repository.Git")
    def test_cleanup_handles_missing_directory(self, _mock_git: MagicMock) -> None:
        """Test that cleanup handles already deleted directories gracefully."""
        manager = RepositoryManager()

        path = manager.clone("https://github.com/example/repo")

        # Manually delete the directory
        path.rmdir()

        # Cleanup should not raise an error
        manager.cleanup()
        assert manager.temp_dirs == []

    def test_cleanup_keeps_a_dir_it_could_not_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial cleanup keeps the survivor tracked so the next one retries it."""
        import errno
        import os

        manager = RepositoryManager()
        kept = tmp_path / "kept"
        gone = tmp_path / "gone"
        kept.mkdir()
        gone.mkdir()
        (kept / "stuck.txt").write_text("x")
        (gone / "ok.txt").write_text("x")
        manager.temp_dirs.extend([kept, gone])
        real_unlink = os.unlink

        def refuse_stuck(path, *args, **kwargs):
            if Path(os.fspath(path)).name == "stuck.txt":
                raise PermissionError(
                    errno.EACCES, "Permission denied", os.fspath(path)
                )
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", refuse_stuck)

        result = manager.cleanup()

        assert result.failed >= 1
        assert manager.temp_dirs == [kept], "a failed dir must stay tracked for retry"
        assert not gone.exists()

    @patch("rootcoz.repository.Git")
    def test_context_manager_enter(self, _mock_git: MagicMock) -> None:
        """Test context manager __enter__ returns self."""
        manager = RepositoryManager()

        with manager as ctx:
            assert ctx is manager

    @patch("rootcoz.repository.Git")
    def test_context_manager_cleanup_on_exit(self, _mock_git: MagicMock) -> None:
        """Test context manager cleans up on exit."""
        path = None
        with RepositoryManager() as manager:
            path = manager.clone("https://github.com/example/repo")
            assert path.exists()

        # Directory should be cleaned up after context manager exits
        assert not path.exists()

    @patch("rootcoz.repository.Git")
    def test_context_manager_cleanup_on_exception(self, _mock_git: MagicMock) -> None:
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

    @patch("rootcoz.repository.Git")
    def test_clone_uses_rootcoz_temp_prefix(self, _mock_git: MagicMock) -> None:
        """Test that cloned directories are under the rootcoz temp base path."""
        manager = RepositoryManager()

        path = manager.clone("https://github.com/example/repo")

        assert path.parent.name == "rootcoz"

        # Cleanup
        manager.cleanup()

    @patch("rootcoz.repository.Git")
    def test_clone_rejects_file_url(self, _mock_git: MagicMock) -> None:
        """Test that clone rejects file:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone("file:///etc/passwd")

    @patch("rootcoz.repository.Git")
    def test_clone_rejects_ssh_url(self, _mock_git: MagicMock) -> None:
        """Test that clone rejects ssh:// URLs."""
        manager = RepositoryManager()
        with pytest.raises(ValueError, match="Only https:// and git://"):
            manager.clone("ssh://git@github.com/org/repo")

    @patch("rootcoz.repository.Git")
    def test_clone_real_failure_handling(self, mock_git: MagicMock) -> None:
        """Test that clone raises error for invalid repository."""
        mock_git.return_value.clone.side_effect = git.exc.GitCommandError("clone", 128)
        manager = RepositoryManager()

        with pytest.raises(git.exc.GitCommandError):
            manager.clone("https://invalid-url-that-does-not-exist.example.com/repo")

        # Even on failure, cleanup should work
        manager.cleanup()


class TestCloneInto:
    """Tests for RepositoryManager.clone_into."""

    @patch("rootcoz.repository.Git")
    def test_clone_into_creates_directory(self, mock_git: MagicMock, tmp_path) -> None:
        """The clone lands in the staging tree, then the target holds it."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        result = manager.clone_into("https://github.com/org/repo", target, depth=1)
        assert result == target
        assert target.exists()
        mock_git.return_value.clone.assert_called_once_with(
            "https://github.com/org/repo",
            str(staging_of(target)),
            depth=1,
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_into_not_tracked_for_cleanup(
        self, _mock_git: MagicMock, tmp_path
    ) -> None:
        """Test that clone_into does NOT add directory to temp_dirs."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        _mock_git.return_value.clone.side_effect = clone_into_staging
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
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
            mock_clone.side_effect = clone_fails_then_succeeds(
                GitCommandError(
                    "git clone",
                    128,
                    stderr="server verification failed: certificate signer not trusted",
                )
            )
            target = tmp_path / "repo"
            manager.clone_into("https://example.com/repo", target)
            assert mock_clone.call_count == 2
            assert target.exists()
            _, kwargs = mock_clone.call_args
            assert kwargs.get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"

    def test_clone_into_does_not_retry_on_other_errors(self, tmp_path) -> None:
        """Non-SSL errors are not retried."""
        manager = RepositoryManager()
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
            mock_clone.side_effect = GitCommandError(
                "git clone", 128, stderr="repository not found"
            )
            with pytest.raises(GitCommandError):
                manager.clone_into("https://example.com/repo", tmp_path / "repo")
            assert mock_clone.call_count == 1

    def test_clone_into_succeeds_without_retry(self, tmp_path) -> None:
        """Successful clone doesn't trigger retry."""
        manager = RepositoryManager()
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
            mock_clone.side_effect = clone_into_staging
            target = tmp_path / "repo"
            manager.clone_into("https://example.com/repo", target)
            assert mock_clone.call_count == 1
            assert target.exists()

    def test_clone_retries_on_ssl_error(self, tmp_path) -> None:
        """clone() retries with GIT_SSL_NO_VERIFY on SSL cert failure."""
        manager = RepositoryManager()
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
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
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
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
        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
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
        from rootcoz.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/my-tests.git", [])
        assert name == "my-tests"

    def test_collision_falls_back(self) -> None:
        """When derived name collides with an additional repo, returns 'tests-repo-1'."""
        from rootcoz.models import AdditionalRepo
        from rootcoz.repository import derive_test_repo_name

        additional = [
            AdditionalRepo.model_validate(
                {"name": "my-tests", "url": "https://github.com/org/my-tests"}
            ),
        ]
        name = derive_test_repo_name("https://github.com/org/my-tests.git", additional)
        assert name == "tests-repo-1"

    def test_no_collision_different_name(self) -> None:
        """No collision when additional repo has a different name."""
        from rootcoz.models import AdditionalRepo
        from rootcoz.repository import derive_test_repo_name

        additional = [
            AdditionalRepo.model_validate(
                {"name": "infra", "url": "https://github.com/org/infra"}
            ),
        ]
        name = derive_test_repo_name("https://github.com/org/my-tests.git", additional)
        assert name == "my-tests"

    def test_empty_additional_repos(self) -> None:
        """Empty additional repos list causes no collision."""
        from rootcoz.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/repo", [])
        assert name == "repo"

    def test_none_additional_repos(self) -> None:
        """None additional repos list causes no collision."""
        from rootcoz.repository import derive_test_repo_name

        name = derive_test_repo_name("https://github.com/org/repo", None)
        assert name == "repo"

    def test_fallback_also_avoids_collision(self) -> None:
        """Fallback name avoids collision with 'tests-repo-1' too."""
        from rootcoz.models import AdditionalRepo
        from rootcoz.repository import derive_test_repo_name

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
        from rootcoz.repository import derive_test_repo_name

        result = derive_test_repo_name("https://github.com/org/build-artifacts", [])
        assert result != "build-artifacts"
        assert result.startswith("tests-repo-")

    def test_reserved_name_falls_back_with_none_additional(self) -> None:
        """Reserved name is blocked even when additional_repos is None."""
        from rootcoz.repository import derive_test_repo_name

        result = derive_test_repo_name("https://github.com/org/build-artifacts", None)
        assert result != "build-artifacts"
        assert result.startswith("tests-repo-")

    def test_uuid_fallback_logs_warning(self) -> None:
        """When all 99 numeric candidates are taken, UUID fallback logs a warning."""
        from rootcoz.models import AdditionalRepo
        from rootcoz.repository import derive_test_repo_name

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

        with patch("rootcoz.repository.logger") as mock_logger:
            result = derive_test_repo_name("https://github.com/org/my-repo", repos)

        assert result.startswith("tests-repo-")
        # Should NOT match tests-repo-N where N is 1..99
        assert result not in {f"tests-repo-{i}" for i in range(1, 100)}
        mock_logger.warning.assert_called_once()
        assert "exhausted" in mock_logger.warning.call_args[0][0].lower()


class TestCloneWithSslRetryCleanup:
    """Tests for Finding 2: partial clone cleanup before SSL retry."""

    def test_partial_clone_cleaned_before_retry(self, tmp_path) -> None:
        """The SSL retry cleans the staging residue, not a caller's target.

        The failed clone lives in ``<workspace>/.staging/<name>`` now, so that is
        the tree the retry has to clear before cloning again.
        """
        manager = RepositoryManager()
        target = tmp_path / "repo"
        staging = staging_of(target)

        def clone(url, directory, **_kwargs) -> None:
            directory = Path(directory)
            if clone.retried:  # type: ignore[attr-defined]
                assert not (directory / "partial-object").exists(), (
                    "the retry cloned into the residue"
                )
                clone_into_staging(url, directory)
                return
            clone.retried = True  # type: ignore[attr-defined]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "partial-object").write_text("partial data")
            raise GitCommandError(
                "git clone",
                128,
                stderr="server verification failed: certificate signer not trusted",
            )

        clone.retried = False  # type: ignore[attr-defined]

        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_git_cls.return_value.clone.side_effect = clone
            manager.clone_into("https://example.com/repo", target)

        assert not staging.exists()
        assert target.exists()

    @pytest.mark.parametrize(
        "reported", [True, False], ids=["delete-reported-failed", "delete-no-op"]
    )
    def test_retry_refused_when_the_partial_clone_survives(
        self, tmp_path, reported: bool
    ) -> None:
        """A delete that left the tree behind must fail the retry, not clone into it.

        ``delete_tree`` reports failures instead of raising, so a dropped
        ``DeleteResult`` let the retry clone into the residue it could not remove
        (#424). Both halves of the guard are checked: a reported ``failed`` count
        and a delete that silently removed nothing.
        """
        from rootcoz.repository import DeleteResult, _clone_with_ssl_retry

        target = tmp_path / "repo"
        target.mkdir()
        residue = target / "residue"
        residue.write_text("stuck")
        result = DeleteResult(0, 1, 1) if reported else DeleteResult(0, 0, 0)
        delete = patch("rootcoz.repository.delete_tree", return_value=result)

        with (
            delete as mock_delete,
            patch("rootcoz.repository.Git") as mock_git_cls,
        ):
            mock_clone = mock_git_cls.return_value.clone
            mock_clone.side_effect = [
                GitCommandError(
                    "git clone",
                    128,
                    stderr="server verification failed: certificate signer not trusted",
                ),
                MagicMock(),
            ]
            with pytest.raises(OSError, match="could not be removed"):
                _clone_with_ssl_retry("https://example.com/repo", target)

        mock_delete.assert_called_once_with(target)
        assert mock_clone.call_count == 1, "retried the clone into the residue"
        assert residue.exists()


class TestRedactUrl:
    """Tests for redact_url credential stripping."""

    def test_no_credentials(self) -> None:
        """URL without credentials is returned unchanged."""
        from rootcoz.repository import redact_url

        assert (
            redact_url("https://github.com/org/repo") == "https://github.com/org/repo"
        )

    def test_username_and_password_redacted(self) -> None:
        """Username and password are replaced with ***."""
        from rootcoz.repository import redact_url

        result = redact_url("https://user:pass@github.com/org/repo")
        assert "user" not in result
        assert "pass" not in result
        assert "***@github.com" in result

    def test_username_only_redacted(self) -> None:
        """Username-only auth is redacted."""
        from rootcoz.repository import redact_url

        result = redact_url("https://token@github.com/org/repo")
        assert "token" not in result
        assert "***@github.com" in result

    def test_preserves_port(self) -> None:
        """Port is preserved when credentials are redacted."""
        from rootcoz.repository import redact_url

        result = redact_url("https://user:pass@github.com:8443/org/repo")
        assert ":8443" in result
        assert "user" not in result

    def test_git_protocol(self) -> None:
        """git:// URLs without credentials are unchanged."""
        from rootcoz.repository import redact_url

        assert redact_url("git://github.com/org/repo") == "git://github.com/org/repo"


class TestCloneBranchParameter:
    """Tests for passing branch parameter through clone functions."""

    @patch("rootcoz.repository.Git")
    def test_clone_with_ssl_retry_passes_branch(
        self, mock_git: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry passes branch to the git clone when non-empty."""
        from rootcoz.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry(
            "https://example.com/repo", clone_dir, depth=1, branch="develop"
        )
        mock_git.return_value.clone.assert_called_once_with(
            "https://example.com/repo",
            str(clone_dir),
            depth=1,
            branch="develop",
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_with_ssl_retry_no_branch_when_empty(
        self, mock_git: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry does NOT pass branch when empty string."""
        from rootcoz.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry("https://example.com/repo", clone_dir, depth=1, branch="")
        mock_git.return_value.clone.assert_called_once_with(
            "https://example.com/repo", str(clone_dir), depth=1, kill_after_timeout=ANY
        )

    @patch("rootcoz.repository.Git")
    def test_clone_with_ssl_retry_default_no_branch(
        self, mock_git: MagicMock, tmp_path
    ) -> None:
        """_clone_with_ssl_retry without branch arg does not pass branch."""
        from rootcoz.repository import _clone_with_ssl_retry

        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        _clone_with_ssl_retry("https://example.com/repo", clone_dir, depth=1)
        mock_git.return_value.clone.assert_called_once_with(
            "https://example.com/repo", str(clone_dir), depth=1, kill_after_timeout=ANY
        )

    def test_clone_with_ssl_retry_passes_branch_on_ssl_retry(self, tmp_path) -> None:
        """branch is passed in BOTH the initial and the SSL retry git clone call."""
        from rootcoz.repository import _clone_with_ssl_retry

        with patch("rootcoz.repository.Git") as mock_git_cls:
            mock_clone = mock_git_cls.return_value.clone
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

    @patch("rootcoz.repository.Git")
    def test_clone_into_passes_branch(self, mock_git: MagicMock, tmp_path) -> None:
        """clone_into forwards branch to _clone_with_ssl_retry."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        manager.clone_into(
            "https://github.com/org/repo", target, depth=1, branch="feature-x"
        )
        mock_git.return_value.clone.assert_called_once_with(
            "https://github.com/org/repo",
            str(staging_of(target)),
            depth=1,
            branch="feature-x",
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_into_no_branch_when_empty(
        self, mock_git: MagicMock, tmp_path
    ) -> None:
        """clone_into does not pass branch when empty string."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        manager.clone_into("https://github.com/org/repo", target, depth=1, branch="")
        mock_git.return_value.clone.assert_called_once_with(
            "https://github.com/org/repo",
            str(staging_of(target)),
            depth=1,
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_passes_branch(self, mock_git: MagicMock) -> None:
        """clone forwards branch to _clone_with_ssl_retry."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="release-1.0")
        mock_git.return_value.clone.assert_called_once()
        call_kwargs = mock_git.return_value.clone.call_args[1]
        assert call_kwargs["branch"] == "release-1.0"
        manager.cleanup()

    @patch("rootcoz.repository.Git")
    def test_clone_no_branch_when_empty(self, mock_git: MagicMock) -> None:
        """clone does not pass branch when empty string."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="")
        mock_git.return_value.clone.assert_called_once()
        call_kwargs = mock_git.return_value.clone.call_args[1]
        assert "branch" not in call_kwargs
        manager.cleanup()


class TestInjectTokenIntoUrl:
    """Tests for _inject_token_into_url helper."""

    def test_injects_token_into_https_url(self) -> None:
        from rootcoz.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://github.com/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        expected = "https://x-token-auth:tok123@github.com/org/repo"  # pragma: allowlist secret
        assert result == expected

    def test_replaces_existing_credentials(self) -> None:
        from rootcoz.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://user:pass@github.com/org/repo",  # pragma: allowlist secret
            "new_token",  # pragma: allowlist secret
        )
        assert "user" not in result
        assert "pass" not in result
        assert "x-token-auth:new_token@github.com" in result  # pragma: allowlist secret

    def test_preserves_port(self) -> None:
        from rootcoz.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "https://gitlab.internal:8443/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        assert (
            "x-token-auth:tok123@gitlab.internal:8443" in result
        )  # pragma: allowlist secret

    def test_ignores_non_https_url(self) -> None:
        from rootcoz.repository import _inject_token_into_url

        result = _inject_token_into_url(
            "git://github.com/org/repo",
            "tok123",  # pragma: allowlist secret
        )
        assert result == "git://github.com/org/repo"

    def test_empty_token_returns_original(self) -> None:
        from rootcoz.repository import _inject_token_into_url

        result = _inject_token_into_url("https://github.com/org/repo", "")
        assert result == "https://github.com/org/repo"

    def test_preserves_path_and_suffix(self) -> None:
        from rootcoz.repository import _inject_token_into_url

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


class TestIsFullCommitSha:
    """Tests for the exactly-40 rule that decides the fetch-by-id path."""

    def test_full_40_char_sha(self) -> None:
        assert _is_full_commit_sha("a" * 40) is True

    def test_full_40_char_sha_mixed_case(self) -> None:
        assert (
            _is_full_commit_sha(
                "aBcDeF1234567890aBcDeF1234567890aBcDeF12"  # pragma: allowlist secret
            )
            is True
        )

    @pytest.mark.parametrize("length", [39, 14, 7])
    def test_shorter_than_40_is_not_fetchable(self, length: int) -> None:
        """git-fetch(1) takes a fully spelled object name only; an abbreviated id fails."""
        assert _is_full_commit_sha("a" * length) is False

    def test_empty_string(self) -> None:
        assert _is_full_commit_sha("") is False

    def test_branch_name(self) -> None:
        assert _is_full_commit_sha("main") is False


class TestCloneShaFlow:
    """A commit ref is fetched by id; the full clone is the fallback, not the first move."""

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_with_sha_fetches_the_object_by_id(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """clone() with a full SHA fetches the object itself -- no full clone."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40

        manager.clone("https://github.com/org/repo", depth=50, branch=sha)

        fetch_repo = mock_repo_cls.init.return_value
        mock_repo_cls.init.assert_called_once_with(manager.temp_dirs[0])
        fetch_repo.create_remote.assert_called_once_with(
            "origin", "https://github.com/org/repo"
        )
        assert fetch_repo.git.fetch.call_args[0] == ("--depth=50", "origin", sha)
        assert fetch_repo.git.checkout.call_args[0] == ("--detach", "FETCH_HEAD")
        assert fetch_repo.git.checkout.call_args[1]["kill_after_timeout"] > 0
        # The whole point: no full clone on the success path.
        mock_git.assert_not_called()
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_with_sha_falls_back_when_the_remote_refuses_the_id(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """A server that refuses the object id is recovered by a full clone."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        mock_repo_cls.init.return_value.git.fetch.side_effect = GitCommandError(
            "git fetch",
            1,
            stderr="error: Server does not allow request for unadvertised object",
        )
        seen: list[bool] = []

        def clone(_url, directory, **_kwargs) -> None:
            seen.append(any(Path(directory).iterdir()))
            clone_into_staging(_url, directory)

        mock_git.return_value.clone.side_effect = clone

        manager.clone("https://github.com/org/repo", depth=50, branch=sha)

        mock_git.return_value.clone.assert_called_once()
        # The fetch rung left a .git/ behind and the fallback did not clone into it.
        assert seen == [False]
        mock_repo_cls.return_value.git.checkout.assert_called_once()
        assert mock_repo_cls.return_value.git.checkout.call_args[0][0] == sha
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_with_sha_raises_a_specific_error_when_the_commit_is_unavailable(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """An absent commit names the repository, the ref and the cause -- no token."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        fetch_repo = mock_repo_cls.init.return_value
        fetch_repo.git.fetch.side_effect = GitCommandError(
            "git fetch", 128, stderr="fatal: git upload-pack: not our ref"
        )
        after_clone = mock_repo_cls.return_value.git
        after_clone.checkout.side_effect = GitCommandError(
            "git checkout", 128, stderr=f"fatal: unable to read tree ({sha})"
        )
        after_clone.rev_parse.side_effect = GitCommandError("git rev-parse", 1)

        with pytest.raises(CloneRefUnavailableError) as excinfo:
            manager.clone(
                "https://x-token-auth:tok123@github.com/org/repo",  # pragma: allowlist secret
                depth=50,
                branch=sha,
            )

        error = excinfo.value
        assert error.label == "Commit or ref not found"
        assert sha in str(error)
        assert "https://github.com/org/repo" in str(error)
        assert "no branch of that name was found" in str(error)
        # It never claims to know where the commit is *not*.
        assert "not reachable from any branch or tag" not in str(error)
        assert "tok123" not in str(error)  # pragma: allowlist secret
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_the_ssl_label_falls_back_to_the_full_clone(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """A cert rejection on the by-id fetch must not regress today's recovery.

        The full-clone rung retries with GIT_SSL_NO_VERIFY, so dropping
        "SSL verification failed" from the fallback set would break a SHA ref on
        a self-signed-cert host that works today.
        """
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        mock_repo_cls.init.return_value.git.fetch.side_effect = GitCommandError(
            "git fetch",
            128,
            stderr="server verification failed: certificate signer not trusted",
        )
        mock_git.return_value.clone.side_effect = clone_fails_then_succeeds(
            GitCommandError(
                "git clone",
                128,
                stderr="server verification failed: certificate signer not trusted",
            )
        )

        manager.clone("https://example.com/repo", depth=1, branch=sha)

        clone = mock_git.return_value.clone
        assert clone.call_count == 2
        assert clone.call_args[1]["env"] == {"GIT_SSL_NO_VERIFY": "1"}
        assert mock_repo_cls.return_value.git.checkout.call_args[0][0] == sha
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_a_timeout_raises_and_does_not_fall_back(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """A hung remote fails at once: a second rung would spend a second deadline."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        mock_repo_cls.init.return_value.git.fetch.side_effect = GitCommandError(
            "git fetch",
            -9,
            stderr='Timeout: the command "git fetch" did not complete in 900 secs.',
        )

        with pytest.raises(CloneRefUnavailableError) as excinfo:
            manager.clone("https://example.com/repo", depth=1, branch=sha)

        assert excinfo.value.label == "Clone timed out"
        mock_git.return_value.clone.assert_not_called()
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_with_branch_uses_depth_and_branch(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """clone() with a branch name uses --depth and --branch as before."""
        manager = RepositoryManager()
        manager.clone("https://github.com/org/repo", depth=50, branch="release-1.8")

        mock_git.return_value.clone.assert_called_once()
        call_kwargs = mock_git.return_value.clone.call_args[1]
        assert call_kwargs["depth"] == 50
        assert call_kwargs["branch"] == "release-1.8"
        # No checkout call (Repo() not instantiated for checkout)
        mock_repo_cls.return_value.git.checkout.assert_not_called()
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_abbreviated_sha_skips_the_fetch_path(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock
    ) -> None:
        """A 7-39 hex ref cannot be fetched by id; today's resolution is kept."""
        manager = RepositoryManager()
        sha = "abc1234def5678"  # pragma: allowlist secret, 14 hex
        assert len(sha) < 40
        mock_git.return_value.clone.side_effect = clone_into_staging

        manager.clone("https://github.com/org/repo", depth=50, branch=sha)

        mock_repo_cls.init.assert_not_called()
        clone_kwargs = mock_git.return_value.clone.call_args[1]
        assert "depth" not in clone_kwargs
        assert "branch" not in clone_kwargs
        assert mock_repo_cls.return_value.git.checkout.call_args[0][0] == sha
        manager.cleanup()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_into_with_sha_fetches_the_object_by_id(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a full SHA fetches the object itself, then renames in place."""
        manager = RepositoryManager()
        sha = ("deadbeef1234567" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        target = tmp_path / "my-repo"
        staging = staging_of(target)
        mock_repo_cls.init.side_effect = _init_creating_leaf(mock_repo_cls)

        manager.clone_into("https://github.com/org/repo", target, depth=1, branch=sha)

        mock_repo_cls.init.assert_called_once_with(staging)
        fetch_repo = mock_repo_cls.init.return_value
        fetch_repo.create_remote.assert_called_once_with(
            "origin", "https://github.com/org/repo"
        )
        assert fetch_repo.git.fetch.call_args[0] == ("--depth=1", "origin", sha)
        assert fetch_repo.git.checkout.call_args[0] == ("--detach", "FETCH_HEAD")
        mock_git.assert_not_called()
        assert target.exists()
        assert not staging.exists()

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_into_with_branch_uses_depth_and_branch(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a branch name preserves existing behavior."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging

        manager.clone_into(
            "https://github.com/org/repo", target, depth=1, branch="develop"
        )

        call = mock_git.return_value.clone.call_args
        assert call[0] == ("https://github.com/org/repo", str(staging_of(target)))
        assert call[1]["depth"] == 1
        assert call[1]["branch"] == "develop"
        # Bounded by the one clone deadline, not by a per-command ceiling.
        assert call[1]["kill_after_timeout"] > 0

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_into_sha_with_token_scrubs_credentials(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a SHA + token still scrubs the staging remote."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        target = tmp_path / "my-repo"
        staging = staging_of(target)
        mock_repo_cls.init.side_effect = _init_creating_leaf(mock_repo_cls)

        with (
            patch("shutil.which", return_value="/usr/bin/git"),
            patch("rootcoz.repository.subprocess") as mock_subprocess,
        ):
            mock_run = mock_subprocess.run
            manager.clone_into(
                "https://github.com/org/repo",
                target,
                depth=1,
                branch=sha,
                token="tok123",  # pragma: allowlist secret
            )

        # Token injected into the remote the fetch rung adds.
        remote_url = mock_repo_cls.init.return_value.create_remote.call_args[0][1]
        assert "tok123" in remote_url  # pragma: allowlist secret
        # Credentials scrubbed against the staging tree
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["cwd"] == staging
        scrub_cmd = mock_run.call_args[0][0]
        assert scrub_cmd[-1] == "https://github.com/org/repo"

    @patch("rootcoz.repository.Repo")
    @patch("rootcoz.repository.Git")
    def test_clone_into_sha_with_ssl_retry(
        self, mock_git: MagicMock, mock_repo_cls: MagicMock, tmp_path
    ) -> None:
        """clone_into() with a SHA falls back to a full clone and retries on SSL."""
        manager = RepositoryManager()
        sha = ("abc1234def5678" * 3)[:40]  # pragma: allowlist secret
        assert len(sha) == 40
        target = tmp_path / "repo"
        mock_repo_cls.init.side_effect = _init_creating_leaf(mock_repo_cls)
        mock_repo_cls.init.return_value.git.fetch.side_effect = GitCommandError(
            "git fetch",
            1,
            stderr="error: Server does not allow request for unadvertised object",
        )
        mock_clone = mock_git.return_value.clone
        mock_clone.side_effect = clone_fails_then_succeeds(
            GitCommandError(
                "git clone",
                128,
                stderr="server verification failed: certificate signer not trusted",
            )
        )

        manager.clone_into("https://example.com/repo", target, branch=sha)

        assert mock_clone.call_count == 2
        # Both attempts resolve the ref locally: no depth, no branch.
        for call in mock_clone.call_args_list:
            assert "depth" not in call[1]
            assert "branch" not in call[1]
        # Retry should have SSL disabled
        _, kwargs = mock_clone.call_args
        assert kwargs.get("env", {}).get("GIT_SSL_NO_VERIFY") == "1"
        # Checkout should still happen
        checkout = mock_repo_cls.return_value.git.checkout
        assert checkout.call_count == 1
        assert checkout.call_args[0][0] == sha


class TestCloneByRefTypeRealGit:
    """The ladder against real git -- no mocks, ``file://`` bare repos."""

    def test_a_commit_on_no_branch_is_fetched_by_id(
        self, ref_repo, tmp_path, monkeypatch
    ) -> None:
        """A commit no ref advertises: today ``unable to read tree``, after: HEAD is it."""
        _pin_protocol(monkeypatch, "2")
        target = tmp_path / "clone"

        _clone_by_ref_type(
            ref_repo["url"], target, branch=ref_repo["dangling"], depth=1
        )

        assert _run_git(target, "rev-parse", "HEAD") == ref_repo["dangling"]
        assert _run_git(target, "rev-list", "--count", "HEAD") == "1"
        assert ref_repo["dangling"] in (target / ".git" / "shallow").read_text()

    @pytest.mark.parametrize(
        ("version", "depth_bounded"),
        [("2", True), ("0", False)],
        ids=["protocol-v2-fetch-rung", "protocol-v0-full-clone-rung"],
    )
    def test_an_unadvertised_ancestor_is_obtained_either_way(
        self, ref_repo, tmp_path, monkeypatch, version: str, depth_bounded: bool
    ) -> None:
        """v2 fetches the id; v0 refuses it and the full-clone rung recovers the commit."""
        _pin_protocol(monkeypatch, version)
        target = tmp_path / "clone"
        ancestor = ref_repo["shas"][-5]

        _clone_by_ref_type(ref_repo["url"], target, branch=ancestor, depth=1)

        assert _run_git(target, "rev-parse", "HEAD") == ancestor
        count = int(_run_git(target, "rev-list", "--count", "HEAD"))
        assert (count == 1) is depth_bounded

    def test_a_commit_the_remote_does_not_have_raises_and_names_it(
        self, ref_repo, tmp_path, monkeypatch
    ) -> None:
        """A ref that is nowhere raises the specific error, naming the repo and the ref."""
        _pin_protocol(monkeypatch, "2")
        absent = "1" * 40
        target = tmp_path / "clone"

        with pytest.raises(CloneRefUnavailableError) as excinfo:
            _clone_by_ref_type(ref_repo["url"], target, branch=absent, depth=1)

        assert excinfo.value.label == "Commit or ref not found"
        assert absent in str(excinfo.value)
        assert sanitize_clone_url(ref_repo["url"]) in str(excinfo.value)

    def test_a_40_hex_branch_name_resolves_to_the_branch(
        self, ref_repo, tmp_path, monkeypatch
    ) -> None:
        """git never DWIMs 40 hex as a ref name; ``origin/<ref>`` is what resolves it."""
        _pin_protocol(monkeypatch, "2")
        target = tmp_path / "clone"

        _clone_by_ref_type(
            ref_repo["url"], target, branch=ref_repo["hex_branch"], depth=1
        )

        assert _run_git(target, "rev-parse", "HEAD") == ref_repo["shas"][58]

    def test_a_branch_ref_stays_shallow(self, ref_repo, tmp_path, monkeypatch) -> None:
        """A branch ref keeps the shallow path: fewer commits, not ``--depth`` of them."""
        _pin_protocol(monkeypatch, "2")
        target = tmp_path / "clone"
        full = tmp_path / "full"

        _clone_by_ref_type(ref_repo["url"], target, branch="main", depth=10)
        _clone_by_ref_type(ref_repo["url"], full, branch="main")

        assert _run_git(target, "rev-parse", "HEAD") == ref_repo["shas"][-1]
        assert (target / ".git" / "shallow").read_text().strip()
        shallow = int(_run_git(target, "rev-list", "--count", "HEAD"))
        assert shallow < int(_run_git(full, "rev-list", "--count", "HEAD"))

    def test_an_abbreviated_id_resolves_through_local_resolution(
        self, ref_repo, tmp_path, monkeypatch
    ) -> None:
        """A 7-39 hex ref keeps today's behaviour: full clone, then a local checkout."""
        _pin_protocol(monkeypatch, "2")
        target = tmp_path / "clone"
        abbreviated = ref_repo["shas"][-5][:8]

        _clone_by_ref_type(ref_repo["url"], target, branch=abbreviated, depth=1)

        assert _run_git(target, "rev-parse", "HEAD") == ref_repo["shas"][-5]


class TestCloneIntoWithToken:
    """Tests for clone_into with token parameter."""

    @patch("rootcoz.repository.Git")
    def test_clone_into_with_token_injects_into_url(
        self, mock_git: MagicMock, tmp_path
    ) -> None:
        """clone_into with token injects it into the clone URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        manager.clone_into(
            "https://github.com/org/repo",
            target,
            depth=1,
            token="tok123",  # pragma: allowlist secret
        )
        mock_git.return_value.clone.assert_called_once_with(
            "https://x-token-auth:tok123@github.com/org/repo",  # pragma: allowlist secret
            str(staging_of(target)),
            depth=1,
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_into_without_token(self, mock_git: MagicMock, tmp_path) -> None:
        """clone_into without token uses original URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        manager.clone_into("https://github.com/org/repo", target, depth=1)
        mock_git.return_value.clone.assert_called_once_with(
            "https://github.com/org/repo",
            str(staging_of(target)),
            depth=1,
            kill_after_timeout=ANY,
        )

    @patch("rootcoz.repository.Git")
    def test_clone_into_with_none_token(self, mock_git: MagicMock, tmp_path) -> None:
        """clone_into with None token uses original URL."""
        manager = RepositoryManager()
        target = tmp_path / "my-repo"
        mock_git.return_value.clone.side_effect = clone_into_staging
        manager.clone_into("https://github.com/org/repo", target, depth=1, token=None)
        mock_git.return_value.clone.assert_called_once_with(
            "https://github.com/org/repo",
            str(staging_of(target)),
            depth=1,
            kill_after_timeout=ANY,
        )
