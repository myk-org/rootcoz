"""Tests for shared CI source workspace helpers in ``sources.base``."""

from __future__ import annotations

from pathlib import Path

from rootcoz.sources.base import link_artifacts_to_workspace, link_refetched_artifacts


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
