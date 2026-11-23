"""SSO credentials that are only whitespace, and date claims that are not numbers.

Both are one failure in two places: a value that looks present and is not usable.
A whitespace-only client id or secret used to render a provider button that could
never finish a login, and a ``NaN``/``inf``/``true`` date claim slipped past the
ID-token expiry checks because every comparison against those values is false.

The IdP is never contacted: the token is genuinely signed and verified locally.
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from rootcoz.auth.sso import AuthError, configured_providers, get_provider
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.config import clear_settings_cache, get_settings
from tests.conftest import build_test_env, make_app_client

#: Every SSO credential present but blank — the shape a masked admin form can
#: save, and the one that must not advertise a provider.
_BLANK_SSO_ENV = build_test_env(
    PUBLIC_BASE_URL="https://rootcoz.example",
    SSO_GOOGLE_CLIENT_ID=" ",
    SSO_GOOGLE_CLIENT_SECRET="  ",  # pragma: allowlist secret
    SSO_GITHUB_CLIENT_ID="\t",
    SSO_GITHUB_CLIENT_SECRET=" ",  # pragma: allowlist secret
    SSO_OPENSHIFT_CLIENT_ID="  ",
    SSO_OPENSHIFT_CLIENT_SECRET="   ",  # pragma: allowlist secret
    SSO_OPENSHIFT_ISSUER="  ",
    SSO_OPENSHIFT_API_URL="  ",
    SSO_REDHAT_CLIENT_ID=" ",
    SSO_REDHAT_CLIENT_SECRET="  ",  # pragma: allowlist secret
    SSO_REDHAT_ISSUER="  ",
)


def _settings(env: dict[str, str]):
    """Build fresh settings from ``env`` alone, with no DB override cached."""
    with patch.dict(os.environ, env, clear=True):
        clear_settings_cache()
        return get_settings()


def test_whitespace_only_credentials_configure_no_provider() -> None:
    """A blank id or secret is not a configuration: nothing is offered."""
    settings = _settings(_BLANK_SSO_ENV)
    assert configured_providers(settings) == []
    for name in ("google", "github", "openshift", "redhat"):
        assert get_provider(name, settings) is None


def test_the_providers_endpoint_lists_nothing_for_blank_credentials(
    temp_db_path: Path,
) -> None:
    """The login page renders exactly this list, so a blank one shows no button."""
    for client in make_app_client(temp_db_path, _BLANK_SSO_ENV):
        assert client.get("/api/auth/sso/providers").json() == {
            "providers": [],
            # `_BLANK_SSO_ENV` sets PUBLIC_BASE_URL, so the flow itself is usable.
            "browser_login": True,
        }


def test_a_real_credential_pair_still_configures_its_provider() -> None:
    """Normalisation must not disable a provider that is genuinely configured."""
    env = build_test_env(
        PUBLIC_BASE_URL="https://rootcoz.example",
        SSO_GITHUB_CLIENT_ID="gh-id",
        SSO_GITHUB_CLIENT_SECRET="gh-secret",  # pragma: allowlist secret
    )
    settings = _settings(env)
    assert [provider.name for provider in configured_providers(settings)] == ["github"]
    assert get_provider("github", settings) is not None


def test_a_blank_secret_setting_becomes_none_and_a_real_one_keeps_its_value() -> None:
    """An empty SecretStr is an absent credential; surrounding space is not a value."""
    blank = _settings(
        build_test_env(SSO_GOOGLE_CLIENT_SECRET="   ")  # pragma: allowlist secret
    )
    assert blank.sso_google_client_secret is None

    real = _settings(
        build_test_env(
            SSO_GOOGLE_CLIENT_SECRET="  s3cret-value  "  # pragma: allowlist secret
        )
    )
    assert isinstance(real.sso_google_client_secret, SecretStr)
    assert real.sso_google_client_secret.get_secret_value() == "s3cret-value"


def _signed_id_token(overrides: dict[str, object]) -> tuple[str, object]:
    """Build a real RS256 ID token plus the key set that verifies it.

    Args:
        overrides: Claims to replace with the given value.

    Returns:
        The compact JWS and a key set usable by ``joserfc``.
    """
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims: dict[str, object] = {
        "iss": GoogleProvider.issuer,
        "aud": "test-client-id",
        "sub": "sub-1",
        "email": "alice@example.com",
        "nonce": "attempt-nonce",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    claims.update(overrides)
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    return token, KeySet.import_key_set({"keys": [key.as_dict(private=False)]})


async def _verify(token: str, key_set: object) -> dict:
    """Run ``_verify_id_token`` with the JWKS fetch stubbed out."""
    provider = GoogleProvider(
        client_id="test-client-id",
        client_secret="s",
        issuer=GoogleProvider.issuer,
    )
    with patch.object(GoogleProvider, "_jwks_for", return_value=key_set, autospec=True):
        return await provider._verify_id_token(
            {"jwks_uri": "https://accounts.google.com/jwks"}, token, "attempt-nonce"
        )


@pytest.mark.parametrize("claim", ["exp", "nbf", "iat"])
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), True], ids=str
)
async def test_a_malformed_date_claim_is_refused(claim: str, value: object) -> None:
    """NaN, an infinity and a bool compare false against every bound."""
    token, key_set = _signed_id_token({claim: value})
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert claim in str(excinfo.value)


async def test_an_expiry_too_large_for_a_float_is_the_far_future() -> None:
    """A huge integer exp is a date no clock reaches — not a crash out of a refusal."""
    token, key_set = _signed_id_token({"exp": 10**400})
    claims = await _verify(token, key_set)
    assert claims["exp"] == 10**400


async def test_a_normal_token_is_still_accepted() -> None:
    """The date checks keep their old meaning for well-formed claims."""
    token, key_set = _signed_id_token({"nbf": int(time.time()) - 5})
    claims = await _verify(token, key_set)
    assert claims["sub"] == "sub-1"


async def test_a_future_nbf_and_iat_are_still_refused() -> None:
    """The new predicate must not weaken the rules it now guards."""
    for claim in ("nbf", "iat"):
        token, key_set = _signed_id_token({claim: int(time.time()) + 300})
        with pytest.raises(AuthError) as excinfo:
            await _verify(token, key_set)
        assert excinfo.value.reason == "id_token_invalid"


async def test_a_negative_overflowing_exp_is_refused_as_expired() -> None:
    """A huge negative integer exp is the far past — the token is expired."""
    token, key_set = _signed_id_token({"exp": -(10**400)})
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "expired" in str(excinfo.value).lower()


async def test_a_positive_overflowing_exp_is_still_the_far_future() -> None:
    """Positive overflow is unchanged — the existing contract is preserved."""
    token, key_set = _signed_id_token({"exp": 10**400})
    claims = await _verify(token, key_set)
    assert claims["exp"] == 10**400
