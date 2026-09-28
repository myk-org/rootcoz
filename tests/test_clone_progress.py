"""Live and persisted repository clone progress."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rootcoz import storage
from rootcoz.engine.core import clone_additional_repos, safe_update_clone_progress
from rootcoz.models import AdditionalRepo
from rootcoz.repository import RepositoryManager


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "aborted", "completed"])
async def test_late_clone_updates_do_not_change_terminal_result(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {"job_name": "test"})
    await storage.update_clone_progress("job", "slow", True)
    await storage.update_status("job", status, {"error": "cancelled"})
    before = await storage.get_result("job")

    # Clone completion can race with cancellation, after the terminal write commits.
    await asyncio.gather(
        storage.update_clone_progress("job", "slow", False),
        storage.update_clone_progress("job", "late", True),
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "aborted"])
async def test_reanalysis_clone_cannot_change_failed_or_cancelled_parent(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {"job_name": "test"})
    await storage.update_clone_progress("job", "slow", True, reanalysis=True)
    await storage.update_status("job", status, {"error": "stopped"})
    before = await storage.get_result("job")

    # A cancelled clone can still run its finally cleanup after the terminal commit.
    await asyncio.gather(
        storage.update_clone_progress("job", "slow", False, reanalysis=True),
        storage.update_clone_progress("job", "late", True, reanalysis=True),
    )
    assert await storage.get_result("job") == before


@pytest.mark.asyncio
async def test_reanalysis_clone_updates_completed_parent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "completed", {"progress_phase": "completed"})
    await storage.update_clone_progress("job", "repo", True, reanalysis=True)
    result = await storage.get_result("job")
    assert result["status"] == "completed"
    assert result["result"]["cloning_repos"] == ["repo"]
    await storage.update_clone_progress("job", "repo", False, reanalysis=True)
    assert (await storage.get_result("job"))["result"]["cloning_repos"] == []


@pytest.mark.asyncio
async def test_clone_progress_notifies_sse_after_persist() -> None:
    notifications: list[str] = []

    async def persist(job_id: str, _name: str, _started: bool) -> None:
        assert job_id == "job"
        notifications.append("persisted")

    with (
        patch("rootcoz.engine.core.update_clone_progress", side_effect=persist),
        patch(
            "rootcoz.engine.core._on_progress_updated",
            lambda _job: notifications.append("sse"),
        ),
    ):
        await safe_update_clone_progress("job", "tests", True)
    assert notifications == ["persisted", "sse"]


@pytest.mark.asyncio
async def test_parallel_clone_progress_and_failure(tmp_path: Path) -> None:
    """Both admitted clones appear; failed clones leave the active set."""
    repos = [
        AdditionalRepo(name=name, url=f"https://example.com/{name}")
        for name in ("one", "two")
    ]
    manager = MagicMock(spec=RepositoryManager)
    entered = asyncio.Event()
    release = asyncio.Event()
    active: set[str] = set()
    snapshots: list[set[str]] = []

    async def progress(_job_id: str, name: str, started: bool) -> None:
        if started:
            active.add(name)
        else:
            active.discard(name)
        snapshots.append(set(active))

    async def clone(_fn, _url, target, **_kwargs):
        if len(active) == 2:
            entered.set()
        await release.wait()
        if target.name == "two":
            raise RuntimeError("clone failed")

    with (
        patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone),
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
    ):
        task = asyncio.create_task(
            clone_additional_repos(manager, repos, tmp_path, job_id="job")
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert {"one", "two"} in snapshots
        finally:
            release.set()
        cloned, _ = await task
    assert set(cloned) == {"one"}
    assert active == set()


@pytest.mark.asyncio
async def test_cancel_during_start_progress_releases_clone_slot(tmp_path: Path) -> None:
    from rootcoz.engine import core

    manager = MagicMock(spec=RepositoryManager)
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def progress(_job: str, _name: str, started: bool) -> None:
        if started:
            entered.set()
            await blocker.wait()

    with (
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
        patch("rootcoz.engine.core.asyncio.to_thread") as worker,
    ):
        task = asyncio.create_task(
            clone_additional_repos(
                manager,
                [AdditionalRepo(name="one", url="https://example.com/one")],
                tmp_path,
                job_id="job",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        before = core._active_clones
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert core._active_clones == before - 1
        worker.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_clone_clears_active_name(tmp_path: Path) -> None:
    """A cancelled analysis leaves no stale active repo in progress."""
    manager = MagicMock(spec=RepositoryManager)
    started = asyncio.Event()
    release = asyncio.Event()
    active: set[str] = set()

    async def progress(_job_id: str, name: str, running: bool) -> None:
        if running:
            active.add(name)
        else:
            active.discard(name)

    async def clone(*_args, **_kwargs):
        started.set()
        await release.wait()

    with (
        patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone),
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
    ):
        task = asyncio.create_task(
            clone_additional_repos(
                manager,
                [AdditionalRepo(name="one", url="https://example.com/one")],
                tmp_path,
                job_id="job",
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        assert active == {"one"}
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert active == set()
        finally:
            release.set()
