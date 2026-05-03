"""Tests for encryption of sensitive fields in request_params."""

import os
from unittest.mock import patch

import pytest

from jenkins_job_insight.encryption import (
    RESPONSE_REDACTED_KEYS,
    SENSITIVE_KEYS,
    _ENCRYPTED_PREFIX,
    _get_or_create_key_file,
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    strip_sensitive_from_response,
)


@pytest.fixture
def _stable_encryption_key():
    """Pin the encryption key so tests are deterministic."""
    with patch.dict(os.environ, {"JJI_ENCRYPTION_KEY": "test-secret-key"}):
        yield


@pytest.fixture
def fallback_key_env(tmp_path):
    """Patch env to remove JJI_ENCRYPTION_KEY and set XDG_DATA_HOME to tmp_path.

    Yields inside the patched environment so tests run with the
    file-based fallback key mechanism.
    """
    env = {k: v for k, v in os.environ.items() if k != "JJI_ENCRYPTION_KEY"}
    env["XDG_DATA_HOME"] = str(tmp_path)
    with patch.dict(os.environ, env, clear=True):
        yield


@pytest.fixture
def sample_params():
    """Return a ``request_params`` dict with sensitive and non-sensitive fields."""
    return {
        "ai_provider": "claude",
        "ai_model": "opus",
        "jenkins_url": "http://jenkins",
        "jenkins_user": "admin",
        "jenkins_password": "s3cret",  # pragma: allowlist secret
        "jenkins_ssl_verify": True,
        "jira_api_token": "jira-tok",  # pragma: allowlist secret
        "jira_pat": "jira-pat-value",  # pragma: allowlist secret
        "github_token": "ghp_abc123",  # pragma: allowlist secret
        "jira_url": "http://jira",
        "jira_email": "a@b.com",
        "base_url": "http://base",
    }


@pytest.mark.usefixtures("_stable_encryption_key")
class TestEncryptDecryptRoundTrip:
    """encrypt -> decrypt must yield the original values."""

    def test_round_trip_preserves_values(self, sample_params) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        decrypted = decrypt_sensitive_fields(encrypted)
        assert decrypted == sample_params

    def test_sensitive_fields_are_encrypted(self, sample_params) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        for key in SENSITIVE_KEYS:
            original = sample_params.get(key)
            if original:
                assert encrypted[key] != original
                assert encrypted[key].startswith(_ENCRYPTED_PREFIX)

    def test_non_sensitive_fields_unchanged(self, sample_params) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        for key in sample_params:
            if key not in SENSITIVE_KEYS:
                assert encrypted[key] == sample_params[key]


@pytest.mark.usefixtures("_stable_encryption_key")
class TestEmptyValues:
    """Empty or missing sensitive fields are left untouched."""

    def test_empty_string_not_encrypted(self) -> None:
        params = {"jenkins_password": "", "github_token": ""}
        encrypted = encrypt_sensitive_fields(params)
        assert encrypted["jenkins_password"] == ""
        assert encrypted["github_token"] == ""

    def test_missing_keys_not_added(self) -> None:
        params = {"ai_provider": "claude"}
        encrypted = encrypt_sensitive_fields(params)
        assert "jenkins_password" not in encrypted
        assert "github_token" not in encrypted


@pytest.mark.usefixtures("_stable_encryption_key")
class TestLegacyPlaintext:
    """Plaintext values without the ``enc:`` prefix pass through decryption."""

    def test_plaintext_values_returned_as_is(self) -> None:
        params = {
            "jenkins_password": "legacy-plain",  # noqa: S105  # pragma: allowlist secret
            "github_token": "ghp_old",  # noqa: S105  # pragma: allowlist secret
        }
        decrypted = decrypt_sensitive_fields(params)
        assert decrypted["jenkins_password"] == params["jenkins_password"]
        assert decrypted["github_token"] == params["github_token"]


class TestKeyChange:
    """Decryption with a different key leaves the ciphertext in place."""

    def test_changed_key_preserves_ciphertext(self, sample_params) -> None:
        with patch.dict(os.environ, {"JJI_ENCRYPTION_KEY": "key-A"}):
            encrypted = encrypt_sensitive_fields(sample_params)

        with patch.dict(os.environ, {"JJI_ENCRYPTION_KEY": "key-B"}):
            decrypted = decrypt_sensitive_fields(encrypted)

        # Decryption silently fails; encrypted value is kept.
        for key in SENSITIVE_KEYS:
            if sample_params.get(key):
                assert decrypted[key] == encrypted[key]


