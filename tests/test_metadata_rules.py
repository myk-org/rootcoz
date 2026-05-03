"""Tests for metadata_rules module, auto-assignment, API, and CLI."""

import json as json_mod
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from jenkins_job_insight import storage
from jenkins_job_insight.cli.main import app as cli_app
from jenkins_job_insight.metadata_rules import (
    load_metadata_rules,
    match_job_metadata,
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
        with pytest.raises(ValueError):
            load_metadata_rules(str(rules_file))

    def test_load_rule_not_dict(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json_mod.dumps(["not a dict"]))
        with pytest.raises(ValueError, match="must be a dict"):
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
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("test-cnv-storage-nfs", team="manual-team")
            result = await storage.auto_assign_job_metadata(
                "test-cnv-storage-nfs", self.RULES
            )
            assert result is None

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


# --- API endpoint tests ---


def _make_mock_settings(**overrides):
    """Create a mock Settings with sensible defaults for metadata rules tests."""
    mock = MagicMock()
    mock.metadata_rules = overrides.get("metadata_rules", [])
    mock.metadata_rules_file = overrides.get("metadata_rules_file", "")
    return mock


class TestMetadataRulesAPI:
    """Tests for metadata rules API endpoints."""

    async def test_list_rules_endpoint(self) -> None:
        rules = [{"pattern": "test-*", "team": "qa"}]
        mock_settings = _make_mock_settings(
            metadata_rules=rules, metadata_rules_file="/data/rules.yaml"
        )

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
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

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
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

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
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

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={},
                )
                assert resp.status_code == 422

    async def test_preview_non_string_job_name(self) -> None:
        mock_settings = _make_mock_settings()

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": 12345},
                )
                assert resp.status_code == 422

    async def test_preview_whitespace_only_job_name(self) -> None:
        mock_settings = _make_mock_settings()

        with patch("jenkins_job_insight.main.get_settings", return_value=mock_settings):
            from jenkins_job_insight.main import app as fastapi_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi_app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/jobs/metadata/rules/preview",
                    json={"job_name": "   "},
                )
                assert resp.status_code == 422


# --- CLI client tests ---


class TestMetadataRulesClient:
    """Tests for JJIClient metadata rules methods."""

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
            patch.dict(os.environ, {"JJI_SERVER": "http://test-server"}),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=None,
            ),
            patch(
                "jenkins_job_insight.cli.main._get_client",
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
            from jenkins_job_insight.config import Settings

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
            from jenkins_job_insight.config import Settings

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
            from jenkins_job_insight.config import Settings

            s = Settings()
            assert s.metadata_rules == []
