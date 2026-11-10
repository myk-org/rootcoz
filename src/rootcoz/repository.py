"""Git repository management for cloning and cleanup."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Self
from urllib.parse import quote, urlparse, urlunparse

from git import Repo
from git.exc import GitCommandError
from pydantic import HttpUrl

from rootcoz import get_child_logger
from rootcoz.temp_paths import DeleteResult, delete_tree, rootcoz_temp_base
from rootcoz.url_utils import strip_url_credentials

if TYPE_CHECKING:
    from rootcoz.models import AdditionalRepo

logger = get_child_logger(__name__)

RESERVED_REPO_NAMES = frozenset({"build-artifacts"})

_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_commit_sha(ref: str) -> bool:
    """Check if a ref looks like a commit SHA (7-40 hex chars)."""
    return bool(_SHA_PATTERN.match(ref))


def redact_url(url: str) -> str:
    """Remove userinfo (credentials) from a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        redacted = parsed._replace(
            netloc=f"***@{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
        )
        return urlunparse(redacted)
    return url


def repo_name_from_url(repo_url: str | HttpUrl) -> str:
    """Extract repository name from a URL for use as a directory name.

    Takes the last path segment and strips the '.git' suffix.
    E.g., 'https://github.com/org/my-repo.git' -> 'my-repo'

    Args:
        repo_url: Repository URL (string or HttpUrl).

    Returns:
        Repository name suitable for use as a subdirectory name.
    """
    name = str(repo_url).rstrip("/").split("/")[-1]
    return name.removesuffix(".git")


def derive_test_repo_name(
    tests_repo_url: str,
    additional_repos_list: list[AdditionalRepo] | None,
) -> str:
    """Derive test repo subdirectory name, avoiding collisions with additional repos.

    Extracts the repository name from the URL and checks whether it would
    collide with any additional repo name.  When a collision is detected the
    function iterates candidate names (``tests-repo-1``, ``tests-repo-2``, ...)
    until it finds one that does not clash.

    Args:
        tests_repo_url: URL of the test repository.
        additional_repos_list: List of additional repo objects (may be None or empty).

    Returns:
        Safe subdirectory name for the test repo clone.
    """
    name = repo_name_from_url(tests_repo_url)
    if not additional_repos_list:
        additional_repos_list = []

    additional_names = {ar.name for ar in additional_repos_list}
    # Also treat reserved names as collisions
    reserved = additional_names | RESERVED_REPO_NAMES
    if name not in reserved:
        return name

    # Collision -- find a unique fallback
    for suffix in range(1, 100):
        candidate = f"tests-repo-{suffix}"
        if candidate not in reserved:
            logger.warning(
                f"Test repo directory name '{name}' collides with additional repo name. "
                f"Using '{candidate}' as fallback."
            )
            return candidate

    # Should never reach here with <100 additional repos
    fallback = f"tests-repo-{uuid.uuid4().hex[:8]}"
    logger.warning(
        f"Test repo directory name '{name}' collides with additional repo names. "
        f"All numeric fallbacks exhausted. Using '{fallback}'."
    )
    return fallback


def _scrub_credentials(target_dir: Path, repo_url: str | HttpUrl) -> None:
    """Reset remote.origin.url to strip embedded credentials from .git/config.

    Called after clone operations that inject auth tokens into the clone URL.
    No-op if .git directory does not exist (e.g. clone failed before creating it).
    """
    if not (target_dir / ".git").exists():
        return
    git_executable = shutil.which("git")
    if git_executable:
        subprocess.run(
            [git_executable, "remote", "set-url", "origin", str(repo_url)],
            cwd=target_dir,
            check=False,
            capture_output=True,
        )


def _staged_name(target_dir: Path) -> str:
    """The target's own name, refused unless it is a plain path component.

    ``clone_into`` clones into ``<target's parent>/.staging/<name>`` and clears
    that path first, so a name like ``..`` would make the staging path the
    caller's workspace and the clearing would delete its every child.
    """
    name = target_dir.name
    if not name or name in (".", "..") or Path(name).name != name:
        raise ValueError(f"Invalid clone target name: {name!r}")
    return name


def _validate_repo_url(repo_url: str | HttpUrl) -> None:
    """Validate repository URL scheme to prevent SSRF."""
    url_str = str(repo_url).lower()
    if not url_str.startswith(("https://", "git://")):
        # Redacted at the raise: every caller logs this with exc_info, and both
        # the userinfo and a ?token= query are credentials. main.py matches the
        # message prefix, which is unchanged.
        raise ValueError(
            "Invalid repository URL scheme. Only https:// and git:// are allowed, "
            f"got: {strip_url_credentials(str(repo_url))}"
        )


