"""A clone appears in the workspace only once it is complete (#511, A8).

``git clone`` creates ``.git`` before it fetches anything, and every scanner that
decides what to index treats a direct child with a ``.git`` as a repository. So a
clone that dies half-way must not be in the workspace at all: ``clone_into``
stages under ``<workspace>/.staging/<name>`` and renames it into place.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from git.exc import GitCommandError

from rootcoz.repository import RepositoryManager


def _clone_into_staging(workspace, name):
    return workspace / ".staging" / name


def test_clone_into_stages_then_renames(tmp_path) -> None:
    """A successful clone lands on the target and leaves no staging residue."""
    manager = RepositoryManager()
    workspace = tmp_path / "workspace"
    target = workspace / "repo"
    seen: list[str] = []

    def clone(_url, directory, **_kwargs):
        directory = Path(directory)
        # No ``.git`` in the target yet: the clone is not visible in place.
        seen.append(str(directory))
        assert not target.exists()
        (directory / ".git").mkdir(parents=True)

    with patch("rootcoz.repository.Git") as mock_git:
        mock_git.return_value.clone.side_effect = clone
        manager.clone_into("https://example.com/repo", target, depth=1)

    assert seen == [str(_clone_into_staging(workspace, "repo"))]
    assert target.is_dir() and (target / ".git").is_dir()
    assert not _clone_into_staging(workspace, "repo").exists()
    # The shared ``.staging`` parent stays: removing it after our own rename
    # deletes the parent of a sibling clone still between its mkdir and its
    # ``git clone``, and that repository then silently fails to clone. Chat
    # purges the whole tree, and an empty dot-directory is the cheaper trade.
    assert list((workspace / ".staging").iterdir()) == []


def test_a_failed_clone_leaves_no_target_and_no_residue(tmp_path) -> None:
    """A clone that dies mid-way leaves nothing a scanner could read as a repo."""
    manager = RepositoryManager()
    workspace = tmp_path / "workspace"
    target = workspace / "repo"

    def clone(_url, directory, **_kwargs):
        (Path(directory) / ".git").mkdir(parents=True)
        (Path(directory) / "half-an-object").write_text("x")
        raise GitCommandError("git clone", 128, stderr="connection reset")

    with (
        patch("rootcoz.repository.Git") as mock_git,
        pytest.raises(GitCommandError),
    ):
        mock_git.return_value.clone.side_effect = clone
        manager.clone_into("https://example.com/repo", target)

    assert not target.exists()
    assert not _clone_into_staging(workspace, "repo").exists()


def test_a_failed_clone_does_not_keep_the_token_in_staging(tmp_path) -> None:
    """The injected remote never survives an abandoned clone.

    The token is in ``<staging>/.git/config`` after ``git clone`` starts, and the
    staging tree is a tree the chat agent can read.
    """
    manager = RepositoryManager()
    workspace = tmp_path / "workspace"
    target = workspace / "repo"
    secret = "x-token-auth:sekrit"  # pragma: allowlist secret

    def clone(_url, directory, **_kwargs):
        (Path(directory) / ".git").mkdir(parents=True)
        (Path(directory) / ".git" / "config").write_text(f"url = {secret}@host/repo")
        raise GitCommandError("git clone", 128, stderr="boom")

    with (
        patch("rootcoz.repository.Git") as mock_git,
        pytest.raises(GitCommandError),
    ):
        mock_git.return_value.clone.side_effect = clone
        manager.clone_into(
            "https://example.com/repo",
            target,
            token="sekrit",  # pragma: allowlist secret
        )

    residue = "".join(
        str(path.read_text(errors="ignore"))
        for path in workspace.rglob("*")
        if path.is_file()
    )
    assert "sekrit" not in residue  # pragma: allowlist secret


def test_a_staging_tree_is_not_a_clone(tmp_path) -> None:
    """A killed clone's residue is never listed, indexed or read as a repository."""
    from rootcoz.engine import graft
    from rootcoz.engine.graft_http import cloned_graph_roots

    workspace = tmp_path / "workspace"
    stale = _clone_into_staging(workspace, "repo")
    (stale / ".git").mkdir(parents=True)

    assert cloned_graph_roots(workspace) == {}
    assert graft._valid_repo(workspace, "repo", workspace / "repo") is False
    assert graft.roots_needing_index(workspace, {"repo": stale}) == {}


def test_a_dot_name_is_refused_before_anything_is_touched(tmp_path) -> None:
    """A target name that is not a plain component cannot clear the workspace."""
    manager = RepositoryManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = workspace / "keep-me"
    sibling.write_text("jfy")

    for name in ("..",):
        with pytest.raises(ValueError, match="Invalid clone target name"):
            manager.clone_into("https://example.com/..", workspace / name)

    assert sibling.read_text() == "jfy"
    assert not (workspace / ".staging").exists()


def test_chat_cleanup_removes_the_staging_tree(tmp_path, monkeypatch) -> None:
    """Clear deletes clones, so it deletes the staging residue too.

    ``cleanup_chat_repos`` keeps dot-prefixed children on purpose (session data),
    and a hard-killed clone's residue can still carry a token.
    """
    from rootcoz.engine import chat
    from rootcoz.temp_paths import DeleteResult

    workspace = tmp_path / "rootcoz-chat-job"
    stale = _clone_into_staging(workspace, "repo")
    (stale / ".git").mkdir(parents=True)
    (stale / ".git" / "config").write_text(
        "x-token-auth:sekrit@host"
    )  # pragma: allowlist secret
    (workspace / ".claude").mkdir()

    monkeypatch.setattr(chat, "get_chat_workspace", lambda *a, **k: workspace)
    with patch("rootcoz.engine.graft.reap_graph", return_value=DeleteResult(0, 0)):
        chat.cleanup_chat_repos("job")

    assert not (workspace / ".staging").exists()
    assert (workspace / ".claude").exists(), "session data is not the staging tree"


@pytest.mark.asyncio
async def test_clone_chat_repos_purges_staging_before_it_starts(
    tmp_path, monkeypatch
) -> None:
    """A preparation removes what a killed clone left before cloning anything."""
    from rootcoz.engine import chat

    workspace = tmp_path / "workspace"
    stale = _clone_into_staging(workspace, "repo")
    (stale / ".git").mkdir(parents=True)

    monkeypatch.setattr(RepositoryManager, "clone_into", lambda *a, **k: None)
    await chat.clone_chat_repos(
        workspace, {"tests_repo_url": "https://example.com/tests-repo"}
    )

    assert not stale.exists()
