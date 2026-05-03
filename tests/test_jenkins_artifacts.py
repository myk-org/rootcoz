"""Tests for Jenkins artifacts tar/zip extraction and context building."""

import io
import shutil
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from jenkins_job_insight import jenkins_artifacts
from jenkins_job_insight.jenkins_artifacts import (
    build_artifacts_context,
    cleanup_extract_dir,
    download_artifact,
    process_build_artifacts,
    store_artifact,
    validate_and_extract_archive,
)


@pytest.fixture
def extract_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override EXTRACT_BASE to use tmp_path so tests never leave files in /tmp."""
    base = tmp_path / "extract-base"
    base.mkdir()
    monkeypatch.setattr(jenkins_artifacts, "EXTRACT_BASE", base)
    return base


def _make_tar_gz(files: dict[str, str]) -> bytes:
    """Create a gzip-compressed tar archive in memory.

    Args:
        files: Mapping of archive member names to their text content.

    Returns:
        Raw bytes of the tar.gz archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_tar_uncompressed(files: dict[str, str]) -> bytes:
    """Create an uncompressed tar archive in memory.

    Args:
        files: Mapping of archive member names to their text content.

    Returns:
        Raw bytes of the tar archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestValidateAndExtractTar:
    """Tests for validate_and_extract_archive (tar archives)."""

    def test_valid_tar_gz(self, extract_base: Path) -> None:
        """Valid tar.gz archive is extracted and files are accessible."""
        tar_data = _make_tar_gz(
            {
                "logs/app.log": "some log content\n",
                "config.yaml": "key: value\n",
            }
        )

        result = validate_and_extract_archive(tar_data)

        assert result.exists()
        assert result.is_dir()
        assert (result / "logs" / "app.log").read_text() == "some log content\n"
        assert (result / "config.yaml").read_text() == "key: value\n"

    def test_valid_tar_uncompressed(self, extract_base: Path) -> None:
        """Valid uncompressed tar archive is extracted and files are accessible."""
        tar_data = _make_tar_uncompressed(
            {
                "data/report.txt": "hello world\n",
            }
        )

        result = validate_and_extract_archive(tar_data)

        assert result.exists()
        assert (result / "data" / "report.txt").read_text() == "hello world\n"

    def test_size_exceeds_max(self, extract_base: Path) -> None:
        """Tar data exceeding max_size_mb returns None."""
        tar_data = _make_tar_gz({"file.txt": "x"})

        assert validate_and_extract_archive(tar_data, max_size_mb=0) is None

    def test_invalid_tar_data(self, extract_base: Path) -> None:
        """Random bytes that are not a valid tar return None."""
        assert (
            validate_and_extract_archive(b"this is definitely not a tar file") is None
        )

    def test_zip_slip_prevention(self, extract_base: Path, tmp_path: Path) -> None:
        """Tar entries with path traversal are skipped, not extracted."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="../../../etc/passwd")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"hello"))
            # Add a safe file too
            safe_info = tarfile.TarInfo(name="safe.txt")
            safe_info.size = 4
            tar.addfile(safe_info, io.BytesIO(b"safe"))

        extract_path = validate_and_extract_archive(buf.getvalue(), max_size_mb=10)
        try:
            # Safe file should exist
            assert (extract_path / "safe.txt").exists()
            # Unsafe file should NOT exist anywhere
            assert not (extract_path / "etc" / "passwd").exists()
        finally:
            shutil.rmtree(extract_path, ignore_errors=True)


class TestExtractZip:
    """Tests for validate_and_extract_archive (zip archives)."""

    def test_valid_zip(self, extract_base: Path) -> None:
        """Valid zip archive is extracted and files are accessible."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("logs/app.log", "some log content\n")
            zf.writestr("config.yaml", "key: value\n")
        zip_data = buf.getvalue()

        result = validate_and_extract_archive(zip_data)

        assert result.exists()
        assert result.is_dir()
        assert (result / "logs" / "app.log").read_text() == "some log content\n"
        assert (result / "config.yaml").read_text() == "key: value\n"

    def test_invalid_zip_data(self, extract_base: Path) -> None:
        """Random bytes that are neither a valid tar nor zip return None."""
        assert (
            validate_and_extract_archive(b"this is definitely not an archive") is None
        )

    def test_zip_path_traversal(self, extract_base: Path) -> None:
        """Zip entries with path traversal are skipped, not extracted."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../../etc/passwd", "malicious")
            zf.writestr("safe.txt", "safe content")

        extract_path = validate_and_extract_archive(buf.getvalue(), max_size_mb=10)
        try:
            # Safe file should exist
            assert (extract_path / "safe.txt").exists()
            # Unsafe file should NOT exist
            assert not (extract_path / "etc" / "passwd").exists()
        finally:
            shutil.rmtree(extract_path, ignore_errors=True)


