"""Tests for metadata_rules module, auto-assignment, API, and CLI."""

import json as json_mod
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from rootcoz import storage
from rootcoz.cli.main import app as cli_app
from rootcoz.metadata_rules import (
    load_metadata_rules,
    match_job_metadata,
    merge_labels,
)
from tests.conftest import make_test_client

# --- Unit tests for load_metadata_rules ---


class TestLoadMetadataRules:
    """Tests for loading rules from YAML and JSON files."""

    def test_load_from_json_file(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json_mod.dumps(
                {
                    "metadata_rules": [
                        {"pattern": "test-*", "team": "qa", "labels": ["smoke"]},
                        {"pattern": "dev-*", "labels": ["dev"]},
                    ]
                }
            )
        )
        rules = load_metadata_rules(str(rules_file))
        assert len(rules) == 2
        assert rules[0]["pattern"] == "test-*"
        assert rules[0]["team"] == "qa"
        assert rules[0]["labels"] == ["smoke"]
        assert rules[1]["pattern"] == "dev-*"

    def test_load_from_yaml_file(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "metadata_rules:\n"
            "  - pattern: 'test-*'\n"
            "    team: qa\n"
            "    labels: [smoke]\n"
            "  - pattern: 'dev-*'\n"
            "    labels: [dev]\n"
        )
        rules = load_metadata_rules(str(rules_file))
        assert len(rules) == 2
        assert rules[0]["team"] == "qa"

    def test_load_bare_list_json(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps([{"pattern": "test-*", "team": "qa"}]))
        rules = load_metadata_rules(str(rules_file))
        assert len(rules) == 1

    def test_load_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_metadata_rules("/nonexistent/rules.json")

    def test_load_missing_pattern_raises(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps([{"team": "qa"}]))
        with pytest.raises(ValueError, match="missing 'pattern'"):
            load_metadata_rules(str(rules_file))

    def test_load_invalid_structure(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text('"just a string"')
        with pytest.raises(TypeError):
            load_metadata_rules(str(rules_file))

    def test_load_rule_not_dict(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps(["not a dict"]))
        with pytest.raises(TypeError, match="must be a dict"):
            load_metadata_rules(str(rules_file))

    def test_load_dict_missing_metadata_rules_key(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps({"other_key": []}))
        with pytest.raises(ValueError, match="metadata_rules"):
            load_metadata_rules(str(rules_file))

    def test_load_invalid_regex_pattern_raises(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json_mod.dumps([{"pattern": "(?P<version>(\\d+)", "team": "qa"}])
        )
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            load_metadata_rules(str(rules_file))

    def test_load_labels_invalid_type_raises(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps([{"pattern": "test-*", "labels": 123}]))
        with pytest.raises(ValueError, match=r"labels.*must be a list or string"):
            load_metadata_rules(str(rules_file))

    def test_load_labels_string_coerced_to_list(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json_mod.dumps([{"pattern": "test-*", "labels": "single"}])
        )
        rules = load_metadata_rules(str(rules_file))
        assert rules[0]["labels"] == ["single"]


# --- Unit tests for match_job_metadata ---


class TestMatchJobMetadata:
    """Tests for pattern matching logic."""

    SAMPLE_RULES = (
        {
            "pattern": "test-kubevirt-console-t1-*",
            "team": "console",
            "tier": "t1",
            "labels": ["kubevirt-console"],
        },
        {
            "pattern": "test-kubevirt-console-t2-*",
            "team": "console",
            "tier": "t2",
            "labels": ["kubevirt-console"],
        },
        {
            "pattern": "test-pytest-cnv-*-storage-*",
            "team": "storage",
            "tier": "t2",
            "labels": ["pytest"],
        },
        {"pattern": "dev-*-rnetser", "labels": ["dev", "personal"]},
        {"pattern": "*-gating", "labels": ["gating"]},
        {"pattern": "verify-*-wrapper", "team": "ci", "labels": ["wrapper", "verify"]},
    )

    def test_glob_exact_match(self) -> None:
        result = match_job_metadata("test-kubevirt-console-t1-smoke", self.SAMPLE_RULES)
        assert result is not None
        assert result["team"] == "console"
        assert result["tier"] == "t1"
        assert "kubevirt-console" in result["labels"]

    def test_glob_wildcard_match(self) -> None:
        result = match_job_metadata(
            "test-pytest-cnv-4.18-storage-nfs", self.SAMPLE_RULES
        )
        assert result is not None
        assert result["team"] == "storage"
        assert result["tier"] == "t2"

    def test_no_match_returns_none(self) -> None:
        result = match_job_metadata("unrelated-job-name", self.SAMPLE_RULES)
        assert result is None

    def test_first_match_wins_for_scalars(self) -> None:
        rules = [
            {"pattern": "test-*", "team": "first-team", "tier": "t1"},
            {"pattern": "test-*", "team": "second-team", "tier": "t2"},
        ]
        result = match_job_metadata("test-something", rules)
        assert result is not None
        assert result["team"] == "first-team"
        assert result["tier"] == "t1"

    def test_labels_accumulate_from_all_matches(self) -> None:
        rules = [
            {"pattern": "test-*-gating", "team": "qa", "labels": ["test"]},
            {"pattern": "*-gating", "labels": ["gating"]},
        ]
        result = match_job_metadata("test-smoke-gating", rules)
        assert result is not None
        assert result["team"] == "qa"
        assert "test" in result["labels"]
        assert "gating" in result["labels"]

    def test_labels_no_duplicates(self) -> None:
        rules = [
            {"pattern": "test-*", "labels": ["smoke"]},
            {"pattern": "test-*", "labels": ["smoke", "extra"]},
        ]
        result = match_job_metadata("test-foo", rules)
        assert result is not None
        assert result["labels"].count("smoke") == 1
        assert "extra" in result["labels"]

    def test_empty_rules_returns_none(self) -> None:
        result = match_job_metadata("any-job", [])
        assert result is None

    def test_regex_version_extraction(self) -> None:
        rules = [
            {
                "pattern": r"test-pytest-cnv-(?P<version>[\d.]+z?)-.*",
                "team": "cnv",
                "labels": ["pytest"],
            },
        ]
        result = match_job_metadata("test-pytest-cnv-4.18z-storage-nfs", rules)
        assert result is not None
        assert result["version"] == "4.18z"
        assert result["team"] == "cnv"

    def test_regex_no_match(self) -> None:
        rules = [
            {"pattern": r"test-(?P<version>\d+)-.*", "team": "versioned"},
        ]
        result = match_job_metadata("unrelated-job", rules)
        assert result is None

    def test_regex_explicit_version_overrides_capture(self) -> None:
        """Explicit version in rule wins over regex capture group."""
        rules = [
            {"pattern": r"test-(?P<version>\d+)-.*", "version": "explicit-v1"},
        ]
        result = match_job_metadata("test-42-something", rules)
        assert result is not None
        # Explicit rule value overwrites captured group
        assert result["version"] == "explicit-v1"

    def test_match_with_only_labels(self) -> None:
        rules = [{"pattern": "dev-*", "labels": ["dev"]}]
        result = match_job_metadata("dev-my-branch", rules)
        assert result is not None
        assert result["team"] is None
        assert result["tier"] is None
        assert result["labels"] == ["dev"]


class TestMergeLabels:
    """Unit tests for merge_labels."""

    def test_order_preservation_first_occurrence_wins(self) -> None:
        assert merge_labels(["a", "b"], ["b", "c"], ["a", "d"]) == ["a", "b", "c", "d"]

    def test_case_sensitive_dedup(self) -> None:
        assert merge_labels(["Nightly"], ["nightly"]) == ["Nightly", "nightly"]

    def test_empty_and_none_inputs(self) -> None:
        assert merge_labels() == []
        assert merge_labels(None) == []
        assert merge_labels([]) == []
        assert merge_labels(None, [], ["a"], None, []) == ["a"]

    def test_blank_labels_skipped(self) -> None:
        """Empty / falsy labels are skipped (``if label`` guard)."""
        assert merge_labels(["a", "", "b"], [""]) == ["a", "b"]


# --- Storage integration tests ---


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


class TestAutoAssignJobMetadata:
    """Tests for auto_assign_job_metadata in storage."""

    RULES = (
        {
            "pattern": "test-*-storage-*",
            "team": "storage",
            "tier": "t2",
            "labels": ["pytest"],
        },
        {"pattern": "dev-*", "labels": ["dev"]},
    )

    async def test_auto_assign_when_no_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs", self.RULES
            )
            assert result is not None
            assert result["team"] == "storage"
            assert result["tier"] == "t2"
            assert result["labels"] == ["pytest"]

            # Verify stored
            stored = await storage.get_job_metadata("test-cnv-storage-nfs")
            assert stored is not None
            assert stored["team"] == "storage"

    async def test_no_overwrite_existing_metadata(self, setup_test_db: Path) -> None:
        """Manual non-NULL values take precedence; NULL fields may be filled from rules."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("test-cnv-storage-nfs", team="manual-team")
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs", self.RULES
            )
            assert result is not None
            assert result["team"] == "manual-team"
            assert result["tier"] == "t2"
            assert result["labels"] == ["pytest"]

            stored = await storage.get_job_metadata("test-cnv-storage-nfs")
            assert stored["team"] == "manual-team"

    async def test_no_match_returns_none(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata("unrelated-job", self.RULES)
            assert result is None

    async def test_empty_rules_returns_none(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata("test-cnv-storage-nfs", [])
            assert result is None

    async def test_empty_job_name_returns_none(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata("", self.RULES)
            assert result is None

    async def test_merge_extras_with_rules_on_new_job(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                self.RULES,
                extra_labels=["Nightly", "CNV"],
            )
            assert result is not None
            assert result["team"] == "storage"
            assert result["labels"] == ["pytest", "Nightly", "CNV"]

    async def test_merge_extras_into_existing_preserves_team(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata(
                "test-cnv-storage-nfs",
                team="manual-team",
                tier="t1",
                labels=["existing"],
            )
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                self.RULES,
                extra_labels=["Nightly"],
            )
            assert result is not None
            assert result["team"] == "manual-team"
            assert result["tier"] == "t1"
            assert result["labels"] == ["existing", "pytest", "Nightly"]

    async def test_merge_extras_dedup_preserves_case(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("any-job", labels=["Nightly", "smoke"])
            result = await storage.auto_assign_job_metadata(
                "any-job",
                [],
                extra_labels=["Nightly", "CNV"],
            )
            assert result is not None
            assert result["labels"] == ["Nightly", "smoke", "CNV"]

    async def test_extras_only_no_rules_creates_labels_row(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata(
                "unrelated-job",
                [],
                extra_labels=["Nightly"],
            )
            assert result is not None
            assert result["team"] is None
            assert result["labels"] == ["Nightly"]

    async def test_empty_extras_skips_existing(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", team="t", labels=["a"])
            result = await storage.auto_assign_job_metadata(
                "job-a", self.RULES, extra_labels=[]
            )
            assert result is None
            stored = await storage.get_job_metadata("job-a")
            assert stored is not None
            assert stored["labels"] == ["a"]

    async def test_rule_fills_labels_only_row(self, setup_test_db: Path) -> None:
        """Rules fill NULL team/tier on a labels-only row from a previous extras-only call."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # First call: extras only, no rule match → labels-only row
            await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                [],
                extra_labels=["Nightly"],
            )
            meta = await storage.get_job_metadata("test-cnv-storage-nfs")
            assert meta is not None
            assert meta["team"] is None
            assert meta["labels"] == ["Nightly"]

            # Second call: rules match, no extras → fills team/tier/version
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                self.RULES,
            )
            assert result is not None
            assert result["team"] == "storage"
            assert result["labels"] == ["Nightly", "pytest"]  # merged

    async def test_rules_and_extras_fill_labels_only_row(
        self, setup_test_db: Path
    ) -> None:
        """Rules + extras on a labels-only row fill NULL scalars and merge all labels."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata(
                "test-cnv-storage-nfs",
                labels=["existing"],
            )
            meta = await storage.get_job_metadata("test-cnv-storage-nfs")
            assert meta is not None
            assert meta["team"] is None
            assert meta["tier"] is None
            assert meta["version"] is None
            assert meta["labels"] == ["existing"]

            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                self.RULES,
                extra_labels=["Nightly", "CNV"],
            )
            assert result is not None
            assert result["team"] == "storage"
            assert result["tier"] == "t2"
            assert result["version"] is None
            assert result["labels"] == ["existing", "pytest", "Nightly", "CNV"]

    async def test_noop_merge_returns_none(self, setup_test_db: Path) -> None:
        """When extras don't change labels, return None (not the existing dict)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-x", labels=["a", "b"])
            result = await storage.auto_assign_job_metadata(
                "job-x", [], extra_labels=["a", "b"]
            )
            assert result is None

    async def test_explicit_only_no_rules_creates_row_and_records_request_source(
        self, setup_test_db: Path
    ) -> None:
        """Request-only explicit metadata populates all columns when no rules exist."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata(
                "unmatched-pipeline-job",
                [],
                explicit={"team": "storage-platform", "tier": "2", "version": "5.1"},
            )
            assert result is not None
            assert result["team"] == "storage-platform"
            assert result["tier"] == "2"
            assert result["version"] == "5.1"
            assert result["sources"] == {
                "team": "request",
                "tier": "request",
                "version": "request",
            }
            stored = await storage.get_job_metadata("unmatched-pipeline-job")
            assert stored is not None
            assert stored["team"] == "storage-platform"
            assert stored["tier"] == "2"
            assert stored["version"] == "5.1"

    async def test_explicit_wins_per_field_and_rules_fill_omitted(
        self, setup_test_db: Path
    ) -> None:
        """Precedence is per-field: request -> rules -> empty."""
        rules = [
            {
                "pattern": "test-*-storage-*",
                "team": "rule-storage",
                "tier": "t2",
                "version": "5.0",
                "labels": ["pytest"],
            }
        ]
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                rules,
                extra_labels=["gating"],
                explicit={"team": "storage-platform", "tier": "", "version": None},
            )
            assert result is not None
            assert result["team"] == "storage-platform"
            assert result["tier"] == "t2"
            assert result["version"] == "5.0"
            assert result["labels"] == ["pytest", "gating"]
            assert result["sources"] == {
                "team": "request",
                "tier": "rules",
                "version": "rules",
                "labels": "request+rules",
            }

    async def test_existing_non_null_preserved_over_explicit_and_rules(
        self, setup_test_db: Path
    ) -> None:
        """Manual PUT .../metadata stays sticky; only NULL columns are filled."""
        rules = [
            {
                "pattern": "test-*-storage-*",
                "team": "rule-team",
                "tier": "rule-tier",
                "version": "5.0",
            }
        ]
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("test-cnv-storage-nfs", team="manual-team")
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                rules,
                explicit={"team": "req-team", "tier": "2"},
            )
            assert result is not None
            assert result["team"] == "manual-team"
            assert result["tier"] == "2"
            assert result["version"] == "5.0"
            assert result["sources"] == {"tier": "request", "version": "rules"}

            # Second call with different explicit values changes nothing -> None
            second = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs",
                rules,
                explicit={"team": "other", "tier": "9", "version": "9.9"},
            )
            assert second is None
            stored = await storage.get_job_metadata("test-cnv-storage-nfs")
            assert stored is not None
            assert (stored["team"], stored["tier"], stored["version"]) == (
                "manual-team",
                "2",
                "5.0",
            )


# --- API endpoint tests ---


def _make_mock_settings(**overrides):
    """Create a mock Settings with sensible defaults for metadata rules tests."""
    mock = MagicMock()
    mock.metadata_rules = overrides.get("metadata_rules", [])
    mock.metadata_rules_file = overrides.get("metadata_rules_file", "")
    mock.admin_key = "test-admin-key-16chars"  # pragma: allowlist secret
    mock.trust_proxy_headers = False
    mock.secure_cookies = False
    return mock


_RULES_AUTH_HEADERS = {
    "Authorization": "Bearer test-admin-key-16chars"
}  # pragma: allowlist secret


class TestMetadataRulesAPI:
    """Tests for metadata rules API endpoints."""

    async def test_list_rules_endpoint(self) -> None:
        rules = [{"pattern": "test-*", "team": "qa"}]
        mock_settings = _make_mock_settings(
            metadata_rules=rules, metadata_rules_file="/data/rules.yaml"
        )

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.get("/api/jobs/metadata/rules")
                assert resp.status_code == 200
                data = resp.json()
                assert data["rules_file"] == "rules.yaml"
                assert len(data["rules"]) == 1

    async def test_preview_rules_endpoint(self) -> None:
        rules = [{"pattern": "test-*", "team": "qa", "labels": ["test"]}]
        mock_settings = _make_mock_settings(
            metadata_rules=rules, metadata_rules_file="/data/rules.yaml"
        )

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": "test-something"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["matched"] is True
                assert data["metadata"]["team"] == "qa"

    async def test_preview_no_match(self) -> None:
        mock_settings = _make_mock_settings(
            metadata_rules=[{"pattern": "test-*", "team": "qa"}]
        )

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": "unrelated-job"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["matched"] is False
                assert data["metadata"] is None

    async def test_preview_missing_job_name(self) -> None:
        mock_settings = _make_mock_settings()

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={},
                )
                assert resp.status_code == 422

    async def test_preview_non_string_job_name(self) -> None:
        mock_settings = _make_mock_settings()

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": 12345},
                )
                assert resp.status_code == 422

    async def test_preview_whitespace_only_job_name(self) -> None:
        mock_settings = _make_mock_settings()

        with patch("rootcoz.main.get_settings", return_value=mock_settings):
            from rootcoz.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
                headers=_RULES_AUTH_HEADERS,
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": "   "},
                )
                assert resp.status_code == 422


# --- CLI client tests ---


class TestMetadataRulesClient:
    """Tests for RootCozClient metadata rules methods."""

    def test_list_metadata_rules(self) -> None:
        response_data = {
            "rules_file": "/data/rules.yaml",
            "rules": [{"pattern": "test-*", "team": "qa"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/jobs/metadata/rules"
            return httpx.Response(200, json=response_data)

        client = make_test_client(handler)
        result = client.list_metadata_rules()
        assert result == response_data

    def test_preview_metadata_rules(self) -> None:
        response_data = {
            "job_name": "test-foo",
            "matched": True,
            "metadata": {"team": "qa"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/jobs/metadata/rules/preview"
            body = json_mod.loads(request.content)
            assert body["job_name"] == "test-foo"
            return httpx.Response(200, json=response_data)

        client = make_test_client(handler)
        result = client.preview_metadata_rules("test-foo")
        assert result["matched"] is True


# --- CLI command tests ---


class TestMetadataRulesCLI:
    """Tests for metadata rules CLI commands."""

    def _invoke(self, args: list[str], handler) -> object:
        runner = CliRunner()
        with (
            patch.dict(os.environ, {"ROOTCOZ_SERVER": "http://test-server"}),
            patch(
                "rootcoz.cli.main.get_server_config",
                return_value=None,
            ),
            patch(
                "rootcoz.cli.main._get_client",
                return_value=make_test_client(handler),
            ),
        ):
            result = runner.invoke(cli_app, args)
            return result

    def test_metadata_rules_command(self) -> None:
        response_data = {
            "rules_file": "/data/rules.yaml",
            "rules": [
                {"pattern": "test-*", "team": "qa", "labels": ["smoke"]},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "rules"], handler)
        assert result.exit_code == 0
        assert "test-*" in result.output
        assert "qa" in result.output
        assert "/data/rules.yaml" in result.output

    def test_metadata_rules_no_rules(self) -> None:
        response_data = {"rules_file": None, "rules": []}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "rules"], handler)
        assert result.exit_code == 0
        assert "No metadata rules configured" in result.output

    def test_metadata_rules_json(self) -> None:
        response_data = {
            "rules_file": "/data/rules.yaml",
            "rules": [{"pattern": "test-*"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "rules", "--json"], handler)
        assert result.exit_code == 0
        parsed = json_mod.loads(result.output)
        assert parsed["rules_file"] == "/data/rules.yaml"

    def test_metadata_preview_command(self) -> None:
        response_data = {
            "job_name": "test-smoke",
            "matched": True,
            "metadata": {
                "team": "qa",
                "tier": "t1",
                "version": None,
                "labels": ["smoke"],
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "preview", "test-smoke"], handler)
        assert result.exit_code == 0
        assert "qa" in result.output
        assert "Match for" in result.output

    def test_metadata_preview_no_match(self) -> None:
        response_data = {"job_name": "unrelated", "matched": False, "metadata": None}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "preview", "unrelated"], handler)
        assert result.exit_code == 0
        assert "No rules matched" in result.output

    def test_metadata_preview_json(self) -> None:
        response_data = {
            "job_name": "test-foo",
            "matched": True,
            "metadata": {"team": "qa"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        result = self._invoke(["metadata", "preview", "--json", "test-foo"], handler)
        assert result.exit_code == 0
        parsed = json_mod.loads(result.output)
        assert parsed["matched"] is True


# --- Config integration test ---


class TestSettingsMetadataRules:
    """Tests for Settings.metadata_rules property."""

    def test_metadata_rules_empty_when_no_file(self) -> None:
        with patch.dict(
            os.environ,
            {
                "JENKINS_URL": "https://jenkins.example.com",
                "JENKINS_USER": "u",
                "JENKINS_PASSWORD": "p",
                "METADATA_RULES_FILE": "",
            },
            clear=False,
        ):
            from rootcoz.config import Settings

            s = Settings()
            assert s.metadata_rules == []

    def test_metadata_rules_loads_from_file(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json_mod.dumps({"metadata_rules": [{"pattern": "test-*", "team": "qa"}]})
        )
        with patch.dict(
            os.environ,
            {
                "JENKINS_URL": "https://jenkins.example.com",
                "JENKINS_USER": "u",
                "JENKINS_PASSWORD": "p",
                "METADATA_RULES_FILE": str(rules_file),
            },
            clear=False,
        ):
            from rootcoz.config import Settings

            s = Settings()
            assert len(s.metadata_rules) == 1
            assert s.metadata_rules[0]["team"] == "qa"

    def test_metadata_rules_bad_file_returns_empty(self) -> None:
        with patch.dict(
            os.environ,
            {
                "JENKINS_URL": "https://jenkins.example.com",
                "JENKINS_USER": "u",
                "JENKINS_PASSWORD": "p",
                "METADATA_RULES_FILE": "/nonexistent/rules.json",
            },
            clear=False,
        ):
            from rootcoz.config import Settings

            s = Settings()
            assert s.metadata_rules == []


class TestAnalyzeAndSubmitMetadataAPI:
    """End-to-end API + audit tests for request team/tier/version (#657)."""

    @staticmethod
    def _wait_for_completion(client, job_id: str) -> dict:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            resp = client.get(f"/results/{job_id}")
            assert resp.status_code in (200, 202), resp.text
            data = resp.json()
            if data.get("status") in ("completed", "failed"):
                return data
            time.sleep(0.02)
        raise AssertionError(f"Job {job_id} did not complete in time")

    @staticmethod
    def _init_sync_db(db_path: Path) -> None:
        import asyncio

        with patch.object(storage, "DB_PATH", db_path):
            asyncio.run(storage.init_db())

    @classmethod
    def _api_client(cls, db_path: Path, *, metadata_rules_file: str = ""):
        from contextlib import contextmanager

        from fastapi.testclient import TestClient

        from rootcoz import main as rootcoz_main
        from rootcoz.config import Settings

        @contextmanager
        def _ctx():
            cls._init_sync_db(db_path)
            settings = Settings(
                admin_key="test-admin-key-16chars",  # pragma: allowlist secret
                metadata_rules_file=metadata_rules_file,
                secure_cookies=False,
            )
            rootcoz_main.app.dependency_overrides[rootcoz_main.get_settings] = lambda: (
                settings
            )
            try:
                with (
                    patch.object(storage, "DB_PATH", db_path),
                    patch.object(rootcoz_main, "get_settings", return_value=settings),
                    TestClient(rootcoz_main.app, headers=_RULES_AUTH_HEADERS) as client,
                ):
                    yield client
            finally:
                rootcoz_main.app.dependency_overrides.pop(
                    rootcoz_main.get_settings, None
                )

        return _ctx()

    def test_submit_request_only_populates_dashboard_filters_and_audits(
        self, temp_db_path: Path
    ) -> None:
        import asyncio

        with self._api_client(temp_db_path) as client:
            resp = client.post(
                "/submit",
                json={
                    "type": "file",
                    "job_name": "ci-unmatched-storage-job",
                    "raw_xml": (
                        '<testsuite name="ci-unmatched-storage-job">'
                        '<testcase name="test_one"><failure>boom</failure></testcase>'
                        "</testsuite>"
                    ),
                    "team": "storage-platform",
                    "tier": "2",
                    "version": "5.1",
                    "labels": ["gating"],
                },
            )
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            done = self._wait_for_completion(client, job_id)
            assert done["status"] == "completed"

            filtered = client.get(
                "/api/dashboard/filtered",
                params={
                    "team": "storage-platform",
                    "tier": "2",
                    "version": "5.1",
                },
            )
            assert filtered.status_code == 200, filtered.text
            jobs = filtered.json()["jobs"]
            assert any(j["job_id"] == job_id for j in jobs)

            audit = asyncio.run(storage.list_audit_events(action="job.metadata_set"))[
                "events"
            ]
            assert len(audit) == 1
            ev = audit[0]
            assert ev["outcome"] == "success"
            assert ev["target_type"] == "job"
            assert ev["target_id"] == "ci-unmatched-storage-job"
            assert ev["details"]["trigger"] == "ingest"
            assert ev["details"]["job_id"] == job_id
            assert ev["details"]["sources"] == {
                "team": "request",
                "tier": "request",
                "version": "request",
                "labels": "request",
            }

            # Second submit with different team preserves existing non-NULL team
            resp2 = client.post(
                "/submit",
                json={
                    "type": "file",
                    "job_name": "ci-unmatched-storage-job",
                    "raw_xml": (
                        '<testsuite name="ci-unmatched-storage-job">'
                        '<testcase name="test_two"><failure>boom</failure></testcase>'
                        "</testsuite>"
                    ),
                    "team": "other-team",
                    "tier": "9",
                    "version": "9.9",
                },
            )
            assert resp2.status_code == 202, resp2.text
            done2 = self._wait_for_completion(client, resp2.json()["job_id"])
            assert done2["status"] == "completed"

            stored = asyncio.run(storage.get_job_metadata("ci-unmatched-storage-job"))
            assert stored is not None
            assert (stored["team"], stored["tier"], stored["version"]) == (
                "storage-platform",
                "2",
                "5.1",
            )
            # No extra audit row when nothing changed
            audit_after = asyncio.run(
                storage.list_audit_events(action="job.metadata_set")
            )["events"]
            assert len(audit_after) == 1

    def test_submit_rules_only_regression_and_mixed_precedence_audit(
        self, temp_db_path: Path, tmp_path: Path
    ) -> None:
        import asyncio

        rules_file = tmp_path / "metadata-rules.yaml"
        rules_file.write_text(
            "metadata_rules:\n"
            "  - pattern: 'test-cnv-*'\n"
            "    team: rule-team\n"
            "    tier: t1\n"
            "    version: '5.0'\n"
            "    labels: [pytest]\n",
            encoding="utf-8",
        )
        with self._api_client(
            temp_db_path, metadata_rules_file=str(rules_file)
        ) as client:
            # 1) Rules-only submission (regression check)
            r1 = client.post(
                "/submit",
                json={
                    "type": "raw",
                    "name": "test-cnv-rules-only",
                    "failures": [{"test_name": "t1", "error_message": "e"}],
                },
            )
            assert r1.status_code == 202, r1.text
            self._wait_for_completion(client, r1.json()["job_id"])

            m1 = asyncio.run(storage.get_job_metadata("test-cnv-rules-only"))
            assert m1 is not None
            assert (m1["team"], m1["tier"], m1["version"]) == (
                "rule-team",
                "t1",
                "5.0",
            )

            # 2) Mixed: request overrides team, empty string tier falls back to rules
            r2 = client.post(
                "/submit",
                json={
                    "type": "raw",
                    "name": "test-cnv-mixed",
                    "team": "override-team",
                    "tier": "   ",
                    "failures": [{"test_name": "t2", "error_message": "e"}],
                },
            )
            assert r2.status_code == 202, r2.text
            self._wait_for_completion(client, r2.json()["job_id"])

            m2 = asyncio.run(storage.get_job_metadata("test-cnv-mixed"))
            assert m2 is not None
            assert (m2["team"], m2["tier"], m2["version"]) == (
                "override-team",
                "t1",
                "5.0",
            )

            events = asyncio.run(storage.list_audit_events(action="job.metadata_set"))[
                "events"
            ]
            by_job = {e["target_id"]: e for e in events}
            assert by_job["test-cnv-rules-only"]["details"]["sources"] == {
                "team": "rules",
                "tier": "rules",
                "version": "rules",
                "labels": "rules",
            }
            assert by_job["test-cnv-mixed"]["details"]["sources"] == {
                "team": "request",
                "tier": "rules",
                "version": "rules",
                "labels": "rules",
            }

    @pytest.mark.parametrize("endpoint", ["/submit", "/analyze"])
    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"team": "a" * 65},
            {"tier": "b" * 33},
            {"version": "c" * 65},
            {"team": "bad\x00team"},
            {"tier": "2\ninjected"},
        ],
    )
    def test_invalid_metadata_returns_422_at_boundary(
        self, temp_db_path: Path, endpoint: str, bad_payload: dict[str, str]
    ) -> None:
        with self._api_client(temp_db_path) as client:
            resp = client.post(
                endpoint,
                json={
                    "type": "raw",
                    "failures": [{"test_name": "t1", "error_message": "e"}],
                    **bad_payload,
                },
            )
            assert resp.status_code == 422, resp.text

    async def test_auto_assign_metadata_failure_is_audited_and_best_effort(
        self, setup_test_db: Path
    ) -> None:
        from unittest.mock import AsyncMock

        from rootcoz import main as rootcoz_main

        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(
                storage,
                "auto_assign_job_metadata",
                AsyncMock(side_effect=RuntimeError("db locked")),
            ),
        ):
            await rootcoz_main._auto_assign_metadata(
                "failing-job",
                [],
                explicit={"team": "storage"},
                actor="alice",
                job_id="job-123",
            )
            events = (await storage.list_audit_events(action="job.metadata_set"))[
                "events"
            ]
            assert len(events) == 1
            assert events[0]["outcome"] == "failure"
            assert events[0]["actor"] == "alice"
            assert events[0]["target_id"] == "failing-job"
            assert events[0]["details"] == {
                "trigger": "ingest",
                "reason": "internal_error",
                "job_id": "job-123",
            }

    def test_reconstruct_from_params_round_trips_metadata_scalars(self) -> None:
        from rootcoz import main as rootcoz_main

        result_data = {
            "job_name": "my-job",
            "build_number": 7,
            "request_params": {
                "analysis_type": "jenkins",
                "job_name": "my-job",
                "build_number": 7,
                "team": "storage-platform",
                "tier": "2",
                "version": "5.1",
                "labels": ["gating"],
            },
        }
        body, _ = rootcoz_main._reconstruct_from_params(result_data)
        assert body.team == "storage-platform"
        assert body.tier == "2"
        assert body.version == "5.1"
        assert body.labels == ["gating"]
