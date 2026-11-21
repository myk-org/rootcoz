"""Tests for Jenkins artifacts tar/zip extraction and context building."""

import io
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from rootcoz import jenkins_artifacts
from rootcoz.jenkins_artifacts import (
    EXTRACT_SIZE_MULTIPLIER,
    _extract_nested_archives,
    _strip_archive_extension,
    cleanup_extract_dir,
    download_artifact,
    get_artifacts_path,
    process_build_artifacts,
    store_artifact,
    validate_and_extract_archive,
)


def test_extract_base_uses_process_tempdir() -> None:
    """EXTRACT_BASE must live under tempfile.gettempdir() for chat cleanup."""
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert jenkins_artifacts.EXTRACT_BASE == (Path(tempfile.gettempdir()) / "rootcoz")
    assert jenkins_artifacts.EXTRACT_BASE.resolve().is_relative_to(tmp_root)


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
    """Tests for get_artifacts_path."""

    def test_returns_path_string(self, tmp_path: Path) -> None:
        """get_artifacts_path returns the directory path as a string."""
        result = get_artifacts_path(tmp_path)
        assert result == str(tmp_path)

    def test_does_not_include_file_contents(self, tmp_path: Path) -> None:
        """Path string does not contain any file content."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "app.log").write_text("ERROR Connection refused\n")

        result = get_artifacts_path(tmp_path)
        assert "Connection refused" not in result
        assert result == str(tmp_path)


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
        # Context should be the artifacts directory path
        assert context == str(artifacts_dir)

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


def _make_zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    """Create a compressed zip archive in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestNestedArchiveTraversalSecurity:
    """Regression tests for #616 (nested archive extension-strip traversal)."""

    @pytest.mark.parametrize(
        "unsafe_name",
        [
            ".zip",
            "..zip",
            "...zip",
            ".tar.gz",
            "..tar.gz",
            "...tar.gz",
            "...tgz",
            "...tar.bz2",
            "...tar.xz",
            "...tar",
            "sub/name.zip",
            "sub\\name.zip",
        ],
    )
    def test_strip_archive_extension_rejects_unsafe_names(
        self, unsafe_name: str
    ) -> None:
        """Derived names that are empty, '.', '..', or contain separators return ''."""
        assert _strip_archive_extension(unsafe_name) == ""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("must-gather.tar.gz", "must-gather"),
            ("report.zip", "report"),
            (".hidden.zip", ".hidden"),
            ("a..b.tar.xz", "a..b"),
        ],
    )
    def test_strip_archive_extension_preserves_valid_names(
        self, filename: str, expected: str
    ) -> None:
        """Legitimate filenames keep their stripped stem."""
        assert _strip_archive_extension(filename) == expected

    def test_nested_dotdot_zip_cannot_escape_to_extract_base(
        self, extract_base: Path
    ) -> None:
        """Nested '...zip' chain cannot write files outside artifacts_dir into EXTRACT_BASE."""
        inner_payload = _make_zip_bytes({"pwned.txt": b"pwned"})
        depth1_zip = _make_zip_bytes({"...zip": inner_payload})
        outer_zip = _make_zip_bytes({"...zip": depth1_zip, "ok.txt": b"legit"})

        session = MagicMock()
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [outer_zip]
        response.close = Mock()
        session.get.return_value = response

        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            [{"relativePath": "outer.zip"}],
            max_size_mb=10,
        )

        assert artifacts_dir is not None
        assert not (extract_base / "pwned.txt").exists()
        assert (artifacts_dir / "outer" / "ok.txt").read_text() == "legit"

    def test_corrupt_nested_dotdot_zip_preserves_sibling_workspaces(
        self, extract_base: Path
    ) -> None:
        """Corrupt nested '...zip' must never delete sibling workspaces under EXTRACT_BASE."""
        victim_ws = extract_base / "victim-workspace"
        victim_ws.mkdir()
        (victim_ws / "precious.txt").write_text("do not delete")

        depth1_zip = _make_zip_bytes({"...zip": b"not a valid archive at all"})
        outer_zip = _make_zip_bytes({"...zip": depth1_zip})

        session = MagicMock()
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [outer_zip]
        response.close = Mock()
        session.get.return_value = response

        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            [{"relativePath": "outer.zip"}],
            max_size_mb=10,
        )

        assert artifacts_dir is not None
        assert victim_ws.exists()
        assert (victim_ws / "precious.txt").read_text() == "do not delete"

    def test_store_artifact_rejects_dotdot_archive_name(
        self, extract_base: Path
    ) -> None:
        """Top-level '...zip' or 'logs/...zip' must not extract into parent directory."""
        artifacts_dir = extract_base / "artifacts-job1"
        artifacts_dir.mkdir()
        (artifacts_dir / "logs").mkdir()
        (artifacts_dir / "logs" / "keep.txt").write_text("keep")

        bad_zip = _make_zip_bytes({"escape.txt": b"escaped"})
        store_artifact("...zip", bad_zip, artifacts_dir, max_size_mb=10)
        store_artifact("logs/...zip", b"corrupt", artifacts_dir, max_size_mb=10)

        assert not (extract_base / "escape.txt").exists()
        assert (artifacts_dir / "logs" / "keep.txt").read_text() == "keep"

    def test_validate_and_extract_never_deletes_extract_base_or_preexisting_dir(
        self, extract_base: Path
    ) -> None:
        """Failed extraction must never wipe EXTRACT_BASE or a pre-existing directory."""
        sibling = extract_base / "other-job"
        sibling.mkdir()
        (sibling / "data.txt").write_text("intact")

        assert (
            validate_and_extract_archive(b"not an archive", extract_dir=extract_base)
            is None
        )
        assert extract_base.exists()
        assert (sibling / "data.txt").read_text() == "intact"

        artifacts_dir = extract_base / "artifacts-job2"
        existing = artifacts_dir / "existing"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("still here")

        assert (
            validate_and_extract_archive(
                b"not an archive",
                extract_dir=existing,
                root_dir=artifacts_dir,
            )
            is None
        )
        assert (existing / "keep.txt").read_text() == "still here"

    def test_legitimate_nested_archives_happy_path(self, extract_base: Path) -> None:
        """Legitimate multi-level nested archives extract cleanly and remove originals."""
        inner_zip = _make_zip_bytes({"deep/report.txt": "nested ok\n"})
        outer_tar_buf = io.BytesIO()
        with tarfile.open(fileobj=outer_tar_buf, mode="w:gz") as tar:
            for name, payload in (
                ("logs/inner.zip", inner_zip),
                ("summary.txt", b"top ok\n"),
            ):
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

        session = MagicMock()
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [outer_tar_buf.getvalue()]
        response.close = Mock()
        session.get.return_value = response

        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            [{"relativePath": "bundle.tar.gz"}],
            max_size_mb=10,
        )

        assert artifacts_dir is not None
        assert (artifacts_dir / "bundle" / "summary.txt").read_text() == "top ok\n"
        assert (
            artifacts_dir / "bundle" / "logs" / "inner" / "deep" / "report.txt"
        ).read_text() == "nested ok\n"
        assert not (artifacts_dir / "bundle" / "logs" / "inner.zip").exists()