class TestBuildArtifactsContext:
    """Tests for build_artifacts_context."""

    def test_does_not_include_error_lines_in_context(self, tmp_path: Path) -> None:
        """Context contains directory structure but not pre-extracted error lines."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "app.log"
        log_file.write_text(
            "2024-01-01 INFO Starting up\n"
            "2024-01-01 ERROR Connection refused\n"
            "2024-01-01 INFO Retrying\n"
            "2024-01-01 FAILURE Something broke\n"
        )

        context = build_artifacts_context(tmp_path)

        assert "BUILD ARTIFACTS CONTEXT" in context
        assert "logs/" in context
        # Error lines should NOT be pre-extracted into the context
        assert "Connection refused" not in context
        assert "FAILURE Something broke" not in context

    def test_does_not_include_warning_events_in_context(self, tmp_path: Path) -> None:
        """Context contains directory structure but not pre-extracted warning events."""
        event_dir = tmp_path / "cluster-scoped-resources"
        event_dir.mkdir(parents=True)
        event_file = event_dir / "events.yaml"
        event_file.write_text(
            "kind: Event\n"
            "type: Warning\n"
            "message: Back-off restarting failed container\n"
            "type: Normal\n"
            "message: Scheduled successfully\n"
        )

        context = build_artifacts_context(tmp_path)

        assert "BUILD ARTIFACTS CONTEXT" in context
        assert "cluster-scoped-resources/" in context
        # Warning events should NOT be pre-extracted
        assert "Warning Events" not in context
        assert "type: Warning" not in context

    def test_does_not_include_status_issues_in_context(self, tmp_path: Path) -> None:
        """Context contains directory structure but not pre-extracted status issues."""
        resource_dir = tmp_path / "namespaces" / "default"
        resource_dir.mkdir(parents=True)
        pod_file = resource_dir / "pods.yaml"
        pod_file.write_text(
            "apiVersion: v1\n"
            "kind: Pod\n"
            "status:\n"
            "  phase: Failed\n"
            "  containerStatuses:\n"
            "    - state:\n"
            "        terminated:\n"
            "          reason: Error pulling image\n"
        )

        context = build_artifacts_context(tmp_path)

        assert "BUILD ARTIFACTS CONTEXT" in context
        assert "namespaces/default/" in context
        # Status issues should NOT be pre-extracted
        assert "Abnormal Status Indicators" not in context

    def test_empty_directory_returns_note(self, tmp_path: Path) -> None:
        """Empty directory still produces valid context with header."""
        context = build_artifacts_context(tmp_path)

        assert "BUILD ARTIFACTS CONTEXT" in context
        assert "Contains 0 files" in context
        # The old "no issues found" message should not appear
        assert "No errors, warnings, or status issues found" not in context

    def test_root_only_files_listed_in_directory_structure(
        self, tmp_path: Path
    ) -> None:
        """Root-level files with no subdirectories still produce a directory structure."""
        (tmp_path / "build.log").write_text("some log content\n")
        (tmp_path / "results.xml").write_text("<results/>\n")

        context = build_artifacts_context(tmp_path)

        assert "BUILD ARTIFACTS CONTEXT" in context
        assert "Directory Structure" in context
        assert "build.log" in context
        assert "results.xml" in context


class TestCleanupExtractDir:
    """Tests for cleanup_extract_dir."""

    def test_cleanup_removes_directory(self, tmp_path: Path) -> None:
        """Cleanup removes the target directory tree."""
        target = tmp_path / "jenkins-artifacts-abc123"
        target.mkdir()
        (target / "nested").mkdir()
        (target / "nested" / "file.txt").write_text("data")

        cleanup_extract_dir(target)

        assert not target.exists()

    def test_cleanup_nonexistent_dir_no_error(self, tmp_path: Path) -> None:
        """Cleanup of a non-existent path does not raise."""
        nonexistent = tmp_path / "does-not-exist"

        # Should not raise; errors are logged as warnings and swallowed
        cleanup_extract_dir(nonexistent)


class TestDownloadArtifact:
    """Tests for download_artifact."""

    @pytest.fixture
    def session(self) -> MagicMock:
        """Create a mock requests.Session."""
        return MagicMock()

    def test_download_artifact_success(self, session: MagicMock) -> None:
        """Successful download returns concatenated chunk bytes."""
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [b"chunk1", b"chunk2", b"chunk3"]
        response.close = Mock()
        session.get.return_value = response

        result = download_artifact(
            session, "https://jenkins.example.com/job/test/1", "artifacts/report.tar.gz"
        )

        assert result == b"chunk1chunk2chunk3"
        session.get.assert_called_once_with(
            "https://jenkins.example.com/job/test/1/artifact/artifacts/report.tar.gz",
            stream=True,
            timeout=60,
        )
        response.close.assert_called_once()

    def test_download_artifact_http_error(self, session: MagicMock) -> None:
        """Non-200 response returns None."""
        response = Mock()
        response.status_code = 404
        response.close = Mock()
        session.get.return_value = response

        result = download_artifact(
            session, "https://jenkins.example.com/job/test/1", "missing.tar.gz"
        )

        assert result is None
        response.close.assert_called_once()

    def test_download_artifact_size_exceeded(self, session: MagicMock) -> None:
        """Download exceeding max_size_mb returns None."""
        response = Mock()
        response.status_code = 200
        # Each chunk is 1 MB; max_size_mb=1 so second chunk exceeds limit
        one_mb = b"x" * (1024 * 1024)
        response.iter_content.return_value = [one_mb, one_mb]
        response.close = Mock()
        session.get.return_value = response

        result = download_artifact(
            session,
            "https://jenkins.example.com/job/test/1",
            "huge.tar.gz",
            max_size_mb=1,
        )

        assert result is None
        response.close.assert_called_once()

    def test_download_artifact_exception(self, session: MagicMock) -> None:
        """Network exception returns None without crashing."""
        session.get.side_effect = ConnectionError("connection refused")

        result = download_artifact(
            session, "https://jenkins.example.com/job/test/1", "artifact.tar.gz"
        )

        assert result is None


class TestStoreArtifact:
    """Tests for store_artifact."""

    def test_store_artifact_raw_file(self, tmp_path: Path) -> None:
        """Non-archive file is written to disk as-is."""
        data = b"plain text content"

        store_artifact("reports/output.txt", data, tmp_path)

        dest = tmp_path / "reports" / "output.txt"
        assert dest.exists()
        assert dest.read_bytes() == data

    def test_store_artifact_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Archive file is extracted into a subdirectory."""
        # Monkeypatch EXTRACT_BASE so validate_and_extract_archive uses tmp_path
        monkeypatch.setattr(jenkins_artifacts, "EXTRACT_BASE", tmp_path)

        tar_data = _make_tar_gz({"inner/file.txt": "extracted content\n"})

        store_artifact("logs/must-gather.tar.gz", tar_data, tmp_path)

        # The archive should be extracted into a directory named after the archive
        # minus its extension
        extract_dir = tmp_path / "logs" / "must-gather"
        assert extract_dir.exists()
        assert (extract_dir / "inner" / "file.txt").read_text() == "extracted content\n"
        # The raw .tar.gz file should NOT exist (extraction succeeded)
        assert not (tmp_path / "logs" / "must-gather.tar.gz").exists()

    def test_store_artifact_failed_archive_fallback(self, tmp_path: Path) -> None:
        """Invalid archive data falls back to storing as raw file."""
        bad_data = b"this is not a valid zip"

        store_artifact("data/archive.zip", bad_data, tmp_path)

        # Should be stored as a raw file since extraction fails
        dest = tmp_path / "data" / "archive.zip"
        assert dest.exists()
        assert dest.read_bytes() == bad_data


