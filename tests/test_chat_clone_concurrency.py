"""Chat clones repos through the same concurrent fan-out as analysis (#chat).

Opening a chat used to wait for the sum of N clones because chat ran its own
sequential loop; the loop is gone, so these are the behaviours that must hold on
the shared path.
"""

import logging
import threading
import time
from pathlib import Path

import pytest

from rootcoz.config import Settings
from rootcoz.engine import core as core_module
from rootcoz.engine.chat import clone_chat_repos
from rootcoz.repository import RepositoryManager


@pytest.fixture
def clone_settings(monkeypatch):
    """A clone budget wide enough that the fan-out, not the budget, is measured."""
    monkeypatch.setattr(
        core_module,
        "get_settings",
        lambda: Settings(max_concurrent_repo_clones=4, max_concurrent_ai_calls=4),
    )


def _repos(count: int) -> list[dict[str, str]]:
    return [
        {"name": f"repo{i}", "url": f"https://example.com/repo{i}"}
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_clones_overlap_instead_of_running_one_at_a_time(
    monkeypatch, tmp_path: Path, clone_settings
) -> None:
    """Six clones of 0.1s each must cost waves, not 0.6s of wall clock."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def clone_into(_self, _url, _target, **_kwargs):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.1)
        with lock:
            in_flight -= 1

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    started = time.monotonic()
    cloned = await clone_chat_repos(workspace, {"additional_repos": _repos(6)})
    elapsed = time.monotonic() - started

    assert cloned is True
    assert peak >= 2, f"clones never overlapped (peak={peak}, {elapsed:.3f}s)"
    assert elapsed < 0.5, f"6 x 0.1s took {elapsed:.3f}s: the fan-out is serial"
    assert core_module._active_clones == 0


@pytest.mark.asyncio
async def test_one_failing_repo_warns_once_and_the_rest_still_clone(
    monkeypatch, tmp_path: Path, clone_settings, caplog
) -> None:
    """A failure is one redacted warning and one name; siblings are unaffected."""
    secret = "sekrit-token"  # pragma: allowlist secret
    cloned: list[str] = []

    def clone_into(_self, url, target, **_kwargs):
        if url.endswith("repo1"):
            raise OSError(
                f"fatal: unable to access 'https://x-token-auth:{secret}@example.com/repo1'"
            )
        cloned.append(target.name)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    failed: list[str] = []

    chat_logger = logging.getLogger("rootcoz.engine.chat")
    monkeypatch.setattr(chat_logger, "propagate", True)
    # One shared failure line, on the shared module's logger (#514).
    core_logger = logging.getLogger("rootcoz.engine.core")
    monkeypatch.setattr(core_logger, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="rootcoz.engine.core"):
        cloned_any = await clone_chat_repos(
            workspace, {"additional_repos": _repos(3)}, failed=failed
        )

    assert cloned_any is True
    assert failed == ["repo1"]
    assert sorted(cloned) == ["repo0", "repo2"]
    warnings = [r for r in caplog.records if "Failed to clone repo repo1" in r.message]
    assert len(warnings) == 1, [r.message for r in caplog.records]
    assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_token"),
    [
        ("https://github.com/acme/tests-repo.git", "tok"),
        ("https://gitlab.com/acme/tests-repo.git", None),
    ],
)
async def test_repo_token_only_reaches_github_hosts(
    monkeypatch, tmp_path: Path, clone_settings, url: str, expected_token: str | None
) -> None:
    """The load-bearing credential guard: no token towards a non-GitHub host."""
    seen: list[str | None] = []

    def clone_into(_self, _url, _target, token=None, **_kwargs):
        seen.append(token)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await clone_chat_repos(workspace, {"tests_repo_url": url}, user_repo_token="tok")

    assert seen == [expected_token]


@pytest.mark.asyncio
async def test_on_repo_names_each_repo_before_its_clone_starts(
    monkeypatch, tmp_path: Path, clone_settings
) -> None:
    """``preparing.phase`` follows the clone that is actually running."""
    cloned: list[str] = []
    unnamed: list[str] = []

    def clone_into(_self, _url, target, **_kwargs):
        cloned.append(target.name)
        if target.name not in reported:
            unnamed.append(target.name)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reported: list[str] = []

    await clone_chat_repos(
        workspace,
        {"additional_repos": _repos(3)},
        on_repo=reported.append,
    )

    # Each repo is named before its own clone starts, in the order they started.
    assert reported[:3] == ["repo0", "repo1", "repo2"]
    assert sorted(cloned) == ["repo0", "repo1", "repo2"]
    assert unnamed == [], f"clone started before on_repo named it: {unnamed}"
    # A finished batch names no repo at all: the last publication is the clear.
    assert reported[-1] == ""


@pytest.mark.asyncio
async def test_a_finished_clone_stops_being_named(
    monkeypatch, tmp_path: Path, clone_settings
) -> None:
    """The detail only ever names a clone that is still running.

    ``repo1`` finishes first and its completion must move the detail back to the
    clone still in flight, not leave ``repo1`` on screen.
    """
    published: list[str] = []
    release_repo0 = threading.Event()

    def on_repo(detail: str) -> None:
        published.append(detail)
        if detail == "repo0" and "repo1" in published:
            # repo1's completion was published; let repo0 finish now.
            release_repo0.set()

    def clone_into(_self, _url, target, **_kwargs):
        if target.name == "repo0":
            assert release_repo0.wait(5), "sibling clone never finished"

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await clone_chat_repos(workspace, {"additional_repos": _repos(2)}, on_repo=on_repo)

    assert published == ["repo0", "repo1", "repo0", ""]


async def test_unsafe_name_is_skipped_and_the_others_clone(
    monkeypatch, tmp_path: Path, clone_settings
) -> None:
    """A name that escapes the workspace is skipped, never fatal."""
    cloned: list[str] = []

    def clone_into(_self, _url, target, **_kwargs):
        cloned.append(target.name)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    failed: list[str] = []

    cloned_any = await clone_chat_repos(
        workspace,
        {
            "tests_repo_url": "https://example.com/.hidden",
            "additional_repos": _repos(1),
        },
        failed=failed,
    )

    assert cloned == ["repo0"]
    assert cloned_any is True
    # A refused repository is named in ``failed`` so the "not in this workspace"
    # row reports it; dropping it silently is what #514 removes.
    assert failed == [".hidden"]
