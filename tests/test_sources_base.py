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
from tests.conftest import capture_main_logs


@pytest.fixture(autouse=True)
def _caplog_reaches_module_logger(caplog):
    """get_child_logger sets propagate=False, so caplog needs its own attach here."""
    import logging

    from rootcoz.engine import core as core_module
    from rootcoz.sources import base as base_module

    # The clone-worker join lives in the shared path (#514), so its line is
    # emitted on core's logger, not the caller's.
    with capture_main_logs(
        caplog, logging.INFO, logger=[base_module.logger, core_module.logger]
    ):
        yield


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
        patch("rootcoz.engine.core.asyncio.to_thread", side_effect=clone),
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
async def test_tests_repo_failure_records_the_label(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed tests-repo clone stores the label and says it in the prompt context."""
    from git.exc import GitCommandError

    from rootcoz import storage
    from rootcoz.sources.base import setup_analysis_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "progress.db")
    await storage.init_db()
    await storage.save_result("job", "", "running", {})
    manager = MagicMock()
    manager.create_workspace.return_value = tmp_path
    manager.clone_into.side_effect = GitCommandError(
        "git clone", 128, stderr="fatal: unable to read tree (abc)"
    )

    result, _ = await setup_analysis_workspace(
        manager,
        tests_repo_url="https://example.com/tests",
        tests_repo_ref="a" * 40,
        job_id="job",
    )

    stored = (await storage.get_result("job"))["result"]["progress_log"]
    failed = [entry for entry in stored if entry["state"] == "failed"]
    assert failed and failed[0]["reason"] == "Commit or ref not found"
    assert result.repo_context == (
        "\nFailed to clone repository (Commit or ref not found)"
    )
    # A label, not a URL: the prompt line reaches the AI.
    assert "example.com" not in result.repo_context


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
    # A cancelled clone names no cause: the default label is not a failure.
    assert "reason" not in result["progress_log"][-1]


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
    # Positive control: this capture does see the module logger, so the absence
    # below is a real absence and not an empty capture.
    assert "clone worker joined after cancellation" in caplog.text
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


class TestRefusalSurvivesTheFriendlyMapper:
    """A refusal is already the sentence, so the mapper never re-labels it."""

    def test_refusal_sentence_is_returned_verbatim(self) -> None:
        from rootcoz.ai_client import SESSION_REFUSAL_MARKER
        from rootcoz.error_messages import make_user_friendly_error

        sentence = (
            f"{SESSION_REFUSAL_MARKER}: the session could not be created for"
            " openai/undeclared (sidecar HTTP 404). Ask an administrator to check"
            " the server log for the sidecar's reason."
        )
        assert make_user_friendly_error(sentence) == sentence

    def test_a_non_refusal_404_still_maps_to_jenkins(self) -> None:
        from rootcoz.error_messages import make_user_friendly_error

        assert "no longer available" in make_user_friendly_error(
            "sidecar 404 for openai/undeclared"
        )


class TestRunConsoleOnlyAnalysisIncomplete:
    """A narration-only answer is a re-analysis problem, not a credential one."""

    @pytest.mark.asyncio
    async def test_no_usable_classification_is_not_blamed_on_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rootcoz.engine.core import AnalysisIncompleteError
        from rootcoz.error_messages import make_user_friendly_error
        from rootcoz.sources.base import run_console_only_analysis

        async def _raise(**_kwargs: object) -> None:
            raise AnalysisIncompleteError(
                "AI analysis incomplete: response has no usable classification; "
                "re-analyze the job",
                error_signature="sig123",
                raw_text="Now let me read the test source code.",
            )

        monkeypatch.setattr("rootcoz.engine.core.analyze_failure_group", _raise)

        success, failures, error_text = await run_console_only_analysis(
            test_name="my-job#7",
            console_context="console output",
            artifacts_context="",
            repo_path=None,
            ai_provider="claude",
            ai_model="opus",
            ai_call_timeout=None,
            custom_prompt="",
            server_url="",
            job_id="job-1",
            additional_repos=None,
            auth_header="",
        )

        assert success is False
        assert "no usable classification" in error_text
        assert "credential" not in error_text.lower()
        # What the user reads must not blame the credentials either.
        assert "credential" not in make_user_friendly_error(error_text).lower()
        assert len(failures) == 1
        assert failures[0].analysis.classification == ""
        assert "no usable classification" in failures[0].analysis_error
        assert "credential" not in failures[0].analysis_error.lower()


def test_cleanup_extract_dir_reports_survivors(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A cleanup that left paths behind must not log ``Cleanup complete``."""
    import errno
    import logging
    import os
    import re

    from rootcoz.sources.base import cleanup_extract_dir

    extract = tmp_path / "extract"
    extract.mkdir()
    (extract / "stuck.txt").write_text("x")
    real_unlink = os.unlink

    def refuse_stuck(path, *args, **kwargs):
        if Path(os.fspath(path)).name == "stuck.txt":
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse_stuck)
    with caplog.at_level(logging.INFO):
        cleanup_extract_dir(extract, label="extracted artifacts")

    assert "Cleanup complete" not in caplog.text
    survived = re.search(r"survived=(\d+)", caplog.text)
    assert survived and int(survived.group(1)) >= 1
    assert extract.exists()