class TestFileBasedFallbackKey:
    """Without JJI_ENCRYPTION_KEY an auto-generated file key is used."""

    def test_round_trip_without_env_key(self, sample_params, fallback_key_env) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        decrypted = decrypt_sensitive_fields(encrypted)
        assert decrypted == sample_params

    def test_sensitive_fields_not_plaintext(
        self, sample_params, fallback_key_env
    ) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        for key in SENSITIVE_KEYS:
            original = sample_params.get(key)
            if original:
                assert encrypted[key] != original

    def test_key_file_created_on_first_use(self, tmp_path, fallback_key_env) -> None:
        key_file = tmp_path / "jji" / ".encryption_key"
        assert not key_file.exists()
        key = _get_or_create_key_file()
        assert key_file.exists()
        assert key_file.read_text().strip() == key
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"

    def test_key_file_reused_on_subsequent_calls(self, fallback_key_env) -> None:
        key1 = _get_or_create_key_file()
        key2 = _get_or_create_key_file()
        assert key1 == key2

    def test_reads_existing_key_after_create_race(
        self, tmp_path, fallback_key_env
    ) -> None:
        """When another process wins the atomic create, we read their key."""
        import secrets as _secrets

        existing_key = _secrets.token_urlsafe(32)
        key_file = tmp_path / "jji" / ".encryption_key"
        key_file.parent.mkdir(parents=True, exist_ok=True)

        # Simulate the TOCTOU race: the file does not exist when checked,
        # but os.open raises FileExistsError because another process created
        # it between the check and the open.  The side_effect callback
        # writes the file before raising, so the fallback read succeeds.
        def race_side_effect(*_args, **_kwargs):
            key_file.write_text(existing_key)
            key_file.chmod(0o600)
            raise FileExistsError

        with patch(
            "jenkins_job_insight.encryption.os.open", side_effect=race_side_effect
        ) as mock_os_open:
            assert _get_or_create_key_file() == existing_key
            mock_os_open.assert_called()


@pytest.mark.usefixtures("_stable_encryption_key")
class TestOriginalDictUnmutated:
    """encrypt/decrypt must not mutate the input dict."""

    def test_encrypt_does_not_mutate(self, sample_params) -> None:
        original = dict(sample_params)
        encrypt_sensitive_fields(sample_params)
        assert sample_params == original

    def test_decrypt_does_not_mutate(self, sample_params) -> None:
        encrypted = encrypt_sensitive_fields(sample_params)
        frozen = dict(encrypted)
        decrypt_sensitive_fields(encrypted)
        assert encrypted == frozen


class TestStripSensitiveFromResponse:
    """strip_sensitive_from_response removes credential fields from request_params."""

    def test_redacted_keys_are_superset_of_sensitive_keys(self) -> None:
        assert SENSITIVE_KEYS <= RESPONSE_REDACTED_KEYS

    def test_strips_all_redacted_keys(self, sample_params) -> None:
        result_data = {"job_name": "my-job", "request_params": dict(sample_params)}
        stripped = strip_sensitive_from_response(result_data)
        for key in RESPONSE_REDACTED_KEYS:
            assert key not in stripped["request_params"]

    def test_preserves_non_sensitive_fields(self, sample_params) -> None:
        result_data = {"job_name": "my-job", "request_params": dict(sample_params)}
        stripped = strip_sensitive_from_response(result_data)
        for key in sample_params:
            if key not in RESPONSE_REDACTED_KEYS:
                assert stripped["request_params"][key] == sample_params[key]

    def test_preserves_top_level_fields(self) -> None:
        result_data = {
            "job_name": "my-job",
            "build_number": 42,
            "request_params": {
                "jenkins_password": "secret"  # pragma: allowlist secret
            },
        }
        stripped = strip_sensitive_from_response(result_data)
        assert stripped["job_name"] == "my-job"
        assert stripped["build_number"] == 42

    def test_does_not_mutate_input(self, sample_params) -> None:
        result_data = {"request_params": dict(sample_params)}
        original_params = dict(result_data["request_params"])
        strip_sensitive_from_response(result_data)
        assert result_data["request_params"] == original_params

    def test_returns_original_when_no_request_params(self) -> None:
        result_data = {"job_name": "my-job", "failures": []}
        stripped = strip_sensitive_from_response(result_data)
        assert stripped is result_data

    def test_returns_empty_dict_as_is(self) -> None:
        assert strip_sensitive_from_response({}) == {}

    def test_returns_none_request_params_as_is(self) -> None:
        result_data = {"job_name": "my-job", "request_params": None}
        stripped = strip_sensitive_from_response(result_data)
        assert stripped is result_data

    def test_returns_none_as_is(self) -> None:
        assert strip_sensitive_from_response(None) is None

    @pytest.mark.parametrize(
        "key",
        sorted(RESPONSE_REDACTED_KEYS),
        ids=sorted(RESPONSE_REDACTED_KEYS),
    )
    def test_each_redacted_key_is_removed(self, key: str) -> None:
        result_data = {"request_params": {key: "value", "safe_key": "keep"}}
        stripped = strip_sensitive_from_response(result_data)
        assert key not in stripped["request_params"]
        assert stripped["request_params"]["safe_key"] == "keep"


