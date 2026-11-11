"""Chat clones repos through the same concurrent fan-out as analysis (#chat).

Opening a chat used to wait for the sum of N clones because chat ran its own
sequential loop; the loop is gone, so these are the behaviours that must hold on
the shared path.
"""

import logging
import re
import threading
import time
from pathlib import Path

import pytest

from rootcoz.config import Settings
from rootcoz.engine import core as core_module
from rootcoz.engine.chat import clone_chat_repos
from rootcoz.repository import RepositoryManager
from tests.conftest import capture_main_logs


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

    # One shared failure line, on the shared module's logger (#514).
    core_logger = logging.getLogger("rootcoz.engine.core")
    # Two repos fan out concurrently, so an identical warning can be emitted
    # twice; the duplicate filter is taken off so the count below counts emits.
    with capture_main_logs(
        caplog,
        logging.WARNING,
        logger=core_logger,
        bypass_duplicate_filter=True,
    ):
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
    "url",
    [
        "https://github.com/acme/tests-repo.git",
        # The repo the caller named, on whatever host they named it: chat's token
        # is a read credential here, and dropping it would break self-hosted and
        # GitLab tests repos (#514).
        "https://gitlab.internal/acme/tests-repo.git",
    ],
)
async def test_the_chat_token_reaches_the_tests_repo_on_any_host(
    monkeypatch, tmp_path: Path, clone_settings, url: str
) -> None:
    """Chat is a read path: no host is special-cased, so its tests repo gets it.

    The write path is what guards credentials. A host gate here withheld the
    token from exactly the self-hosted and GitLab tests repos a user asks to
    clone, which matches main's behaviour and main's docstring.
    """
    seen: list[str | None] = []

    def clone_into(_self, _url, _target, token=None, **_kwargs):
        seen.append(token)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await clone_chat_repos(workspace, {"tests_repo_url": url}, user_repo_token="tok")

    assert seen == ["tok"]


@pytest.mark.asyncio
async def test_prep_detail_names_the_width_not_one_repo(
    monkeypatch, tmp_path: Path, clone_settings
) -> None:
    """The detail shows how many clones are in flight, and drops as they end (#547).

    A serial implementation can only ever publish ``cloning 1 of 2``, so naming
    two at once is the assertion that separates wide from serial -- and a
    finished clone leaves the count instead of lingering on screen.
    """
    published: list[str] = []
    release_repo0 = threading.Event()

    def on_repo(detail: str) -> None:
        published.append(detail)
        if detail == "cloning 2 of 2":
            release_repo0.set()

    def clone_into(_self, _url, target, **_kwargs):
        # Hold the first clone open until the second is admitted, so the width
        # is what the detail reports rather than scheduling luck.
        if target.name == "repo0":
            assert release_repo0.wait(5), "the sibling clone was never admitted"

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await clone_chat_repos(workspace, {"additional_repos": _repos(2)}, on_repo=on_repo)

    assert published == ["cloning 1 of 2", "cloning 2 of 2", "cloning 1 of 2", ""]
    assert all(re.match(r"^cloning \d+ of \d+$", d) for d in published if d), published


@pytest.mark.asyncio
async def test_chat_clone_depth_reaches_both_repo_kinds(
    monkeypatch, tmp_path: Path
) -> None:
    """``chat_clone_depth`` -- not a hardcoded 50 -- decides every chat clone (#547)."""
    monkeypatch.setattr(
        "rootcoz.config.get_settings",
        lambda: Settings(chat_clone_depth=7, max_concurrent_repo_clones=4),
    )
    depths: dict[str, int] = {}

    def clone_into(_self, _url, target, depth=None, **_kwargs):
        depths[target.name] = depth

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await clone_chat_repos(
        workspace,
        {
            "tests_repo_url": "https://github.com/acme/tests-repo.git",
            "additional_repos": _repos(2),
        },
    )

    assert depths == {"tests-repo": 7, "repo0": 7, "repo1": 7}


def test_chat_clone_depth_defaults_to_one() -> None:
    """The AI reads the working tree, so history is paid for and never read."""
    assert Settings().chat_clone_depth == 1


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
