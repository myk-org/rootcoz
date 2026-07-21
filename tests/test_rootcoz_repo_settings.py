"""Tests for .rootcoz/settings.json load, schema validation, and merge priority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rootcoz.config import Settings
from rootcoz.models import AdditionalRepo, BaseAnalysisRequest
from rootcoz.rootcoz_repo_settings import (
    ROOTCOZ_SETTINGS_SCHEMA_PATH,
    RootcozRepoSettings,
    RootcozSettingsError,
    apply_rootcoz_repo_settings,
    load_rootcoz_repo_settings,
    rootcoz_settings_json_schema,
    write_rootcoz_settings_schema,
)


def _write_settings(repo: Path, data: dict) -> Path:
    rootcoz = repo / ".rootcoz"
    rootcoz.mkdir(parents=True)
    path = rootcoz / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestRootcozRepoSettingsSchema:
    def test_valid_full_document(self) -> None:
        s = RootcozRepoSettings.model_validate(
            {
                "ai_provider": "claude",
                "ai_model": "claude-sonnet-4-5",
                "ai_call_timeout": 10,
                "max_concurrent_ai_calls": 3,
                "peer_ai_configs": [
                    {"ai_provider": "gemini", "ai_model": "gemini-2.5-pro"}
                ],
                "peer_analysis_max_rounds": 3,
                "additional_repos": [
                    {
                        "name": "product",
                        "url": "https://github.com/org/product",
                        "ref": "main",
                    }
                ],
            }
        )
        assert s.ai_provider == "claude"
        assert s.additional_repos is not None
        assert s.additional_repos[0].name == "product"

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(Exception, match="Unknown keys|extra"):
            RootcozRepoSettings.model_validate({"jenkins_url": "https://x"})

    def test_rejects_token_in_additional_repos(self) -> None:
        with pytest.raises(Exception):
            RootcozRepoSettings.model_validate(
                {
                    "additional_repos": [
                        {
                            "name": "product",
                            "url": "https://github.com/org/product",
                            "token": "secret",
                        }
                    ]
                }
            )

    def test_rejects_duplicate_additional_repo_names(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate additional repo names"):
            RootcozRepoSettings.model_validate(
                {
                    "additional_repos": [
                        {"name": "extra", "url": "https://github.com/org/a"},
                        {"name": "extra", "url": "https://github.com/org/b"},
                    ]
                }
            )

    def test_rejects_extra_keys_on_peer_configs(self) -> None:
        with pytest.raises(Exception, match="extra|forbidden"):
            RootcozRepoSettings.model_validate(
                {
                    "peer_ai_configs": [
                        {
                            "ai_provider": "claude",
                            "ai_model": "opus",
                            "unexpected_field": "nope",
                        }
                    ]
                }
            )

    def test_schema_file_matches_model(self, tmp_path: Path) -> None:
        generated = write_rootcoz_settings_schema(tmp_path / "schema.json")
        shipped = json.loads(ROOTCOZ_SETTINGS_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert json.loads(generated.read_text(encoding="utf-8")) == shipped
        assert rootcoz_settings_json_schema()["additionalProperties"] is False
        desc = rootcoz_settings_json_schema()["description"]
        assert "ai_provider/ai_model" in desc
        assert "server defaults > this file" in desc


class TestLoadRootcozRepoSettings:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_rootcoz_repo_settings(tmp_path) is None

    def test_valid_file(self, tmp_path: Path) -> None:
        _write_settings(
            tmp_path,
            {"ai_provider": "gemini", "ai_model": "gemini-2.5-flash"},
        )
        loaded = load_rootcoz_repo_settings(tmp_path)
        assert loaded is not None
        assert loaded.ai_provider == "gemini"
        assert loaded.ai_model == "gemini-2.5-flash"

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        rootcoz = tmp_path / ".rootcoz"
        rootcoz.mkdir()
        (rootcoz / "settings.json").write_text("{not-json", encoding="utf-8")
        with pytest.raises(RootcozSettingsError, match="Invalid JSON"):
            load_rootcoz_repo_settings(tmp_path)

    def test_schema_failure_raises(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, {"ai_call_timeout": -1})
        with pytest.raises(RootcozSettingsError, match="JSON Schema validation"):
            load_rootcoz_repo_settings(tmp_path)
        # Message must not embed raw ValidationError input values
        with pytest.raises(RootcozSettingsError) as exc_info:
            load_rootcoz_repo_settings(tmp_path)
        assert "input_value" not in str(exc_info.value)

    def test_rejects_duplicate_additional_repo_names_in_file(
        self, tmp_path: Path
    ) -> None:
        _write_settings(
            tmp_path,
            {
                "additional_repos": [
                    {"name": "dup", "url": "https://github.com/org/a"},
                    {"name": "dup", "url": "https://github.com/org/b"},
                ]
            },
        )
        with pytest.raises(RootcozSettingsError, match="JSON Schema validation"):
            load_rootcoz_repo_settings(tmp_path)

    def test_rejects_symlink_settings(self, tmp_path: Path) -> None:
        rootcoz = tmp_path / ".rootcoz"
        rootcoz.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text('{"ai_provider": "claude"}', encoding="utf-8")
        target = rootcoz / "settings.json"
        target.symlink_to(outside)
        with pytest.raises(RootcozSettingsError, match="symlink|regular file"):
            load_rootcoz_repo_settings(tmp_path)

    def test_rejects_non_utf8_settings(self, tmp_path: Path) -> None:
        rootcoz = tmp_path / ".rootcoz"
        rootcoz.mkdir()
        (rootcoz / "settings.json").write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(RootcozSettingsError, match="encoding|Invalid"):
            load_rootcoz_repo_settings(tmp_path)

    def test_request_tier_rounds_and_concurrency_win(self) -> None:
        body = BaseAnalysisRequest(
            peer_analysis_max_rounds=2,
            max_concurrent_ai_calls=7,
        )
        settings = Settings(
            peer_analysis_max_rounds=3,
            max_concurrent_ai_calls=3,
        )
        repo = RootcozRepoSettings(
            peer_analysis_max_rounds=9,
            max_concurrent_ai_calls=1,
        )
        effective = apply_rootcoz_repo_settings(body, settings, repo)
        assert effective.settings.peer_analysis_max_rounds == 2
        assert effective.settings.max_concurrent_ai_calls == 7

    def test_propagate_repo_settings_overlay_mutates_caller(self) -> None:
        from rootcoz.rootcoz_repo_settings import propagate_repo_settings_overlay

        caller = Settings(
            ai_call_timeout=10,
            max_concurrent_ai_calls=3,
            peer_analysis_max_rounds=3,
        )
        effective = Settings(
            ai_call_timeout=42,
            max_concurrent_ai_calls=9,
            peer_analysis_max_rounds=5,
        )
        propagate_repo_settings_overlay(caller, effective)
        assert caller.ai_call_timeout == 42
        assert caller.max_concurrent_ai_calls == 9
        assert caller.peer_analysis_max_rounds == 5

    def test_collision_detection(self) -> None:
        from rootcoz.rootcoz_repo_settings import assert_no_tests_repo_name_collision

        repos = [
            AdditionalRepo(name="my-tests", url="https://github.com/org/other", ref="")
        ]
        with pytest.raises(RootcozSettingsError, match="collides"):
            assert_no_tests_repo_name_collision("my-tests", repos)

    def test_unsupported_provider_rejected(self) -> None:
        body = BaseAnalysisRequest()
        settings = Settings(ai_provider="", ai_model="")
        # Force an unsupported provider via pre-resolved args when repo unset
        with pytest.raises(RootcozSettingsError, match="Unsupported AI provider"):
            apply_rootcoz_repo_settings(
                body,
                settings,
                None,
                ai_provider="not-a-provider",
                ai_model="x",
            )


class TestApplyRootcozRepoSettings:
    def test_request_wins_over_repo_and_server(self) -> None:
        body = BaseAnalysisRequest(ai_provider="cursor", ai_model="gpt-5")
        settings = Settings(ai_provider="claude", ai_model="claude-sonnet-4-5")
        repo = RootcozRepoSettings(
            ai_provider="gemini",
            ai_model="gemini-2.5-pro",
        )
        effective = apply_rootcoz_repo_settings(
            body, settings, repo, ai_provider="claude", ai_model="claude-sonnet-4-5"
        )
        assert effective.ai_provider == "cursor"
        assert effective.ai_model == "gpt-5"

    def test_repo_wins_over_server_when_request_unset(self) -> None:
        body = BaseAnalysisRequest()
        settings = Settings(
            ai_provider="claude",
            ai_model="claude-sonnet-4-5",
            ai_call_timeout=10,
            max_concurrent_ai_calls=3,
        )
        repo = RootcozRepoSettings(
            ai_provider="gemini",
            ai_model="gemini-2.5-pro",
            ai_call_timeout=20,
            max_concurrent_ai_calls=5,
            peer_ai_configs=[{"ai_provider": "claude", "ai_model": "opus"}],
            peer_analysis_max_rounds=4,
            additional_repos=[
                {
                    "name": "product",
                    "url": "https://github.com/org/product",
                    "ref": "main",
                }
            ],
        )
        effective = apply_rootcoz_repo_settings(
            body,
            settings,
            repo,
            ai_provider="claude",
            ai_model="claude-sonnet-4-5",
        )
        # AI provider/model: server wins over settings.json
        assert effective.ai_provider == "claude"
        assert effective.ai_model == "claude-sonnet-4-5"
        # Other keys: settings.json wins over server
        assert effective.settings.ai_call_timeout == 20
        assert effective.settings.max_concurrent_ai_calls == 5
        assert effective.settings.peer_analysis_max_rounds == 4
        assert effective.peer_ai_configs is not None
        assert len(effective.peer_ai_configs) == 1
        assert len(effective.additional_repos) == 1
        assert effective.additional_repos[0].name == "product"

    def test_repo_fills_ai_when_server_unset(self) -> None:
        body = BaseAnalysisRequest()
        settings = Settings(ai_provider="", ai_model="")
        repo = RootcozRepoSettings(
            ai_provider="gemini",
            ai_model="gemini-2.5-pro",
        )
        effective = apply_rootcoz_repo_settings(body, settings, repo)
        assert effective.ai_provider == "gemini"
        assert effective.ai_model == "gemini-2.5-pro"

    def test_empty_request_additional_repos_disables(self) -> None:
        body = BaseAnalysisRequest(additional_repos=[])
        settings = Settings(additional_repos="product:https://github.com/org/product")
        repo = RootcozRepoSettings(
            additional_repos=[
                {
                    "name": "from-repo",
                    "url": "https://github.com/org/from-repo",
                }
            ]
        )
        effective = apply_rootcoz_repo_settings(body, settings, repo)
        assert effective.additional_repos == []

    def test_server_used_when_no_repo_file(self) -> None:
        body = BaseAnalysisRequest()
        settings = Settings(
            ai_provider="claude",
            ai_model="sonnet",
            peer_ai_configs="gemini:flash",
        )
        effective = apply_rootcoz_repo_settings(
            body,
            settings,
            None,
            ai_provider="claude",
            ai_model="sonnet",
        )
        assert effective.ai_provider == "claude"
        assert effective.ai_model == "sonnet"
        assert effective.peer_ai_configs is not None
        assert effective.peer_ai_configs[0]["ai_provider"] == "gemini"