class TestCumulativeExtractionBudgetAndSizeCap:
    """Regression tests for #601 (cumulative budget & jenkins_artifacts_max_size_mb cap)."""

    def test_cumulative_nested_archive_bomb_stopped_at_budget(
        self, extract_base: Path
    ) -> None:
        """Multiple nested archives cannot cumulatively exceed max_size_mb * EXTRACT_SIZE_MULTIPLIER."""
        eight_mib = b"\x00" * (8 * 1024 * 1024)
        part_zip = _make_zip_bytes({"zeros.bin": eight_mib})
        outer_zip = _make_zip_bytes(
            {
                "part0.zip": part_zip,
                "part1.zip": part_zip,
                "part2.zip": part_zip,
            }
        )

        session = MagicMock()
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [outer_zip]
        response.close = Mock()
        session.get.return_value = response

        max_size_mb = 1
        budget_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024

        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            [{"relativePath": "outer.zip"}],
            max_size_mb=max_size_mb,
        )

        assert artifacts_dir is not None
        total_on_disk = sum(
            f.stat().st_size
            for f in artifacts_dir.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
        assert total_on_disk <= budget_bytes
        # At least the first 8 MiB part extracted cleanly, while the rest were stopped
        assert (artifacts_dir / "outer" / "part0" / "zeros.bin").exists()
        assert not (artifacts_dir / "outer" / "part1" / "zeros.bin").exists()
        assert not (artifacts_dir / "outer" / "part2" / "zeros.bin").exists()

    def test_multi_depth_nested_bomb_respects_cumulative_budget(
        self, extract_base: Path
    ) -> None:
        """Multi-level nested archives share a single cumulative extraction budget."""
        four_mib = b"\x00" * (4 * 1024 * 1024)
        level3 = _make_zip_bytes({"l3.bin": four_mib})
        level2 = _make_zip_bytes({"l2.bin": four_mib, "level3.zip": level3})
        level1 = _make_zip_bytes({"l1.bin": four_mib, "level2.zip": level2})

        artifacts_dir = extract_base / "artifacts-depth-bomb"
        artifacts_dir.mkdir()
        (artifacts_dir / "level1.zip").write_bytes(level1)

        max_size_mb = 1
        budget_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
        extracted = _extract_nested_archives(artifacts_dir, max_size_mb=max_size_mb)

        total_on_disk = sum(
            f.stat().st_size
            for f in artifacts_dir.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
        assert extracted == 2  # level1 (4 MiB) + level2 (4 MiB); level3 exceeds 10 MiB
        assert total_on_disk <= budget_bytes

    @pytest.mark.parametrize("remaining", [0, -1])
    def test_remaining_bytes_non_positive_fails_closed(
        self, extract_base: Path, remaining: int
    ) -> None:
        """remaining_bytes <= 0 must fail closed (never treated as unlimited)."""
        zip_data = _make_zip_bytes({"a.txt": "hello"})
        tar_data = _make_tar_gz({"a.txt": "hello"})

        assert (
            validate_and_extract_archive(
                zip_data, max_size_mb=10, remaining_bytes=remaining
            )
            is None
        )
        assert (
            validate_and_extract_archive(
                tar_data, max_size_mb=10, remaining_bytes=remaining
            )
            is None
        )

    def test_process_build_artifacts_stops_downloads_at_cumulative_budget(
        self, extract_base: Path
    ) -> None:
        """Multiple raw artifacts stop once cumulative workspace budget is reached."""
        one_mib = b"a" * (1024 * 1024)
        artifact_list = [{"relativePath": f"file{i}.bin"} for i in range(12)]

        session = MagicMock()

        def fake_get(url: str, **kwargs):
            response = Mock()
            response.status_code = 200
            response.iter_content.return_value = [one_mib]
            response.close = Mock()
            return response

        session.get.side_effect = fake_get

        max_size_mb = 1
        budget_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024

        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            artifact_list,
            max_size_mb=max_size_mb,
        )

        assert artifacts_dir is not None
        total_on_disk = sum(
            f.stat().st_size
            for f in artifacts_dir.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
        assert total_on_disk == budget_bytes
        assert session.get.call_count == 10

    def test_over_cap_jenkins_artifacts_max_size_mb_rejected(self) -> None:
        """Request models reject over-cap values; Settings clamps legacy values to ceiling."""
        from pydantic import ValidationError

        from rootcoz.config import (
            JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX,
            Settings,
            clear_db_settings_cache,
            get_settings,
            update_db_settings_cache,
        )
        from rootcoz.main import _validate_settings_updates
        from rootcoz.models import (
            AnalyzeRequest,
            ReAnalyzeRequest,
            UnifiedAnalyzeRequest,
        )

        over_cap = JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX + 1

        with pytest.raises(ValidationError):
            AnalyzeRequest(
                job_name="j", build_number=1, jenkins_artifacts_max_size_mb=over_cap
            )
        with pytest.raises(ValidationError):
            UnifiedAnalyzeRequest(
                type="jenkins",
                job_name="j",
                build_number=1,
                jenkins_artifacts_max_size_mb=over_cap,
            )
        with pytest.raises(ValidationError):
            ReAnalyzeRequest(jenkins_artifacts_max_size_mb=over_cap)
        with pytest.raises(ValidationError):
            Settings(jenkins_artifacts_max_size_mb=0)

        # Admin write-path validation still strictly rejects > 2048
        import asyncio

        errors = asyncio.run(
            _validate_settings_updates({"jenkins_artifacts_max_size_mb": over_cap})
        )
        assert any("jenkins_artifacts_max_size_mb" in e for e in errors)

        # Legacy env/DB values above the ceiling clamp instead of blocking startup
        # or wiping other DB overrides (#601).
        assert (
            Settings(
                jenkins_artifacts_max_size_mb=over_cap
            ).jenkins_artifacts_max_size_mb
            == JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX
        )
        clear_db_settings_cache()
        try:
            update_db_settings_cache(
                {
                    "jenkins_artifacts_max_size_mb": str(over_cap),
                    "ai_model": "preserved-db-model",
                }
            )
            effective = get_settings()
            assert (
                effective.jenkins_artifacts_max_size_mb
                == JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX
            )
            assert effective.ai_model == "preserved-db-model"
        finally:
            clear_db_settings_cache()

        # Boundary value itself is accepted
        req = AnalyzeRequest(
            job_name="j",
            build_number=1,
            jenkins_artifacts_max_size_mb=JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX,
        )
        assert req.jenkins_artifacts_max_size_mb == JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX

    def test_tar_mode_000_directory_still_counted_against_budget(
        self, extract_base: Path
    ) -> None:
        """Tar archives with mode-000 directories cannot hide bytes from the disk cap."""
        buf = io.BytesIO()
        payload = b"A" * (6 * 1024 * 1024)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            dir_info = tarfile.TarInfo(name="hidden")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o000
            tar.addfile(dir_info)

            file_info = tarfile.TarInfo(name="hidden/data.bin")
            file_info.size = len(payload)
            file_info.mode = 0o000
            tar.addfile(file_info, io.BytesIO(payload))
        tar_bytes = buf.getvalue()

        session = MagicMock()

        def fake_get(url: str, **kwargs):
            response = Mock()
            response.status_code = 200
            response.iter_content.return_value = [tar_bytes]
            response.close = Mock()
            return response

        session.get.side_effect = fake_get

        max_size_mb = 1
        budget_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
        _, artifacts_dir = process_build_artifacts(
            session,
            "https://jenkins.example.com/job/test/1",
            [{"relativePath": "first.tar.gz"}, {"relativePath": "second.tar.gz"}],
            max_size_mb=max_size_mb,
        )

        assert artifacts_dir is not None
        # First 6 MiB tar extracts with owner-accessible dirs; second 6 MiB tar
        # exceeds the remaining 4 MiB budget (and is not hidden behind mode 000).
        assert (artifacts_dir / "first" / "hidden" / "data.bin").exists()
        assert not (artifacts_dir / "second" / "hidden" / "data.bin").exists()
        total_on_disk = sum(
            f.stat().st_size
            for f in artifacts_dir.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
        assert total_on_disk <= budget_bytes

    def test_failed_archive_into_existing_dir_leaves_existing_untouched_and_respects_budget(
        self, extract_base: Path
    ) -> None:
        """A failing second archive sharing an extract subdir leaves zero partial files."""
        artifacts_dir = extract_base / "artifacts-collision"
        artifacts_dir.mkdir()

        # First archive creates artifacts_dir/shared/keep.txt
        first_tar = _make_tar_gz({"keep.txt": "original-content"})
        assert store_artifact("shared.tar.gz", first_tar, artifacts_dir, max_size_mb=1)
        assert (artifacts_dir / "shared" / "keep.txt").read_text() == "original-content"

        # Second archive targets the same subdir ("shared") but fails mid-extraction
        # with an oversized payload exceeding remaining_bytes.
        bad_zip_buf = io.BytesIO()
        with zipfile.ZipFile(bad_zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("keep.txt", "corrupted-overwrite")
            zf.writestr("huge.bin", b"\x00" * (2 * 1024 * 1024))
        bad_zip = bad_zip_buf.getvalue()

        # Remaining budget is 500 bytes: extraction fails (2 MiB > 500 B), staging
        # is discarded, original keep.txt is untouched, and raw fallback is also
        # rejected because len(bad_zip) > 500 B.
        stored = store_artifact(
            "shared.zip", bad_zip, artifacts_dir, max_size_mb=1, remaining_bytes=500
        )
        assert stored is False
        assert (artifacts_dir / "shared" / "keep.txt").read_text() == "original-content"
        assert not (artifacts_dir / "shared" / "huge.bin").exists()
        assert not (artifacts_dir / "shared.zip").exists()
        assert not list(artifacts_dir.glob(".extract-stage-*"))

    def test_stored_replay_and_jenkins_source_reconstruct_clamp_legacy_values(
        self,
    ) -> None:
        """Legacy stored jobs with >2048 MB degrade safely on replay and reconstruction."""
        from rootcoz.config import JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX, Settings
        from rootcoz.main import _reconstruct_from_params
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.jenkins_source import JenkinsSource

        over_cap = JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX + 5000

        replayed = UnifiedAnalyzeRequest.model_validate(
            {
                "type": "jenkins",
                "job_name": "legacy-job",
                "build_number": 7,
                "jenkins_artifacts_max_size_mb": over_cap,
            },
            context={"stored_replay": True},
        )
        assert (
            replayed.jenkins_artifacts_max_size_mb == JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX
        )

        body, effective = _reconstruct_from_params(
            {
                "job_name": "legacy-job",
                "build_number": 7,
                "request_params": {
                    "analysis_type": "jenkins",
                    "jenkins_artifacts_max_size_mb": over_cap,
                },
            }
        )
        assert body.jenkins_artifacts_max_size_mb == JENKINS_ARTIFACTS_MAX_SIZE_MB_MAX
        assert (
            effective.jenkins_artifacts_max_size_mb
            <= Settings().jenkins_artifacts_max_size_mb
        )

        # JenkinsSource reconstruction clamps even when settings is None and ignores non-numeric stored data
        source_clamped = JenkinsSource.from_stored_params(
            {
                "job_name": "legacy-job",
                "build_number": 7,
                "jenkins_url": "https://jenkins.example.com",
                "jenkins_artifacts_max_size_mb": over_cap,
            },
            settings=None,
        )
        assert source_clamped is not None
        assert (
            source_clamped.settings.jenkins_artifacts_max_size_mb
            <= Settings().jenkins_artifacts_max_size_mb
        )

        source_bad_val = JenkinsSource.from_stored_params(
            {
                "job_name": "legacy-job",
                "build_number": 7,
                "jenkins_url": "https://jenkins.example.com",
                "jenkins_artifacts_max_size_mb": "not-an-int",
            },
            settings=None,
        )
        assert source_bad_val is not None
        assert (
            source_bad_val.settings.jenkins_artifacts_max_size_mb
            == Settings().jenkins_artifacts_max_size_mb
        )

    def test_merge_settings_caps_jenkins_artifacts_max_size_mb_to_server_limit(
        self,
    ) -> None:
        """Per-request jenkins_artifacts_max_size_mb cannot exceed the server setting."""
        from rootcoz.config import Settings
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        server_settings = Settings(jenkins_artifacts_max_size_mb=200)

        over_req = AnalyzeRequest(
            job_name="j", build_number=1, jenkins_artifacts_max_size_mb=1000
        )
        merged_over = _merge_settings(over_req, server_settings)
        assert merged_over.jenkins_artifacts_max_size_mb == 200

        under_req = AnalyzeRequest(
            job_name="j", build_number=1, jenkins_artifacts_max_size_mb=50
        )
        merged_under = _merge_settings(under_req, server_settings)
        assert merged_under.jenkins_artifacts_max_size_mb == 50
