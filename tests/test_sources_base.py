"""Tests for shared CI source workspace helpers in ``sources.base``."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rootcoz.sources.base import (
    CISource,
    link_artifacts_to_workspace,
    link_refetched_artifacts,
    write_console_output_file,
)
from rootcoz.sources.jenkins_source import JenkinsSource
from rootcoz.sources.prow_source import ProwSource


@pytest.mark.asyncio
async def test_workspace_clone_reports_test_repo_start_and_end(tmp_path: Path) -> None:
    from rootcoz.sources.base import setup_analysis_workspace

    manager = MagicMock()
    manager.create_workspace.return_value = tmp_path
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, bool]] = []

    async def clone(*_args, **_kwargs):
        started.set()
        await release.wait()

    async def progress(_job_id: str, name: str, active: bool, **_kwargs) -> None:
        events.append((name, active))

    with (
        patch("rootcoz.sources.base.asyncio.to_thread", side_effect=clone),
        patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress),
    ):
        task = asyncio.create_task(
            setup_analysis_workspace(
                manager, tests_repo_url="https://example.com/tests", job_id="job"
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert events == [("tests", True)]
        finally:
            release.set()
        await task
    assert events == [("tests", True), ("tests", False)]


@pytest.mark.asyncio
async def test_workspace_failed_clone_reports_failed(
    tmp_path: Path, monkeypatch
) -> None:
    from rootcoz import storage
    from rootcoz.sources.base import setup_analysis_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    manager = MagicMock()
    manager.create_workspace.return_value = tmp_path
    manager.clone_into.side_effect = RuntimeError("clone failed")
    await setup_analysis_workspace(
        manager,
        tests_repo_url="https://user:secret@example.com/tests",  # pragma: allowlist secret
        tests_repo_ref="main",
        job_id="job",
    )
    result = (await storage.get_result("job"))["result"]
    assert result["cloning_repos"] == []
    assert [(e["repo"], e["state"]) for e in result["progress_log"]] == [
        ("tests", "cloning"),
        ("tests", "failed"),
    ]
    assert [(e["url"], e["ref"]) for e in result["progress_log"]] == [
        ("https://example.com/tests", "main"),
        ("https://example.com/tests", "main"),
    ]
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_workspace_cancelled_clone_reports_cancelled(
    tmp_path: Path, monkeypatch
) -> None:
    from rootcoz import storage
    from rootcoz.sources.base import setup_analysis_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    manager = MagicMock()
    manager.create_workspace.return_value = tmp_path
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def clone(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    manager.clone_into.side_effect = clone
    task = asyncio.create_task(
        setup_analysis_workspace(
            manager, tests_repo_url="https://example.com/tests", job_id="job"
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    result = (await storage.get_result("job"))["result"]
    assert result["cloning_repos"] == []
    assert [(e["repo"], e["state"]) for e in result["progress_log"]] == [
        ("tests", "cloning"),
        ("tests", "cancelled"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_thread_clone_finishes_before_progress_and_cleanup(
    tmp_path: Path, repeat_cancel: bool, worker_fails: bool, caplog
) -> None:
    from rootcoz.sources.base import setup_analysis_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    events: list[str] = []
    manager = MagicMock()
    manager.create_workspace.return_value = workspace

    def clone(_url: str, target: Path, **_kwargs) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release clone thread"
        assert workspace.exists(), "workspace was removed while clone was running"
        if worker_fails:
            events.append("worker finished")
            raise RuntimeError("secret=not-for-logs")
        target.mkdir()
        events.append("worker finished")

    def cleanup() -> None:
        events.append("cleanup")
        shutil.rmtree(workspace)

    async def progress(_job: str, _repo: str, active: bool, **_kwargs) -> None:
        events.append("cloning" if active else "cancelled")

    manager.clone_into.side_effect = clone
    manager.cleanup.side_effect = cleanup

    async def caller() -> None:
        try:
            await setup_analysis_workspace(
                manager, tests_repo_url="https://example.com/tests", job_id="job"
            )
        finally:
            manager.cleanup()

    with patch("rootcoz.engine.core.safe_update_clone_progress", side_effect=progress):
        task = asyncio.create_task(caller())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0.02)
            if repeat_cancel:
                task.cancel()
                await asyncio.sleep(0.02)
            assert events == ["cloning"]
            assert not task.done()
            assert workspace.exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert events == ["cloning", "worker finished", "cancelled", "cleanup"]
    assert "not-for-logs" not in caplog.text


class TestLinkArtifactsToWorkspace:
    """link_artifacts_to_workspace must validate and repair artifact symlinks."""

    def test_creates_symlink(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        (extract / "log.txt").write_text("ok")

        assert link_artifacts_to_workspace(repo, extract, "job-1") is True
        link = repo / "build-artifacts"
        assert link.is_symlink()
        assert link.resolve() == extract.resolve()

    def test_existing_valid_symlink_returns_true_keeps_context(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        link = repo / "build-artifacts"
        link.symlink_to(extract)

        assert link_artifacts_to_workspace(repo, extract, "job-1") is True
        # Callers must keep artifacts_context when link already exists
        assert (
            link_refetched_artifacts(repo, extract, "Artifacts available", "job-1")
            == "Artifacts available"
        )

    def test_broken_symlink_is_repaired(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        (extract / "log.txt").write_text("ok")
        link = repo / "build-artifacts"
        link.symlink_to(tmp_path / "missing-extract")

        assert link_artifacts_to_workspace(repo, extract, "job-1") is True
        assert link.is_symlink()
        assert link.resolve() == extract.resolve()

    def test_wrong_target_symlink_is_repaired(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        link = repo / "build-artifacts"
        link.symlink_to(other)

        assert link_artifacts_to_workspace(repo, extract, "job-1") is True
        assert link.resolve() == extract.resolve()

    def test_existing_directory_returns_false_clears_context(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        (repo / "build-artifacts").mkdir()

        assert link_artifacts_to_workspace(repo, extract, "job-1") is False
        assert (
            link_refetched_artifacts(repo, extract, "Artifacts available", "job-1")
            == ""
        )

    def test_existing_file_returns_false_clears_context(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        (repo / "build-artifacts").write_text("not a symlink")

        assert link_artifacts_to_workspace(repo, extract, "job-1") is False
        assert (
            link_refetched_artifacts(repo, extract, "Artifacts available", "job-1")
            == ""
        )

    def test_missing_extract_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "missing-extract"

        assert link_artifacts_to_workspace(repo, extract, "job-1") is False
        assert not (repo / "build-artifacts").exists()

    def test_missing_extract_removes_stale_symlink(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        link = repo / "build-artifacts"
        link.symlink_to(other)
        extract = tmp_path / "missing-extract"

        assert link_artifacts_to_workspace(repo, extract, "job-1") is False
        assert not link.exists()
        assert not link.is_symlink()


class TestWriteConsoleOutputFile:
    """write_console_output_file shares console write/fallback across CI plugins."""

    def test_writes_raw_output(self, tmp_path: Path) -> None:
        assert write_console_output_file(tmp_path, "build log") is True
        assert (tmp_path / "console-output.txt").read_text(
            encoding="utf-8"
        ) == "build log"

    def test_empty_output_uses_fallback(self, tmp_path: Path) -> None:
        assert write_console_output_file(tmp_path, "") is True
        assert (tmp_path / "console-output.txt").read_text(
            encoding="utf-8"
        ) == "No console output available for this build."

    def test_none_output_uses_fallback(self, tmp_path: Path) -> None:
        assert write_console_output_file(tmp_path, None) is True
        assert "No console output" in (tmp_path / "console-output.txt").read_text(
            encoding="utf-8"
        )

    def test_unpaired_surrogate_writes_successfully(self, tmp_path: Path) -> None:
        """Surrogates must not abort chat workspace population."""
        assert write_console_output_file(tmp_path, "bad\ud800log") is True
        out = tmp_path / "console-output.txt"
        assert out.exists()
        # Replacement char for the surrogate under utf-8 errors=replace
        assert "bad" in out.read_text(encoding="utf-8")
        assert "log" in out.read_text(encoding="utf-8")


class TestCISourceCleanup:
    """Default CISource.cleanup uses _extract_path + _extract_label."""

    def test_base_cleanup_removes_extract_dir(self, tmp_path: Path) -> None:
        extract = tmp_path / "extract"
        extract.mkdir()
        (extract / "f.txt").write_text("x")

        class _Stub(CISource):
            _extract_label = "stub artifacts"

            async def fetch(self):  # pragma: no cover - unused
                raise NotImplementedError

        source = _Stub()
        source._extract_path = extract
        source.cleanup()
        assert not extract.exists()
        assert source._extract_path is None

    def test_prow_and_jenkins_share_base_cleanup(self) -> None:
        assert ProwSource.cleanup is CISource.cleanup
        assert JenkinsSource.cleanup is CISource.cleanup
        assert ProwSource._extract_label == "Prow artifacts"
        assert JenkinsSource._extract_label == "Jenkins artifacts"

    def test_prow_cleanup_uses_label(self, tmp_path: Path, monkeypatch) -> None:
        extract = tmp_path / "extract"
        extract.mkdir()
        called: dict[str, object] = {}

        def _fake_cleanup(path, label="extracted artifacts"):
            called["path"] = path
            called["label"] = label

        monkeypatch.setattr("rootcoz.sources.base.cleanup_extract_dir", _fake_cleanup)
        source = ProwSource(
            job_name="job",
            build_id="1",
            gcs_bucket="bucket",
            prow_url="https://prow.example.com",
        )
        source._extract_path = extract
        source.cleanup()
        assert called["path"] == extract
        assert called["label"] == "Prow artifacts"
        assert source._extract_path is None