class TestProcessBuildArtifacts:
    """Tests for process_build_artifacts."""

    @pytest.fixture
    def extract_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Override EXTRACT_BASE to use tmp_path."""
        base = tmp_path / "extract-base"
        base.mkdir()
        monkeypatch.setattr(jenkins_artifacts, "EXTRACT_BASE", base)
        return base

    @pytest.fixture
    def session(self) -> MagicMock:
        """Create a mock requests.Session."""
        return MagicMock()

    def test_process_build_artifacts_empty_list(
        self, session: MagicMock, extract_base: Path
    ) -> None:
        """Empty artifact list returns empty context and None path."""
        context, artifacts_dir = process_build_artifacts(
            session, "https://jenkins.example.com/job/test/1", []
        )

        assert context == ""
        assert artifacts_dir is None

    def test_process_build_artifacts_downloads_and_stores(
        self, session: MagicMock, extract_base: Path
    ) -> None:
        """Artifacts are downloaded, stored, and context is built."""
        artifact_list = [
            {"relativePath": "logs/app.log"},
            {"relativePath": "config.yaml"},
        ]

        def fake_get(url: str, **kwargs):
            response = Mock()
            response.status_code = 200
            response.close = Mock()
            if "app.log" in url:
                response.iter_content.return_value = [
                    b"2024-01-01 ERROR something failed\n"
                ]
            else:
                response.iter_content.return_value = [b"key: value\n"]
            return response

        session.get.side_effect = fake_get

        context, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            artifact_list,
        )

        assert artifacts_dir is not None
        assert artifacts_dir.exists()
        # Both artifacts should be stored
        assert (artifacts_dir / "logs" / "app.log").exists()
        assert (artifacts_dir / "config.yaml").exists()
        # Context should contain artifacts output from the stored files
        assert "BUILD ARTIFACTS CONTEXT" in context

        # Cleanup
        shutil.rmtree(artifacts_dir, ignore_errors=True)

    def test_process_build_artifacts_all_downloads_fail(
        self, session: MagicMock, extract_base: Path
    ) -> None:
        """When all downloads fail, returns failure note and None path."""
        artifact_list = [
            {"relativePath": "artifact1.tar.gz"},
            {"relativePath": "artifact2.zip"},
        ]

        response = Mock()
        response.status_code = 404
        response.close = Mock()
        session.get.return_value = response

        context, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            artifact_list,
        )

        assert "No artifacts could be downloaded" in context
        assert artifacts_dir is None
