"""Tests for shared CI source workspace helpers in ``sources.base``."""

from __future__ import annotations

from pathlib import Path

from rootcoz.sources.base import (
    CISource,
    link_artifacts_to_workspace,
    link_refetched_artifacts,
    write_console_output_file,
)
from rootcoz.sources.jenkins_source import JenkinsSource
from rootcoz.sources.prow_source import ProwSource


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
        assert (tmp_path / "console-output.txt").read_text() == "build log"

    def test_empty_output_uses_fallback(self, tmp_path: Path) -> None:
        assert write_console_output_file(tmp_path, "") is True
        assert (
            tmp_path / "console-output.txt"
        ).read_text() == "No console output available for this build."

    def test_none_output_uses_fallback(self, tmp_path: Path) -> None:
        assert write_console_output_file(tmp_path, None) is True
        assert "No console output" in (tmp_path / "console-output.txt").read_text()


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
