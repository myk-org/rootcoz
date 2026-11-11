"""Git repository management for cloning and cleanup."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import quote, urlparse, urlunparse

from git import Git, Repo
from git.exc import GitCommandError
from pydantic import HttpUrl

from rootcoz import get_child_logger
from rootcoz.temp_paths import DeleteResult, delete_tree, rootcoz_temp_base
from rootcoz.url_utils import (
    redact_urls_in_text,
    sanitize_clone_url,
    strip_url_credentials,
)

if TYPE_CHECKING:
    from rootcoz.models import AdditionalRepo

logger = get_child_logger(__name__)

RESERVED_REPO_NAMES = frozenset({"build-artifacts"})

_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FULL_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


def _is_commit_sha(ref: str) -> bool:
    """Check if a ref looks like a commit SHA (7-40 hex chars)."""
    return bool(_SHA_PATTERN.match(ref))


def _is_full_commit_sha(ref: str) -> bool:
    """Can the remote be asked for this ref by object id?

    git-fetch(1) accepts a "fully spelled hex object name" only: an abbreviated
    id is ``fatal: couldn't find remote ref`` on every remote measured.
    """
    return bool(_FULL_SHA_PATTERN.match(ref))


class CloneRefUnavailableError(RuntimeError):
    """A commit ref could not be obtained. ``label`` is a member of the closed set.

    A ``RuntimeError`` so every existing non-fatal clone handler keeps treating a
    missing repository as a warning.
    """

    def __init__(self, label: str, message: str) -> None:
        """Keep the label next to the message a human reads."""
        super().__init__(message)
        self.label = label


# Stderr phrases and the cause label they name -- the one table, moved out of
# ``engine/core.py``. Matching fixed phrases only: stderr may embed a
# credential-bearing URL.
_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("did not complete in", "process killed because it timed out"), "Clone timed out"),
    (("authentication failed",), "Authentication failed"),
    (
        (
            "not our ref",
            "unable to read tree",
            "could not get object",
            "couldn't find remote ref",
            "does not allow request for unadvertised object",
        ),
        "Commit or ref not found",
    ),
    # "not found in upstream origin" deliberately NOT in the row above: git emits
    # it as "fatal: Remote branch <x> not found in upstream origin" -- a *branch*
    # typo. There it would shadow the branch row and report a missing commit for
    # a mistyped branch name. The branch row's "remote branch" pattern has it.
    (
        ("remote branch", "pathspec", "bad revision", "not a valid ref"),
        "Branch or ref not found",
    ),
    (
        ("ssl certificate", "certificate verify", "server verification failed"),
        "SSL verification failed",
    ),
    (("permission denied",), "Permission denied"),
    (
        ("could not resolve host", "name or service not known"),
        "Network DNS resolution failed",
    ),
    (("repository not found", "not a git repository"), "Repository not found"),
)


def clone_failure_reason(exc: BaseException) -> str:
    """A label from the closed set above. Never stderr, never a URL, never a sentence.

    This is the value that is stored, served and rendered, so it has to be
    reproducible from the table alone: git's stderr carries the token-bearing
    remote URL.
    """
    if isinstance(exc, CloneRefUnavailableError):
        return exc.label
    if isinstance(exc, GitCommandError):
        stderr = (exc.stderr or "").lower()
        for patterns, label in _LABELS:
            if any(pattern in stderr for pattern in patterns):
                return label
        return "Git command failed"
    if isinstance(exc, ValueError) and str(exc).startswith(
        "Invalid repository URL scheme."
    ):
        return "Invalid repository URL scheme"
    return "Clone worker failed"


def clone_failure_detail(exc: BaseException) -> str:
    """One credential-free line of detail *for the log only*. Never stored, never served.

    Capped at 200 characters: stderr is remote-controlled, so the bound is what
    keeps a verbose or hostile remote from writing an unbounded log line.
    """
    return _printable(" ".join(redact_urls_in_text(str(exc)).split()))[:200]


def _printable(text: str) -> str:
    """Drop control and format characters: the URL and the ref come from the request.

    Measured: ``sanitize_clone_url`` eats a newline and concatenates the words
    around it, and leaves ``\\x00`` in place.
    """
    return "".join(ch for ch in text if ch.isprintable())


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


def _clone_timeout() -> float:
    """The wall-clock ceiling for one clone, read once per public call."""
    from rootcoz.config import get_settings  # local: config imports models, not this

    return float(get_settings().repo_clone_timeout_seconds)


def _remaining(deadline: float | None) -> float:
    """Seconds left for this git command; floored at 1 s so a spent deadline fails fast."""
    if deadline is None:
        return _clone_timeout()
    return max(1.0, deadline - time.monotonic())


def _kill_clone_tree(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the clone *and its children*, so nothing keeps the pipes open."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        proc.kill()


def _git_clone(
    repo_url: str,
    clone_dir: Path,
    *,
    kill_after_timeout: float,
    depth: int | None = None,
    branch: str = "",
    env: dict[str, str] | None = None,
) -> None:
    """``git clone`` in its own process group, killed **as a group** on timeout.

    GitPython is not used here: its watchdog SIGKILLs the ``git`` parent only.
    ``git-remote-https`` inherits the stdout/stderr pipes, so ``communicate()``
    never sees EOF -- measured, ``git.clone(..., kill_after_timeout=3)`` against
    a listener that accepts and never answers was still blocked past 25 s on
    ``https://`` while ``git://`` returned at 3.06 s, with the helper surviving.
    ``start_new_session`` makes the clone its own group leader, so the timeout
    has a group to kill and the call returns in seconds.

    Raises:
        GitCommandError: git failed, or the clone outlived *kill_after_timeout*
            (its stderr says "did not complete in", which labels it
            "Clone timed out" in ``_LABELS``).
    """
    cmd = [Git.GIT_PYTHON_GIT_EXECUTABLE or "git", "clone"]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", str(repo_url), str(clone_dir)]
    # LC_ALL/LANGUAGE as GitPython set them: ``_LABELS`` matches English phrases.
    full_env = {
        **os.environ,
        "LANGUAGE": "C",
        "LC_ALL": "C",
        **(env or {}),
    }
    proc = subprocess.Popen(
        cmd,
        cwd=str(clone_dir.parent),
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, raw_stderr = proc.communicate(timeout=kill_after_timeout)
    except subprocess.TimeoutExpired:
        _kill_clone_tree(proc)
        proc.communicate()  # pipes close with the group; collect what was written
        raise GitCommandError(
            cmd,
            proc.returncode,
            stderr=f"Timeout: the command did not complete in "
            f"{kill_after_timeout:.0f} secs.",
        ) from None
    if proc.returncode != 0:
        raise GitCommandError(
            cmd, proc.returncode, stderr=raw_stderr.decode(errors="replace")
        )


def _reset_target(target_dir: Path) -> None:
    """Leave an empty directory at *target_dir*, or refuse to clone into it.

    A delete that could not remove the tree leaves a non-empty dir, and cloning
    into that residue fails in a way that hides the real cause. Same test as
    ``RepositoryManager.cleanup``: only a delete that left nothing is a safe
    base for the next attempt.
    """
    if target_dir.exists():
        result = delete_tree(target_dir)
        if result.failed or target_dir.exists():
            logger.error(
                f"Partial clone at {target_dir} survived the delete "
                f"(failed={result.failed}); not cloning into it again"
            )
            raise OSError(f"Cannot retry the clone: {target_dir} could not be removed")
    target_dir.mkdir(parents=True, exist_ok=True)


def _clone_with_ssl_retry(
    repo_url: str,
    clone_dir: Path,
    depth: int | None = None,
    branch: str = "",
    deadline: float | None = None,
) -> None:
    """Clone a repo, retrying without SSL verification on cert errors.

    Args:
        repo_url: Repository URL to clone.
        clone_dir: Target directory for the clone.
        depth: Number of commits to fetch. None means full clone (no --depth).
        branch: Git ref (branch/tag) to check out. Empty string means default branch.
        deadline: ``time.monotonic()`` value the whole clone must finish by.
    """
    kwargs: dict[str, Any] = {}
    if depth is not None:
        kwargs["depth"] = depth
    if branch:
        kwargs["branch"] = branch
    try:
        _git_clone(
            repo_url,
            clone_dir,
            kill_after_timeout=_remaining(deadline),
            **kwargs,
        )
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
            # Clean up the partial clone before the retry.
            _reset_target(clone_dir)
            _git_clone(
                repo_url,
                clone_dir,
                kill_after_timeout=_remaining(deadline),
                **kwargs,
                env={"GIT_SSL_NO_VERIFY": "1"},
            )
        else:
            raise


def _has_remote_branch(git: Git, ref: str) -> bool:
    """Does ``refs/remotes/origin/<ref>`` resolve? A non-zero rev-parse is the answer."""
    try:
        git.rev_parse("--verify", "--quiet", f"refs/remotes/origin/{ref}")
    except GitCommandError:
        return False
    return True


def _checkout_ref(target_dir: Path, ref: str, deadline: float | None) -> None:
    """Check *ref* out, then retry it as a remote branch **only if that branch exists**.

    git never DWIMs a 40-hex argument as a ref name (git-branch(1): "it will be
    ignored when you just specify 40-hex"), so a branch literally named with 40
    hex digits has to be checked out as ``origin/<ref>``; ``refs/heads/<ref>``
    does not exist in a fresh clone (measured).

    The retry is guarded and the first exception is kept: when ``origin/<ref>``
    does not resolve, ``git checkout --detach origin/<ref>`` answers "--detach
    does not take a path argument", which matches no label and would be reported
    as the useless "Git command failed". Re-raising keeps the message that does
    label it: an absent object gives ``unable to read tree`` ("Commit or ref not
    found"), an abbreviated id gives ``pathspec ... did not match`` ("Branch or
    ref not found").
    """
    git = Repo(target_dir).git
    try:
        git.checkout(ref, kill_after_timeout=_remaining(deadline))
    except GitCommandError:
        if not _has_remote_branch(git, ref):
            raise
        git.checkout(
            "--detach", f"origin/{ref}", kill_after_timeout=_remaining(deadline)
        )


def _ref_message(repo_url: str, ref: str, exc: BaseException) -> str:
    """A message that asserts only what was observed, built from the label."""
    label = clone_failure_reason(exc)
    # The SHAPE decides first, not the label: an absent abbreviated id re-raises
    # git's ``pathspec ... did not match`` (label "Branch or ref not found"), so a
    # shape test nested in the "Commit or ref not found" branch could never fire.
    if not _is_full_commit_sha(ref):
        why = (
            "an abbreviated commit id cannot be fetched from a remote; "
            "use the full 40 characters"
        )
    elif label == "Commit or ref not found":
        why = "the remote serves no commit by that id, and no branch of that name was found"
    else:
        why = f"the clone failed ({label})"
    # 300 characters: a one-line message for a log or a CLI, and short enough
    # that a remote padding its own text cannot push the cause out of view.
    return _printable(
        f"Cannot clone {sanitize_clone_url(str(repo_url))} at {ref}: {why}"
    )[:300]


def _ref_unavailable(
    repo_url: str, ref: str, exc: GitCommandError
) -> CloneRefUnavailableError:
    """Wrap a failed ref checkout, keeping its label and its wrapped cause chain."""
    return CloneRefUnavailableError(
        clone_failure_reason(exc), _ref_message(repo_url, ref, exc)
    )


# Both labels mean "the by-id fetch did not get us the commit, but a full clone
# might". SSL is in the set because ``_clone_with_ssl_retry`` recovers a
# self-signed host with GIT_SSL_NO_VERIFY=1; without it a SHA ref on that host
# would regress from working to "SSL verification failed".
_FALLBACK_LABELS = frozenset({"Commit or ref not found", "SSL verification failed"})


def _fetch_commit_by_id(
    repo_url: str, target_dir: Path, sha: str, depth: int | None, deadline: float | None
) -> bool:
    """Fetch one commit by object id into a fresh tree: init, remote add, fetch, checkout.

    Returns False only when the remote says it does not have or will not hand
    over the object -- the cases the full-clone rung recovers. Any other failure
    (auth, DNS, TLS, timeout) is raised at once: falling back against the same
    dead remote would spend a second deadline to learn the same thing.
    """
    try:
        repo = Repo.init(target_dir)
        repo.create_remote("origin", str(repo_url))
        args = ([f"--depth={depth}"] if depth else []) + ["origin", sha]
        repo.git.fetch(*args, kill_after_timeout=_remaining(deadline))
        repo.git.checkout(
            "--detach", "FETCH_HEAD", kill_after_timeout=_remaining(deadline)
        )
    except GitCommandError as exc:
        label = clone_failure_reason(exc)
        if label in _FALLBACK_LABELS:
            logger.info(
                "Remote did not serve %s by id (%s); using a full clone", sha, label
            )
            return False
        raise CloneRefUnavailableError(label, _ref_message(repo_url, sha, exc)) from exc
    return True


def _clone_by_ref_type(
    repo_url: str,
    target_dir: Path,
    *,
    branch: str = "",
    depth: int | None = None,
    log_prefix: str = "Cloning repository",
    deadline: float | None = None,
) -> None:
    """Clone a repository, choosing the strategy from the shape of *branch*.

    A **full 40-hex** ref is fetched by object id (``git fetch --depth <d> origin
    <sha>`` + ``checkout --detach FETCH_HEAD``), so a commit that no branch or tag
    advertises can be obtained without cloning the whole history. When that is
    impossible the ref is resolved the way this path always has -- a full clone
    plus an explicit checkout -- because ``git clone --branch`` does not accept
    raw SHAs, and for a strict server the commit may live only on a side branch.
    Branch and tag refs keep the shallow-clone path.

    Args:
        repo_url: URL of the git repository to clone.
        target_dir: Directory to clone into.
        branch: Git ref or commit SHA.  Empty string means default branch.
        depth: Graph depth for the by-id fetch (and for a branch/tag clone).
            The full-clone fallback is deliberately complete: a shallow
            history cannot hold a commit outside the one it fetched.
        log_prefix: Human-readable prefix for the log message.
        deadline: ``time.monotonic()`` value the whole clone must finish by.

    Raises:
        CloneRefUnavailableError: The ref could not be obtained; ``label``
            carries a label from the closed set and the message names the cause.
    """
    if branch and _is_commit_sha(branch):
        if _is_full_commit_sha(branch) and _fetch_commit_by_id(
            repo_url, target_dir, branch, depth, deadline
        ):
            logger.info(
                f"{log_prefix} to {target_dir} (sha={branch}, depth={depth or 'full'})"
            )
            return
        # Either the remote would not serve the object by id, or this is an
        # abbreviated id no remote can be asked for. Resolve it the way this
        # path always has, so nothing that works today stops working.
        logger.info(f"{log_prefix} to {target_dir} (sha={branch}, local resolution)")
        _reset_target(target_dir)  # the fetch rung may have left a .git/ behind
        try:
            _clone_with_ssl_retry(repo_url, target_dir, deadline=deadline)
            _checkout_ref(target_dir, branch, deadline)
        except GitCommandError as exc:
            raise _ref_unavailable(repo_url, branch, exc) from exc
    else:
        ref_msg = f" (ref={branch})" if branch else ""
        logger.info(f"{log_prefix} to {target_dir}{ref_msg}")
        _clone_with_ssl_retry(
            repo_url, target_dir, depth, branch=branch, deadline=deadline
        )


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
            deadline=time.monotonic() + _clone_timeout(),
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
                deadline=time.monotonic() + _clone_timeout(),
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
        # The shared ``.staging`` parent stays: removing it after our own rename
        # deletes the parent of a sibling clone that has not created its child
        # yet, and that clone then lands nowhere. Chat purges the whole tree.
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
