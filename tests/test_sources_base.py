"""Tests for shared CI source workspace helpers in ``sources.base``."""

from __future__ import annotations

from pathlib import Path

from rootcoz.sources.base import link_artifacts_to_workspace, link_refetched_artifacts


class TestLinkArtifactsToWorkspace:
    """link_artifacts_to_workspace must not false-negative on existing paths."""

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

    def test_existing_symlink_returns_true_keeps_context(self, tmp_path: Path) -> None:
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

    def test_existing_directory_returns_true(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        extract = tmp_path / "extract"
        extract.mkdir()
        (repo / "build-artifacts").mkdir()

        assert link_artifacts_to_workspace(repo, extract, "job-1") is True