def _inject_token_into_url(url: str, token: str) -> str:
    """Inject an authentication token into a clone URL.

    For HTTPS URLs, embeds the token as ``x-token-auth:<token>@host``.
    Non-HTTPS URLs are returned unchanged (token is ignored).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not token:
        return url
    # Replace any existing userinfo with token-based auth
    host_port = parsed.netloc.rsplit("@", 1)[-1]
    netloc = f"x-token-auth:{quote(token, safe='')}@{host_port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _clone_with_ssl_retry(
    repo_url: str, clone_dir: Path, depth: int | None = None, branch: str = ""
) -> None:
    """Clone a repo, retrying without SSL verification on cert errors.

    Args:
        repo_url: Repository URL to clone.
        clone_dir: Target directory for the clone.
        depth: Number of commits to fetch. None means full clone (no --depth).
        branch: Git ref (branch/tag) to check out. Empty string means default branch.
    """
    kwargs: dict[str, object] = {}
    if depth is not None:
        kwargs["depth"] = depth
    if branch:
        kwargs["branch"] = branch
    try:
        Repo.clone_from(repo_url, clone_dir, **kwargs)
    except GitCommandError as exc:
        stderr = str(exc)
        if (
            "SSL" in stderr
            or "certificate" in stderr
            or "server verification failed" in stderr
        ):
            logger.warning(
                f"SSL certificate verification failed for {redact_url(str(repo_url))}, "
                "retrying with SSL verification disabled (GIT_SSL_NO_VERIFY=1)"
            )
            # Clean up the partial clone before the retry: a delete that could
            # not remove the tree leaves a non-empty dir, and cloning into that
            # residue fails in a way that hides the real cause. Same test as
            # ``RepositoryManager.cleanup``: only a delete that left nothing is
            # a safe base for the retry.
            if clone_dir.exists():
                result = delete_tree(clone_dir)
                if result.failed or clone_dir.exists():
                    logger.error(
                        f"Partial clone at {clone_dir} survived the delete "
                        f"(failed={result.failed}); not retrying the clone into it"
                    )
                    raise OSError(
                        f"Cannot retry the clone: {clone_dir} could not be removed"
                    ) from exc
                clone_dir.mkdir(parents=True, exist_ok=True)
            Repo.clone_from(
                repo_url,
                clone_dir,
                **kwargs,
                env={"GIT_SSL_NO_VERIFY": "1"},
            )
        else:
            raise


def _clone_by_ref_type(
    repo_url: str,
    target_dir: Path,
    *,
    branch: str = "",
    depth: int | None = None,
    log_prefix: str = "Cloning repository",
) -> None:
    """Clone a repository, choosing strategy based on whether *branch* is a commit SHA.

    For commit SHAs a full (unshallow) clone is performed followed by an explicit
    checkout, because ``git clone --branch`` does not accept raw SHAs.  For
    regular refs (branches / tags) the standard shallow-clone path is used.

    Args:
        repo_url: URL of the git repository to clone.
        target_dir: Directory to clone into.
        branch: Git ref or commit SHA.  Empty string means default branch.
        depth: Shallow-clone depth (ignored for SHA clones).
        log_prefix: Human-readable prefix for the log message.
    """
    if branch and _is_commit_sha(branch):
        logger.info(f"{log_prefix} to {target_dir} (sha={branch})")
        _clone_with_ssl_retry(repo_url, target_dir)
        Repo(target_dir).git.checkout(branch)
    else:
        ref_msg = f" (ref={branch})" if branch else ""
        logger.info(f"{log_prefix} to {target_dir}{ref_msg}")
        _clone_with_ssl_retry(repo_url, target_dir, depth, branch=branch)


class RepositoryManager:
    """Manages temporary git repository clones."""

    def __init__(self) -> None:
        """Initialize repository manager."""
        self.base_path = rootcoz_temp_base()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.temp_dirs: list[Path] = []

    def clone(self, repo_url: str | HttpUrl, depth: int = 50, branch: str = "") -> Path:
        """Clone repository to a unique temporary directory.

        Each clone gets a UUID-based directory to support parallel webhook calls.

        Args:
            repo_url: URL of the git repository to clone (string or HttpUrl).
            depth: Number of commits to fetch for git history context.
            branch: Git ref (branch/tag) to check out. Empty string means default branch.
                    Accepts both branch/tag names and commit SHAs (7-40 hex chars).

        Returns:
            Path to the cloned repository.

        Raises:
            ValueError: If repo_url uses an unsafe scheme (not https:// or git://).
        """
        _validate_repo_url(repo_url)
        clone_id = str(uuid.uuid4())[:8]
        repo_name = repo_name_from_url(repo_url)
        clone_dir = self.base_path / f"{repo_name}-{clone_id}"
        clone_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dirs.append(clone_dir)
        _clone_by_ref_type(
            str(repo_url),
            clone_dir,
            branch=branch,
            depth=depth,
            log_prefix="Cloning repository",
        )
        return clone_dir

    def clone_into(
        self,
        repo_url: str | HttpUrl,
        target_dir: Path,
        depth: int = 1,
        branch: str = "",
        token: str | None = None,
    ) -> Path:
        """Clone repository into a specific target directory.

        Unlike clone(), this places the repo at an explicit path instead
        of auto-generating one.  The target directory is NOT individually
        tracked for cleanup -- it is expected to live under a workspace
        directory whose lifecycle is managed by the caller.

        Args:
            repo_url: URL of the git repository to clone.
            target_dir: Exact directory to clone into.
            depth: Number of commits to fetch (default 1 for shallow).
            branch: Git ref (branch/tag) to check out. Empty string means default branch.
            token: Authentication token for private repos (injected into URL).

        Returns:
            Path to the cloned repository (same as target_dir).

        Raises:
            ValueError: If repo_url uses an unsafe scheme.
        """
        _validate_repo_url(repo_url)
        staging = target_dir.parent / ".staging" / _staged_name(target_dir)
        staging.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.parent.chmod(0o700)
        # A clone appears in place only once it is complete. ``git clone``
        # creates ``.git`` before it fetches, so a partial tree in the target
        # would be listed, indexed and read as a finished repository — and the
        # staging tree is one level below the workspace, where no scanner looks.
        if staging.is_symlink() or staging.exists():
            delete_tree(staging)
        clone_url = str(repo_url)
        if token:
            clone_url = _inject_token_into_url(clone_url, token)
        failed = True
        try:
            _clone_by_ref_type(
                clone_url,
                staging,
                branch=branch,
                depth=depth,
                log_prefix="Cloning repository into",
            )
            failed = False
        finally:
            if token:
                # Aimed at the staging tree: it is where the injected remote
                # landed, and it reaches transcripts the agent can read.
                _scrub_credentials(staging, repo_url)
            if failed:
                delete_tree(staging)
        try:
            os.replace(staging, target_dir)
        except OSError:
            delete_tree(staging)
            raise
        try:
            # Only when no sibling clone is still staged there. Analysis workspaces
            # are read by the agent, and an empty ``.staging`` there is a directory
            # that looks like leftover state; chat purges the whole tree itself.
            staging.parent.rmdir()
        except OSError:
            pass
        return target_dir

    def create_workspace(self) -> Path:
        """Create a workspace directory for repository clones.

        Both the test repository and additional repositories are cloned
        as subdirectories of this workspace.

        Returns:
            Path to the created workspace directory.
        """
        workspace_id = str(uuid.uuid4())[:8]
        workspace_dir = self.base_path / f"workspace-{workspace_id}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dirs.append(workspace_dir)
        logger.info(f"Created workspace directory: {workspace_dir}")
        return workspace_dir

    def cleanup(self) -> DeleteResult:
        """Remove all cloned repositories; return what went and what did not.

        Sync on purpose — it is a reaper step, called from
        ``temp_paths.cleanup_off_loop`` and never from the event loop.
        """
        logger.debug("Cleaning up temporary directories")
        total = DeleteResult(0, 0)
        survivors: list[Path] = []
        for temp_dir in self.temp_dirs:
            result = delete_tree(temp_dir)
            total = total.plus(result)
            # Only what is really gone is forgotten: a dir a failed delete left
            # behind stays tracked, so the next cleanup retries it instead of
            # leaking it silently.
            if result.failed or temp_dir.exists():
                survivors.append(temp_dir)
        self.temp_dirs[:] = survivors
        if survivors:
            logger.warning(
                f"Keeping {len(survivors)} temp dir(s) tracked after a partial cleanup"
            )
        return total

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: object,
    ) -> None:
        """Exit context manager and cleanup."""
        self.cleanup()
