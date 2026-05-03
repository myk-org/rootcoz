"""Tests for VAPID key auto-generation and configuration."""

import json
import os
from unittest.mock import patch

from jenkins_job_insight.vapid import (
    DEFAULT_CLAIM_EMAIL,
    _generate_vapid_keys,
    _get_or_create_vapid_keys,
    get_vapid_config,
)


class TestGenerateVapidKeys:
    """Tests for _generate_vapid_keys()."""

    def test_returns_public_and_private(self):
        keys = _generate_vapid_keys()
        assert "public_key" in keys
        assert "private_key" in keys
        assert isinstance(keys["public_key"], str)
        assert isinstance(keys["private_key"], str)
        assert len(keys["public_key"]) > 0
        assert len(keys["private_key"]) > 0

    def test_keys_are_unique(self):
        k1 = _generate_vapid_keys()
        k2 = _generate_vapid_keys()
        assert k1["public_key"] != k2["public_key"]
        assert k1["private_key"] != k2["private_key"]


class TestGetOrCreateVapidKeys:
    """Tests for _get_or_create_vapid_keys() file persistence."""

    def test_creates_key_file_on_first_use(self, tmp_path):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            keys = _get_or_create_vapid_keys()
        key_file = tmp_path / "jji" / ".vapid_keys.json"
        assert key_file.exists()
        stored = json.loads(key_file.read_text())
        assert stored["public_key"] == keys["public_key"]
        assert stored["private_key"] == keys["private_key"]

    def test_reuses_existing_key_file(self, tmp_path):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            keys1 = _get_or_create_vapid_keys()
            keys2 = _get_or_create_vapid_keys()
        assert keys1 == keys2

    def test_file_permissions_0600(self, tmp_path):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            _get_or_create_vapid_keys()
        key_file = tmp_path / "jji" / ".vapid_keys.json"
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_tightens_loose_permissions(self, tmp_path):
        """If the file was created with loose permissions, they get tightened."""
        jji_dir = tmp_path / "jji"
        jji_dir.mkdir()
        key_file = jji_dir / ".vapid_keys.json"
        keys = _generate_vapid_keys()
        key_file.write_text(json.dumps(keys))
        key_file.chmod(0o644)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            result = _get_or_create_vapid_keys()
        assert result == keys
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_handles_corrupt_file(self, tmp_path):
        """Corrupt file triggers regeneration."""
        jji_dir = tmp_path / "jji"
        jji_dir.mkdir()
        key_file = jji_dir / ".vapid_keys.json"
        key_file.write_text("not valid json")
        key_file.chmod(0o600)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            keys = _get_or_create_vapid_keys()
        assert keys["public_key"]
        assert keys["private_key"]

    def test_handles_race_condition(self, tmp_path, monkeypatch):
        """When another process wins the O_EXCL race, falls back to reading their file."""
        jji_dir = tmp_path / "jji"
        jji_dir.mkdir()
        key_file = jji_dir / ".vapid_keys.json"
        existing_keys = _generate_vapid_keys()
        key_file.write_text(json.dumps(existing_keys))
        key_file.chmod(0o600)

        from pathlib import Path as _Path

        real_exists = _Path.exists
        monkeypatch.setattr(
            _Path,
            "exists",
            lambda self: False if self == key_file else real_exists(self),
        )
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}, clear=False):
            keys = _get_or_create_vapid_keys()
        assert keys == existing_keys


class TestGetVapidConfig:
    """Tests for get_vapid_config()."""

    def test_returns_env_vars_when_set(self):
        env = {
            "VAPID_PUBLIC_KEY": "test-pub",
            "VAPID_PRIVATE_KEY": "test-priv",  # pragma: allowlist secret  # gitleaks:allow
            "VAPID_CLAIM_EMAIL": "test@example.com",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = get_vapid_config()
        assert cfg["public_key"] == "test-pub"
        _priv = "test-priv"  # pragma: allowlist secret  # gitleaks:allow
        assert cfg["private_key"] == _priv
        assert cfg["claim_email"] == "test@example.com"

    def test_env_vars_take_priority(self, tmp_path):
        """Even if a key file exists, env vars are used."""
        env = {
            "VAPID_PUBLIC_KEY": "env-pub",
            "VAPID_PRIVATE_KEY": "env-priv",  # pragma: allowlist secret  # gitleaks:allow
            "VAPID_CLAIM_EMAIL": "env@example.com",
            "XDG_DATA_HOME": str(tmp_path),
        }
        # Create a key file with different values
        jji_dir = tmp_path / "jji"
        jji_dir.mkdir()
        _priv = "file-priv"  # pragma: allowlist secret  # gitleaks:allow
        key_data = {"public_key": "file-pub", "private_key": _priv}
        (jji_dir / ".vapid_keys.json").write_text(json.dumps(key_data))
        with patch.dict(os.environ, env, clear=False):
            cfg = get_vapid_config()
        assert cfg["public_key"] == "env-pub"
        _priv = "env-priv"  # pragma: allowlist secret  # gitleaks:allow
        assert cfg["private_key"] == _priv

    def test_default_claim_email_when_not_set(self, tmp_path):
        """Uses default claim email when VAPID_CLAIM_EMAIL is not set."""
        env = {
            "VAPID_PUBLIC_KEY": "pub",
            "VAPID_PRIVATE_KEY": "priv",  # pragma: allowlist secret  # gitleaks:allow
            "VAPID_CLAIM_EMAIL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = get_vapid_config()
        assert cfg["claim_email"] == DEFAULT_CLAIM_EMAIL

    def test_auto_generates_when_no_env_vars(self, tmp_path):
        """Auto-generates keys when env vars are empty."""
        env = {
            "VAPID_PUBLIC_KEY": "",
            "VAPID_PRIVATE_KEY": "",  # pragma: allowlist secret  # gitleaks:allow
            "VAPID_CLAIM_EMAIL": "",
            "XDG_DATA_HOME": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = get_vapid_config()
        assert cfg["public_key"]
        assert cfg["private_key"]
        assert cfg["claim_email"] == DEFAULT_CLAIM_EMAIL

    def test_returns_empty_dict_on_failure(self):
        """Returns {} when key generation fails."""
        with (
            patch(
                "jenkins_job_insight.vapid._get_or_create_vapid_keys",
                side_effect=RuntimeError("boom"),
            ),
            patch.dict(
                os.environ,
                {"VAPID_PUBLIC_KEY": "", "VAPID_PRIVATE_KEY": ""},
                clear=False,
            ),
        ):
            cfg = get_vapid_config()
        assert cfg == {}