@pytest.mark.usefixtures("_stable_encryption_key")
class TestAdditionalReposTokenEncryption:
    """Tests for encrypt/decrypt/strip of tokens nested in additional_repos."""

    def test_encrypt_additional_repos_token(self) -> None:
        """Tokens inside additional_repos entries are encrypted."""
        params = {
            "ai_provider": "claude",
            "additional_repos": [
                {
                    "name": "infra",
                    "url": "https://github.com/org/infra",
                    "token": "tok",
                },  # pragma: allowlist secret
                {"name": "product", "url": "https://github.com/org/product"},
            ],
        }
        encrypted = encrypt_sensitive_fields(params)
        assert encrypted["additional_repos"][0]["token"].startswith(_ENCRYPTED_PREFIX)
        assert "token" not in encrypted["additional_repos"][1]

    def test_decrypt_additional_repos_token(self) -> None:
        """Encrypted tokens in additional_repos are decrypted."""
        params = {
            "additional_repos": [
                {
                    "name": "infra",
                    "url": "https://github.com/org/infra",
                    "token": "tok",
                },  # pragma: allowlist secret
            ],
        }
        encrypted = encrypt_sensitive_fields(params)
        decrypted = decrypt_sensitive_fields(encrypted)
        assert (
            decrypted["additional_repos"][0]["token"] == "tok"  # noqa: S105  # pragma: allowlist secret
        )

    def test_round_trip_additional_repos_tokens(self) -> None:
        """encrypt -> decrypt preserves additional_repos tokens."""
        params = {
            "additional_repos": [
                {
                    "name": "a",
                    "url": "https://github.com/org/a",
                    "token": "tok1",
                },  # pragma: allowlist secret
                {"name": "b", "url": "https://github.com/org/b"},
                {
                    "name": "c",
                    "url": "https://github.com/org/c",
                    "token": "tok3",
                },  # pragma: allowlist secret
            ],
        }
        encrypted = encrypt_sensitive_fields(params)
        decrypted = decrypt_sensitive_fields(encrypted)
        assert (
            decrypted["additional_repos"][0]["token"] == "tok1"  # noqa: S105  # pragma: allowlist secret
        )
        assert "token" not in decrypted["additional_repos"][1]
        assert (
            decrypted["additional_repos"][2]["token"] == "tok3"  # noqa: S105  # pragma: allowlist secret
        )

    def test_strip_additional_repos_tokens(self) -> None:
        """strip_sensitive_from_response removes tokens from additional_repos."""
        result_data = {
            "request_params": {
                "ai_provider": "claude",
                "additional_repos": [
                    {
                        "name": "infra",
                        "url": "https://github.com/org/infra",
                        "token": "enc:xyz",
                    },  # pragma: allowlist secret
                    {"name": "product", "url": "https://github.com/org/product"},
                ],
            }
        }
        stripped = strip_sensitive_from_response(result_data)
        assert "token" not in stripped["request_params"]["additional_repos"][0]
        assert stripped["request_params"]["additional_repos"][0]["name"] == "infra"
        assert (
            stripped["request_params"]["additional_repos"][0]["url"]
            == "https://github.com/org/infra"
        )

    def test_empty_token_not_encrypted(self) -> None:
        """Empty string tokens are not encrypted."""
        params = {
            "additional_repos": [
                {"name": "infra", "url": "https://github.com/org/infra", "token": ""},
            ],
        }
        encrypted = encrypt_sensitive_fields(params)
        assert encrypted["additional_repos"][0]["token"] == ""

    def test_none_additional_repos_unchanged(self) -> None:
        """None additional_repos passes through unchanged."""
        params = {"additional_repos": None}
        encrypted = encrypt_sensitive_fields(params)
        assert encrypted["additional_repos"] is None

    def test_no_additional_repos_key(self) -> None:
        """Missing additional_repos key is fine."""
        params = {"ai_provider": "claude"}
        encrypted = encrypt_sensitive_fields(params)
        assert "additional_repos" not in encrypted
