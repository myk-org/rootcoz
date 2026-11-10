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
    with caplog.at_level(logging.WARNING, logger="rootcoz.engine.chat"):
        cloned_any = await clone_chat_repos(
            workspace, {"additional_repos": _repos(3)}, failed=failed
        )

    assert cloned_any is True
    assert failed == ["repo1"]
    assert sorted(cloned) == ["repo0", "repo2"]
    warnings = [r for r in caplog.records if "failed to clone repo repo1" in r.message]
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

    # Once per repo actually cloned, in the order the clones started.
    assert reported == ["repo0", "repo1", "repo2"]
    assert sorted(cloned) == reported
    assert unnamed == [], f"clone started before on_repo named it: {unnamed}"


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
    assert failed == []
