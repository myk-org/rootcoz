"""Tests for first-party SSO: the provider package and the browser flow.

Stage 3 of issue #197 covers Google (the OIDC path OpenShift and Red Hat SSO
reuse later); Stage 6 adds the first-login choice screen. The IdP itself is
mocked — no test talks to Google — but the ID-token verification runs for real
against locally generated keys.

The rule these tests protect: a **name is never used to find an account**. The
first login asks the person (claim an existing account with its own credential,
or create one), and a `(provider, subject)` link is the only thing that resolves
a login automatically.
"""

import base64
import contextlib
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage
from rootcoz.auth import identity as auth_identity
from rootcoz.auth.sso import AuthError, ClaimSet, configured_providers, get_provider
from rootcoz.auth.sso import state as sso_state
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.config import clear_settings_cache, get_settings, normalize_domain_list
from rootcoz.encryption import encrypt_value
from tests.conftest import build_test_env, make_app_client

_SSO_ENV = {
    "SSO_GOOGLE_CLIENT_ID": "test-client-id",
    "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    "PUBLIC_BASE_URL": "https://rootcoz.example",
}

#: What the settings page's own POST carries — the proof a link start needs.
_SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def _claims(
    subject: str = "sub-1",
    username_hint: str = "alice",
    email: str | None = "alice@example.com",
) -> ClaimSet:
    """A claim set as a provider would produce it."""
    return ClaimSet(
        subject=subject,
        username_hint=username_hint,
        email=email,
        # Google asserts verification; the allow-list tests depend on it.
        claims={
            "sub": subject,
            "email": email,
            "email_verified": True,
            "preferred_username": username_hint,
        },
    )


def _returning(value: dict):
    """An async stand-in for ``request_json`` that returns ``value``."""

    async def _call(*args, **kwargs):
        return value

    return _call


def _sealed(
    provider: str = "google",
    next_path: str = "/report/1",
    mode: str = sso_state.MODE_LOGIN,
    link_to: str = "",
) -> tuple[str, str]:
    """Seal a login or link attempt and return (sealed cookie, state value)."""
    attempt, sealed, _challenge = sso_state.new_attempt(
        provider=provider,
        next_path=next_path,
        ttl_seconds=600,
        mode=mode,
        link_to=link_to,
    )
    return sealed, attempt.state


@contextlib.contextmanager
def _github_client(transport):
    """Run with `httpx.AsyncClient` bound to a mock transport.

    The real class is captured before the patch, so every call in the flow gets
    its own client (the helper opens and closes one per request) without the
    patch re-entering itself.
    """
    import httpx

    real_client = httpx.AsyncClient

    def factory(*_args: object, **_kwargs: object) -> object:
        return real_client(transport=transport, timeout=5.0)

    with patch("httpx.AsyncClient", factory):
        yield


async def _ticket(
    client=None,
    *,
    provider: str = "google",
    subject: str = "sub-1",
    email: str | None = "alice@example.com",
    suggested: str = "alice",
) -> str:
    """Mint a first-login ticket as the callback would.

    When ``client`` is given, the browser-binding cookie the callback sets is
    attached too — the ticket alone is deliberately not enough.
    """
    ticket = await storage.create_sso_ticket(
        provider=provider,
        subject=subject,
        email=email,
        suggested_username=suggested,
    )
    if client is not None:
        client.cookies.set(sso_state.TICKET_COOKIE, sso_state.ticket_binding(ticket))
    return ticket


async def _last_logout_reason(temp_db_path: Path) -> str | None:
    """The `idp_logout` reason on the most recent logout row."""
    rows = [
        r for r in await _audit_actions(temp_db_path) if r["action"] == "auth.logout"
    ]
    return rows[-1]["details"]["idp_logout"] if rows else None


async def _last_sso_refusal(temp_db_path: Path) -> dict:
    """The most recent SSO refusal row, for its reason and subject."""
    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"] == "auth.sso_login_failed"
    ]
    return rows[0] if rows else {}


async def _audit_actions(temp_db_path: Path) -> list[dict]:
    """Every audit row written to the test database."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        result = await storage.list_audit_events(limit=200)
    return result["events"]


def test_a_provider_is_offered_only_when_it_is_configured() -> None:
    """A button that cannot work must never be rendered."""
    import os

    with patch.dict(os.environ, build_test_env(), clear=True):
        clear_settings_cache()
        assert configured_providers(get_settings()) == []
        assert get_provider("google", get_settings()) is None
        assert get_provider("nope", get_settings()) is None

    with patch.dict(
        os.environ,
        build_test_env(
            SSO_GOOGLE_CLIENT_ID="cid",
            SSO_GOOGLE_CLIENT_SECRET="secret",  # pragma: allowlist secret
        ),
        clear=True,
    ):
        clear_settings_cache()
        assert [p.name for p in configured_providers(get_settings())] == ["google"]
        assert get_provider("google", get_settings()) is not None
        assert get_provider("github", get_settings()) is None

    clear_settings_cache()


@pytest.mark.parametrize(
    "url,allow_loopback,expected",
    [
        ("https://idp.example.com/logout", False, True),
        ("http://idp.example.com/logout", True, False),
        ("http://localhost:8443/logout", True, True),
        ("http://127.0.0.1/logout", True, True),
        ("https://u:p@idp.example.com/logout", True, False),  # pragma: allowlist secret
        ("https://idp.example.com/logout#frag", True, False),
        ("https://idp.example.com/logout\n", True, False),
        ("javascript:alert(1)", True, False),
        ("https://[::1/x", True, False),
        ("", True, False),
    ],
)
def test_the_navigation_predicate_answers_a_boolean(
    url: str, allow_loopback: bool, expected: bool
) -> None:
    """One rule for "may the browser go here", and it never raises."""
    from rootcoz.url_utils import is_safe_navigation_url

    assert is_safe_navigation_url(url, allow_loopback_http=allow_loopback) is expected


def test_the_registry_and_storage_agree_on_provider_names() -> None:
    """A provider storage does not know would fail at the first link."""
    from rootcoz.auth.sso import PROVIDER_BY_NAME

    # Equality, not a subset: a provider in storage but missing from the registry
    # is exactly the omission this stage could introduce.
    assert set(PROVIDER_BY_NAME) == set(storage.SSO_PROVIDERS)
    assert GoogleProvider.issuer == "https://accounts.google.com"


def test_each_provider_is_offered_only_when_fully_configured() -> None:
    """A half-configured provider would render a button that cannot work."""
    import os

    from rootcoz.auth.sso import configured_providers
    from rootcoz.auth.sso.github import GitHubProvider
    from rootcoz.auth.sso.openshift import OpenShiftProvider
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    full = build_test_env(
        SSO_GITHUB_CLIENT_ID="gh-id",
        SSO_GITHUB_CLIENT_SECRET="gh-secret",  # pragma: allowlist secret
        SSO_OPENSHIFT_CLIENT_ID="ocp-id",
        SSO_OPENSHIFT_CLIENT_SECRET="ocp-secret",  # pragma: allowlist secret
        SSO_OPENSHIFT_ISSUER="https://oauth-openshift.apps.example.com",
        SSO_OPENSHIFT_API_URL="https://api.example.com:6443",
        SSO_REDHAT_CLIENT_ID="rh-id",
        SSO_REDHAT_CLIENT_SECRET="rh-secret",  # pragma: allowlist secret
        SSO_REDHAT_ISSUER="https://sso.redhat.com/auth/realms/redhat-external",
    )
    with patch.dict(os.environ, full, clear=True):
        clear_settings_cache()
        names = [provider.name for provider in configured_providers(get_settings())]
        assert names == ["github", "openshift", "redhat"]
        # OpenShift's cluster origin drives its OAuth2 flow, its API URL the account
        # lookup; Red Hat SSO's issuer is what its OIDC discovery is built from.
        assert get_provider("openshift", get_settings()) is not None
        assert get_provider("redhat", get_settings()).issuer == (
            "https://sso.redhat.com/auth/realms/redhat-external"
        )

    # An OIDC provider without its issuer cannot work, so it is not offered.
    missing_issuer = build_test_env(
        SSO_OPENSHIFT_CLIENT_ID="ocp-id",
        SSO_OPENSHIFT_CLIENT_SECRET="ocp-secret",  # pragma: allowlist secret
        SSO_OPENSHIFT_API_URL="https://api.example.com:6443",
    )
    with patch.dict(os.environ, missing_issuer, clear=True):
        clear_settings_cache()
        assert configured_providers(get_settings()) == []
        assert get_provider("openshift", get_settings()) is None
        assert OpenShiftProvider.from_settings(get_settings()) is None

    # The API URL is the second origin: without it every lookup would go to the
    # OAuth server, which serves only /oauth/*.
    missing_api_url = build_test_env(
        SSO_OPENSHIFT_CLIENT_ID="ocp-id",
        SSO_OPENSHIFT_CLIENT_SECRET="ocp-secret",  # pragma: allowlist secret
        SSO_OPENSHIFT_ISSUER="https://oauth-openshift.apps.example.com",
    )
    with patch.dict(os.environ, missing_api_url, clear=True):
        clear_settings_cache()
        assert configured_providers(get_settings()) == []
        assert OpenShiftProvider.from_settings(get_settings()) is None

    # An issuer without a scheme is a deployment error, not a hidden provider:
    # settings validation refuses it (the same way an invalid DEFAULT_USER_ROLE
    # fails at startup) instead of making the button quietly disappear.
    scheme_less = build_test_env(
        SSO_OPENSHIFT_CLIENT_ID="ocp-id",
        SSO_OPENSHIFT_CLIENT_SECRET="ocp-secret",  # pragma: allowlist secret
        SSO_OPENSHIFT_ISSUER="oauth-openshift.apps.example.com",
    )
    from pydantic import ValidationError

    with patch.dict(os.environ, scheme_less, clear=True):
        clear_settings_cache()
        with pytest.raises(ValidationError, match="http"):
            get_settings()

    # GitHub needs both credentials and nothing else.
    github_only = build_test_env(SSO_GITHUB_CLIENT_ID="gh-id")
    with patch.dict(os.environ, github_only, clear=True):
        clear_settings_cache()
        assert GitHubProvider.from_settings(get_settings()) is None

    clear_settings_cache()
    assert RedHatSSOProvider.name == "redhat"


def test_the_providers_endpoint_lists_only_configured_providers(
    temp_db_path: Path,
) -> None:
    """The login page asks this and renders exactly what comes back."""
    for client in make_app_client(temp_db_path):
        assert client.get("/api/auth/sso/providers").json() == {
            "providers": [],
            "browser_login": False,
        }

    for client in make_app_client(temp_db_path, _SSO_ENV):
        assert client.get("/api/auth/sso/providers").json() == {
            "providers": [{"name": "google", "display_name": "Google"}],
            # `_SSO_ENV` sets PUBLIC_BASE_URL, so a browser login can complete.
            "browser_login": True,
        }


def test_the_state_cookie_is_sealed_and_bound_to_its_provider() -> None:
    """The attempt must not be readable, transferable, or open to another IdP."""
    sealed, state = _sealed()
    attempt = sso_state.open_attempt(sealed, provider="google")
    assert attempt.state == state
    assert attempt.next_path == "/report/1"
    assert attempt.code_verifier not in sealed
    assert attempt.nonce

    with pytest.raises(sso_state.StateError, match="another provider"):
        sso_state.open_attempt(sealed, provider="github")
    with pytest.raises(sso_state.StateError, match="missing"):
        sso_state.open_attempt("", provider="google")


def test_a_forged_plaintext_attempt_is_refused() -> None:
    """An attacker who can set the cookie must not be believed.

    The general decryption helper passes unprefixed values through as legacy
    plaintext; a sealed cookie cannot allow that, or the CSRF nonce and the PKCE
    verifier would both be attacker-chosen.
    """
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "purpose": "rootcoz.sso.login_attempt",
                    "provider": "google",
                    "state": "attacker-nonce",
                    "verifier": "attacker-verifier",
                    "nonce": "attacker-id-token-nonce",
                    "next": "/",
                    "issued_at": int(time.time()),
                    "ttl": 600,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )

    with pytest.raises(sso_state.StateError, match="not sealed"):
        sso_state.open_attempt(forged, provider="google")


def test_a_sealed_value_minted_for_another_purpose_is_refused() -> None:
    """A sealed cookie from another feature must not be replayable here."""
    other = encrypt_value(
        json.dumps(
            {
                "purpose": "something.else",
                "provider": "google",
                "state": "s",
                "verifier": "v",
                "nonce": "n",
                "next": "/",
                "issued_at": int(time.time()),
                "ttl": 600,
            }
        )
    )
    packed = base64.urlsafe_b64encode(other.encode()).decode().rstrip("=")
    with pytest.raises(sso_state.StateError, match="something else"):
        sso_state.open_attempt(packed, provider="google")


def test_an_expired_attempt_is_refused() -> None:
    """A link captured hours ago cannot be replayed."""
    payload = json.dumps(
        {
            "purpose": "rootcoz.sso.login_attempt",
            "provider": "google",
            "state": "s",
            "verifier": "v",
            "nonce": "n",
            "next": "/",
            "issued_at": 1,
            "ttl": 60,
        }
    )
    stale = (
        base64.urlsafe_b64encode(encrypt_value(payload).encode()).decode().rstrip("=")
    )
    with pytest.raises(sso_state.StateError, match="expired"):
        sso_state.open_attempt(stale, provider="google")


@pytest.mark.parametrize(
    "field,value",
    [
        ("sso_openshift_issuer", "oauth-openshift.apps.example.com"),
        ("sso_openshift_issuer", "ftp://example.com"),
        ("sso_openshift_issuer", "http://oauth-openshift.apps.example.com"),
        ("sso_openshift_api_url", "api.example.com:6443"),
        ("sso_openshift_api_url", "ftp://api.example.com:6443"),
        ("sso_openshift_api_url", "http://api.example.com:6443"),
        ("sso_redhat_issuer", "sso.redhat.com/auth/realms/x"),
        ("sso_redhat_issuer", "http://sso.redhat.com/auth/realms/x"),
    ],
)
def test_a_malformed_issuer_is_refused_where_it_is_written(
    field: str, value: str
) -> None:
    """A bad issuer used to make the provider silently vanish from the login page."""
    from pydantic import ValidationError

    from rootcoz.config import Settings

    with pytest.raises(ValidationError, match="http"):
        Settings.model_validate({field: value})


def test_a_plaintext_origin_disables_the_provider_at_runtime() -> None:
    """A value that bypassed validation (an old DB row) must not be discovered.

    Settings validation refuses such a value on write; this is the second line
    of defence for a row that predates the rule.
    """
    from pydantic import SecretStr

    from rootcoz.auth.sso.openshift import OpenShiftProvider

    settings = get_settings().model_copy(
        update={
            "sso_openshift_client_id": "ocp-id",
            "sso_openshift_client_secret": SecretStr(
                "ocp-secret"  # pragma: allowlist secret
            ),
            "sso_openshift_issuer": "http://oauth-openshift.apps.example.com",
            "sso_openshift_api_url": "https://api.example.com:6443",
        }
    )
    assert OpenShiftProvider.from_settings(settings) is None

    # The API URL crosses the network with the caller's token, so it is held to
    # the same rule as the issuer.
    plaintext_api = settings.model_copy(
        update={
            "sso_openshift_issuer": "https://oauth-openshift.apps.example.com",
            "sso_openshift_api_url": "http://api.example.com:6443",
        }
    )
    assert OpenShiftProvider.from_settings(plaintext_api) is None

    # A loopback dev server is allowed for both, so a local cluster still works.
    dev = settings.model_copy(
        update={
            "sso_openshift_issuer": "http://localhost:8443",
            "sso_openshift_api_url": "http://localhost:6443",
        }
    )
    assert OpenShiftProvider.from_settings(dev) is not None


def test_a_malformed_list_is_refused_wherever_settings_are_built() -> None:
    """The field validator makes every write path reject it, not just the login."""
    from pydantic import ValidationError

    from rootcoz.config import Settings

    with pytest.raises(ValidationError, match="Invalid SSO domain entry"):
        Settings.model_validate({"sso_allowed_domains": "*"})
    # A valid list round-trips unchanged (the accessor does the parsing).
    assert Settings.model_validate(
        {"sso_allowed_domains": "@RedHat.com"}
    ).sso_allowed_domains_set == frozenset({"redhat.com"})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/report/1", "/report/1"),
        ("https://evil.example/x", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("", "/"),
        ("/" + "x" * 600, "/"),
    ],
)
def test_the_post_login_destination_cannot_leave_the_site(
    raw: str, expected: str
) -> None:
    """An SSO link must not double as an open redirect."""
    assert sso_state.safe_next(raw) == expected


def _github_transport(*, emails: object, user: dict | None = None):
    """A MockTransport standing in for GitHub's endpoints.

    Returns the transport and the list of requests it saw, so a test can assert
    the token POST shape as well as the outcome.
    """
    import httpx

    seen: list[httpx.Request] = []
    default_user = {"id": 42, "login": "rnetser", "email": "old@example.com"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_token"})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=emails)
        if request.url.path == "/user":
            return httpx.Response(200, json=user if user is not None else default_user)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize(
    "provider,asserts",
    [("google", True), ("github", True), ("openshift", False), ("redhat", True)],
)
def test_each_provider_declares_whether_it_asserts_verification(
    provider: str, asserts: bool
) -> None:
    """The domain rule can only demand a claim the IdP actually publishes."""
    from rootcoz.auth.sso import PROVIDER_BY_NAME

    assert (
        PROVIDER_BY_NAME[provider].__dict__.get("asserts_email_verified", asserts)
        is asserts
    )


async def test_an_idp_without_a_verification_claim_still_uses_the_domain_rule(
    temp_db_path: Path,
) -> None:
    """OpenShift publishes no `email_verified`; the domain check is still applied."""
    from rootcoz.auth.sso.base import ClaimSet as BaseClaimSet

    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        # A claim set from a provider that does not assert verification.
        openshift_claims = BaseClaimSet(
            subject="ocp-1",
            username_hint="alice",
            email="alice@redhat.com",
            claims={"sub": "ocp-1", "email": "alice@redhat.com"},
            asserts_email_verified=False,
        )
        choice = await auth_identity.resolve(
            "openshift", openshift_claims, get_settings()
        )
        assert isinstance(choice, auth_identity.LoginChoice)

        # The domain rule still refuses a domain that is not listed.
        wrong = BaseClaimSet(
            subject="ocp-2",
            username_hint="alice",
            email="alice@gmail.com",
            claims={"sub": "ocp-2", "email": "alice@gmail.com"},
            asserts_email_verified=False,
        )
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("openshift", wrong, get_settings())
        assert excinfo.value.reason == "domain_not_allowed"


async def test_github_returns_the_verified_primary_address() -> None:
    """The real HTTP path: GitHub is OAuth2, and only a verified address counts."""
    import httpx

    from rootcoz.auth.sso.github import GitHubProvider

    transport, seen = _github_transport(
        emails=[
            {"email": "old@example.com", "primary": False, "verified": False},
            {"email": "rnetser@redhat.com", "primary": True, "verified": True},
        ]
    )
    provider = GitHubProvider(client_id="id", client_secret="secret")
    with _github_client(transport):
        claims = await provider.exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )

    assert claims.subject == "42"
    assert claims.username_hint == "rnetser"
    assert claims.email == "rnetser@redhat.com"
    assert claims.claims["email_verified"] is True

    token_request = next(r for r in seen if r.url.path == "/login/oauth/access_token")
    body = token_request.content.decode()
    assert "grant_type" not in body  # GitHub's OAuth2 token POST has no such field
    assert "code=c" in body and "client_id=id" in body
    assert token_request.headers.get("accept") == "application/json"
    emails_request = next(r for r in seen if r.url.path == "/user/emails")
    assert emails_request.headers.get("authorization") == "Bearer gho_token"
    assert httpx  # the transport itself is the proof the helper ran


async def test_github_prefers_the_primary_verified_address() -> None:
    """Order in GitHub's response must not decide which address is used."""
    from rootcoz.auth.sso.github import GitHubProvider

    transport, _seen = _github_transport(
        emails=[
            {"email": "secondary@redhat.com", "primary": False, "verified": True},
            {"email": "primary@redhat.com", "primary": True, "verified": True},
        ]
    )
    provider = GitHubProvider(client_id="id", client_secret="secret")
    with _github_client(transport):
        claims = await provider.exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )

    assert claims.email == "primary@redhat.com"


async def test_github_without_a_verified_address_reports_it_unverified() -> None:
    """No verified address → `email_verified: false`, so the domain rule refuses it."""
    from rootcoz.auth.sso.github import GitHubProvider

    transport, _seen = _github_transport(
        emails=[{"email": "unverified@example.com", "primary": True, "verified": False}]
    )
    provider = GitHubProvider(client_id="id", client_secret="secret")
    with _github_client(transport):
        claims = await provider.exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )

    assert claims.claims["email_verified"] is False
    # Not stored on the identity: an unverified address is one its holder can
    # change at will. It stays in `claims` for the audit trail.
    assert claims.email is None
    assert claims.claims["email"] == "old@example.com"


async def test_github_rejects_a_token_response_without_a_token() -> None:
    """A 200 with an error body is still a failure, not a login."""
    import httpx

    from rootcoz.auth.sso.github import GitHubProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    provider = GitHubProvider(client_id="id", client_secret="secret")
    with (
        _github_client(httpx.MockTransport(handler)),
        pytest.raises(AuthError) as excinfo,
    ):
        await provider.exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )
    assert excinfo.value.reason == "exchange_failed"


def test_the_http_helper_checks_status_and_shape() -> None:
    """A 500, a non-JSON body and a wrong shape are all the same refusal."""
    import asyncio

    import httpx

    from rootcoz.auth.sso.http import request_json

    def run(handler, *, expect: str = "object"):
        transport = httpx.MockTransport(handler)
        with _github_client(transport):
            return asyncio.run(
                request_json(
                    "GET", "https://api.github.com/x", reason="r", expect=expect
                )
            )

    assert run(lambda request: httpx.Response(200, json={"a": 1})) == {"a": 1}
    assert run(lambda request: httpx.Response(200, json=[1, 2]), expect="array") == [
        1,
        2,
    ]

    for handler, expect in (
        (lambda request: httpx.Response(500, json={}), "object"),
        (lambda request: httpx.Response(200, text="not json"), "object"),
        (lambda request: httpx.Response(200, json=[1]), "object"),
        (lambda request: httpx.Response(200, json={"a": 1}), "array"),
    ):
        with pytest.raises(AuthError) as excinfo:
            run(handler, expect=expect)
        assert excinfo.value.reason == "r"


async def test_github_authorization_url_asks_for_the_email_scope() -> None:
    """Without `user:email` GitHub returns no address at all."""
    from rootcoz.auth.sso.github import GitHubProvider

    provider = GitHubProvider(client_id="id", client_secret="secret")
    url = await provider.authorization_url(
        redirect_uri="https://x/cb", state="st", code_challenge="", nonce=""
    )
    assert url.startswith(GitHubProvider.authorize_endpoint)
    assert "user%3Aemail" in url
    assert "state=st" in url
    assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url


def _openshift_transport(user: dict, identities: dict | None = None):
    """A MockTransport standing in for a cluster's OAuth server and API server.

    Returns the transport and the requests it saw, so a test can assert which of
    the two origins each call reached.
    """
    import httpx

    seen: list[httpx.Request] = []
    identity_prefix = "/apis/user.openshift.io/v1/identities/"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "sha256~token"})
        if request.url.path == "/apis/user.openshift.io/v1/users/~":
            return httpx.Response(200, json=user)
        if request.url.path.startswith(identity_prefix):
            name = request.url.path[len(identity_prefix) :]
            found = (identities or {}).get(name)
            if found is None:
                return httpx.Response(404, json={"kind": "Status"})
            return httpx.Response(200, json=found)
        return httpx.Response(404, json={"kind": "Status"})

    return httpx.MockTransport(handler), seen


def _openshift_provider():
    """The provider with both of the cluster's origins configured."""
    from rootcoz.auth.sso.openshift import OpenShiftProvider

    return OpenShiftProvider(
        client_id="id",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://oauth-openshift.apps.example.com",
        api_url="https://api.example.com:6443",
    )


async def test_openshift_reads_the_account_from_the_api_server() -> None:
    """The OAuth origin serves only /oauth/*: the account lives on the API host."""
    import httpx

    user = {
        "metadata": {"uid": "u-1", "name": "alice"},
        "identities": ["oidc:alice"],
    }
    transport, seen = _openshift_transport(
        user, {"oidc:alice": {"extra": {"email": ["alice@example.com"]}}}
    )
    with _github_client(transport):
        claims = await _openshift_provider().exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )

    assert claims.subject == "u-1"
    assert claims.username_hint == "alice"
    assert claims.email == "alice@example.com"
    assert httpx  # the transport itself is the proof the helper ran

    token_request = next(r for r in seen if r.url.path == "/oauth/token")
    assert token_request.url.host == "oauth-openshift.apps.example.com"
    user_request = next(r for r in seen if r.url.path.endswith("/users/~"))
    assert user_request.url.host == "api.example.com"
    assert user_request.url.port == 6443
    assert user_request.headers["authorization"] == "Bearer sha256~token"
    identity_request = next(r for r in seen if "/identities/" in r.url.path)
    assert identity_request.url.host == "api.example.com"


@pytest.mark.parametrize(
    "user",
    [
        {"metadata": [1]},
        {"metadata": "u-1"},
        {"identities": ["oidc:alice"]},
        {"metadata": {"name": "alice"}},
        {"metadata": {"uid": "", "name": "alice"}},
    ],
)
async def test_a_cluster_reply_without_a_usable_account_id_is_a_refusal(
    user: dict,
) -> None:
    """Malformed required data is the handled refusal, never an AttributeError."""
    transport, _seen = _openshift_transport(user)
    with _github_client(transport), pytest.raises(AuthError) as excinfo:
        await _openshift_provider().exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )
    assert excinfo.value.reason == "no_subject"


@pytest.mark.parametrize(
    "user,identities",
    [
        # No identities list at all, and one that is not a list.
        ({"metadata": {"uid": "u-1"}}, {}),
        ({"metadata": {"uid": "u-1"}, "identities": "oidc:alice"}, {}),
        # Entries that are not identity names are skipped, not dereferenced.
        ({"metadata": {"uid": "u-1"}, "identities": [None, 7, ""]}, {}),
        # An identity the token cannot read is skipped too.
        ({"metadata": {"uid": "u-1"}, "identities": ["oidc:gone"]}, {}),
        # An identity without an address (or with a malformed one) is normal.
        ({"metadata": {"uid": "u-1"}, "identities": ["oidc:a"]}, {"oidc:a": {}}),
        (
            {"metadata": {"uid": "u-1"}, "identities": ["oidc:a"]},
            {"oidc:a": {"email": 1, "extra": [1]}},
        ),
    ],
)
async def test_optional_identity_data_that_is_malformed_is_skipped(
    user: dict, identities: dict
) -> None:
    """Only SSO_ALLOWED_DOMAINS wants the address, so a gap never fails a login."""
    transport, _seen = _openshift_transport(user, identities)
    with _github_client(transport):
        claims = await _openshift_provider().exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )
    assert claims.subject == "u-1"
    assert claims.email is None


async def test_a_recycled_account_name_is_not_the_previous_person(
    temp_db_path: Path,
) -> None:
    """A cluster can reassign metadata.name; the link is on the immutable uid."""
    for client in make_app_client(temp_db_path):
        ticket = await _ticket(
            client, provider="openshift", subject="uid-old", suggested="alice"
        )
        created = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert created.status_code == 200, created.text

        # The same name now belongs to a different person (a new uid).
        user = {"metadata": {"uid": "uid-new", "name": "alice"}}
        transport, _seen = _openshift_transport(user)
        with _github_client(transport):
            claims = await _openshift_provider().exchange(
                code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
            )

        assert claims.subject == "uid-new"
        assert claims.username_hint == "alice"
        # Not signed into the previous owner's account: this is a new choice.
        choice = await auth_identity.resolve("openshift", claims, get_settings())
        assert isinstance(choice, auth_identity.LoginChoice)
        assert choice.suggested_username == "alice"


async def test_an_unlinked_identity_gets_a_suggestion_not_an_account(
    temp_db_path: Path,
) -> None:
    """The resolver never creates anything: it reports that a choice is needed."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        choice = await auth_identity.resolve("google", _claims(), get_settings())

        assert isinstance(choice, auth_identity.LoginChoice)
        assert choice.suggested_username == "alice"
        assert choice.email == "alice@example.com"
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_a_reserved_name_is_never_suggested(temp_db_path: Path) -> None:
    """The screen never proposes a name storage would refuse."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        for hint in ("admin", "rootcoz-ai", "re-analyze"):
            choice = await auth_identity.resolve(
                "google", _claims(username_hint=hint, email=None), get_settings()
            )
            assert isinstance(choice, auth_identity.LoginChoice)
            assert choice.suggested_username == ""

        choice = await auth_identity.resolve(
            "google",
            _claims(username_hint="admin", email="alice@example.com"),
            get_settings(),
        )
        assert isinstance(choice, auth_identity.LoginChoice)
        assert choice.suggested_username == "alice"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", frozenset()),
        ("   ", frozenset()),
        ("@RedHat.com", frozenset({"redhat.com"})),
        ("redhat.com.", frozenset({"redhat.com"})),
        ("a.com, B.com ", frozenset({"a.com", "b.com"})),
        ("a.com,,b.com", frozenset({"a.com", "b.com"})),
    ],
)
def test_the_domain_list_is_normalised(raw: str, expected: frozenset[str]) -> None:
    """Case, a leading '@', blanks and whitespace never change the meaning."""
    assert normalize_domain_list(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "*",
        "*.redhat.com",
        "https://redhat.com",
        "redhat.com/x",
        "red hat.com",
        ".redhat.com",
        # A value that yields no domain would silently disable the policy.
        ",",
        ",,",
        "@",
    ],
)
def test_a_malformed_domain_list_is_refused(raw: str) -> None:
    """A wildcard, a URI, a dot-entry or a list of blanks must fail closed."""
    with pytest.raises(ValueError, match="Invalid SSO domain entry"):
        normalize_domain_list(raw)


@pytest.mark.parametrize(
    "email,allowed,verdict",
    [
        ("alice@redhat.com", "redhat.com", "ok"),
        ("alice@eng.redhat.com", "redhat.com", "ok"),
        ("alice@REDHAT.com", "redhat.com", "ok"),
        ("alice@redhat.com", "@redhat.com", "ok"),
        ("alice@notredhat.com", "redhat.com", "denied"),
        ("alice@redhat.com.evil.io", "redhat.com", "denied"),
        ("alice@redhat.co", "redhat.com", "denied"),
        ("alice@gmail.com", "redhat.com", "denied"),
        ("alice@redhat.com", "", "ok"),
        ("alice@anything.example", "", "ok"),
    ],
)
async def test_the_domain_allow_list_decides_who_may_sign_in(
    temp_db_path: Path, email: str, allowed: str, verdict: str
) -> None:
    """Exact domain or a real subdomain — never a look-alike suffix."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS=allowed)
    for _client in make_app_client(temp_db_path, env):
        if verdict == "ok":
            choice = await auth_identity.resolve(
                "google", _claims(email=email), get_settings()
            )
            assert isinstance(choice, auth_identity.LoginChoice)
        else:
            with pytest.raises(AuthError) as excinfo:
                await auth_identity.resolve(
                    "google", _claims(email=email), get_settings()
                )
            assert excinfo.value.reason == "domain_not_allowed"


async def test_a_ticket_cannot_finish_after_its_domain_is_revoked(
    temp_db_path: Path,
) -> None:
    """A ten-minute ticket must not outlive the policy that produced it."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for client in make_app_client(temp_db_path, env):
        # Minted while the domain was allowed (the callback mints it).
        ticket = await _ticket(client, email="alice@redhat.com")

        # The admin tightens the list before the person presses "Create"; the
        # setting is reloaded per request, so no restart is involved.
        tightened = client.put(
            "/api/admin/settings",
            json={"settings": {"sso_allowed_domains": "example.org"}},
            headers={
                "Authorization": "Bearer test-admin-key-16chars"
            },  # pragma: allowlist secret
        )
        assert tightened.status_code == 200

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )

        assert response.status_code == 403
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_an_unlisted_domain_creates_nothing(temp_db_path: Path) -> None:
    """Refused before the account lookup, so nothing is written for a stranger."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for client in make_app_client(temp_db_path, env):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider,
            "exchange",
            return_value=_claims(email="alice@gmail.com"),
            autospec=True,
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=domain_not_allowed"
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_an_unverified_email_is_refused_when_the_list_is_set(
    temp_db_path: Path,
) -> None:
    """The list is only as good as the claim it checks."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        claims = ClaimSet(
            subject="sub-1",
            username_hint="alice",
            email="alice@redhat.com",
            claims={
                "sub": "sub-1",
                "email": "alice@redhat.com",
                "email_verified": False,
            },
        )
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", claims, get_settings())
        assert excinfo.value.reason == "email_not_verified"


async def test_a_verified_email_passes_and_a_silent_provider_does_not(
    temp_db_path: Path,
) -> None:
    """Only a positive `email_verified` assertion satisfies the list."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        verified = ClaimSet(
            subject="sub-ok",
            username_hint="alice",
            email="alice@redhat.com",
            claims={
                "sub": "sub-ok",
                "email": "alice@redhat.com",
                "email_verified": True,
            },
        )
        choice = await auth_identity.resolve("google", verified, get_settings())
        assert isinstance(choice, auth_identity.LoginChoice)

        silent = ClaimSet(
            subject="sub-silent",
            username_hint="alice",
            email="alice@redhat.com",
            claims={"sub": "sub-silent", "email": "alice@redhat.com"},
        )
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", silent, get_settings())
        assert excinfo.value.reason == "email_not_verified"


async def test_a_missing_email_is_refused_when_the_list_is_set(
    temp_db_path: Path,
) -> None:
    """No address means nothing to check: fail closed, do not guess."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", _claims(email=None), get_settings())
        assert excinfo.value.reason == "domain_not_allowed"


async def test_revoking_a_domain_cuts_off_an_already_linked_account(
    temp_db_path: Path,
) -> None:
    """The list applies on every login, not only at creation."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")

        # Allowed now.
        resolved = await auth_identity.resolve("google", _claims(), get_settings())
        assert isinstance(resolved, auth_identity.IdentityResolution)

    revoked = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="example.org")
    for _client in make_app_client(temp_db_path, revoked):
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", _claims(), get_settings())
        assert excinfo.value.reason == "domain_not_allowed"


async def test_the_domain_policy_does_not_touch_key_logins(temp_db_path: Path) -> None:
    """Username + API key and ADMIN_KEY ignore the SSO domain list entirely."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for client in make_app_client(temp_db_path, env):
        _name, raw_key = await storage.create_user("alice")
        login = client.post(
            "/api/auth/login", json={"username": "alice", "api_key": raw_key}
        )
        assert login.status_code == 200
        assert client.get("/api/auth/me").status_code == 200

        # The bootstrap admin never consults the list, so a restrictive domain
        # can never lock an operator out of the deployment.
        admin = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert admin.status_code == 200
        assert admin.json()["is_admin"] is True


async def test_the_domain_refusal_is_audited_without_the_address(
    temp_db_path: Path,
) -> None:
    """The trail carries provider and reason — never the address (not even the domain).

    `AGENTS.md` allows provider, username, reason and subject ids in `details`;
    an address is contact data, so it stays out.
    """
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for client in make_app_client(temp_db_path, env):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider,
            "exchange",
            return_value=_claims(email="alice@gmail.com"),
            autospec=True,
        ):
            client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"] == "auth.sso_login_failed"
    ]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["details"] == {
        "provider": "google",
        "reason": "domain_not_allowed",
    }
    assert "alice@gmail.com" not in json.dumps(rows)


async def test_a_linked_account_skips_the_choice(temp_db_path: Path) -> None:
    """The link — never the name — is what resolves a login automatically."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")

        resolved = await auth_identity.resolve(
            "google", _claims(username_hint="alice"), get_settings()
        )
        assert isinstance(resolved, auth_identity.IdentityResolution)
        assert resolved.username == "ruthn"
        assert resolved.created is False


async def test_an_orphaned_link_is_healed_on_the_next_login(
    temp_db_path: Path,
) -> None:
    """A link whose account is gone must not lock that person out forever."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")
        async with storage._connect_db() as db:
            await db.execute("DELETE FROM users WHERE username = 'alice'")
            await db.commit()

        choice = await auth_identity.resolve("google", _claims(), get_settings())

        assert isinstance(choice, auth_identity.LoginChoice)
        # The orphan is gone, so the choice screen can create or link again.
        assert await storage.get_user_identity("google", "sub-1") is None


async def test_a_disabled_linked_account_is_refused(temp_db_path: Path) -> None:
    """A live link to a disabled account does not authenticate."""
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE users SET disabled = 1 WHERE username = 'ruthn'")

        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("google", _claims(), get_settings())
        assert excinfo.value.reason == "account_disabled"


async def test_the_callback_redirects_to_the_choice_screen(
    temp_db_path: Path,
) -> None:
    """First login: a ticket, not an account and not a refusal."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login#sso_ticket=")
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_the_ticket_never_reveals_the_address(temp_db_path: Path) -> None:
    """Name + masked email would reconstruct it, so only the domain is returned."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        body = client.post("/api/auth/sso/ticket", json={"ticket": ticket}).json()

        assert body["provider"] == "google"
        assert body["suggested_username"] == "alice"
        assert body["email_domain"] == "example.com"
        assert "alice@example.com" not in json.dumps(body)

        assert (
            client.post("/api/auth/sso/ticket", json={"ticket": "nope"}).status_code
            == 404
        )


async def test_creating_an_account_with_the_chosen_name(temp_db_path: Path) -> None:
    """The person picks the name; it is created, linked and signed in."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert {k: v for k, v in body.items() if k != "api_key"} == {
            "username": "ruthn",
            "status": "active",
            # The ticket's destination rides back so the SPA does not guess it.
            "next": "/",
        }
        # The account's only credential its owner has ever seen: shown once.
        assert body["api_key"].startswith("rootcoz_")
        assert response.headers["cache-control"] == "no-store"
        assert response.cookies.get("rootcoz_session")
        identity = await storage.get_user_identity("google", "sub-1")
        assert identity is not None
        assert identity["username"] == "ruthn"
        assert client.get("/api/auth/me").json()["username"] == "ruthn"

        replay = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "other"}
        )
        assert replay.status_code == 404


async def test_a_taken_name_is_refused_not_suffixed(temp_db_path: Path) -> None:
    """A name already in use is a bad choice, never a silent rename."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, existing_key = await storage.create_user("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 400
        # No `ruthn-1`, no second row, and the existing account is untouched.
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT username FROM users ORDER BY username")
            assert [row["username"] for row in await cursor.fetchall()] == ["ruthn"]
        assert await storage.get_user_by_key(existing_key) is not None
        # The ticket is still usable with another name.
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "rnetser"}
            ).status_code
            == 200
        )


async def test_a_taken_keyless_name_cannot_be_claimed_by_creating(
    temp_db_path: Path,
) -> None:
    """``create_user``'s "existing account without a key" path must not apply.

    That path mints a key for an existing keyless row; reusing it here would hand
    somebody else's account to whoever completed the IdP login.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        async with storage._connect_db() as db:
            await db.execute(
                "INSERT INTO users (username, role, status) VALUES ('ruthn', 'reviewer', 'active')"
            )
            await db.commit()
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code == 400
        assert await storage.get_user_identity("google", "sub-1") is None
        # The keyless account did not gain a key behind its owner's back.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["api_key_hash"] is None


async def test_create_records_provider_provenance(temp_db_path: Path) -> None:
    """``created_via`` names the provider, and a later link does not rewrite it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT created_via FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["created_via"] == "google"


async def test_linking_does_not_rewrite_provenance(temp_db_path: Path) -> None:
    """An account created internally keeps ``internal`` after a link."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn", created_via="internal")
        ticket = await _ticket(client)
        # A fresh key the test can use; provenance must survive the link.
        raw_key = await storage.rotate_own_key("ruthn")

        assert (
            client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            ).status_code
            == 200
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT created_via FROM users WHERE username = 'ruthn'"
            )
            assert (await cursor.fetchone())["created_via"] == "internal"


async def test_the_choice_keeps_the_requested_destination(temp_db_path: Path) -> None:
    """A deep link survives the detour and rides back so the SPA can use it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed(next_path="/report/42")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )
        # The fragment carries the ticket; the screen reads it and POSTs it back.
        ticket = response.headers["location"].split("sso_ticket=", 1)[1]
        created = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert created.status_code == 200
        assert created.json()["next"] == "/report/42"


async def test_linking_respects_the_allow_list(temp_db_path: Path) -> None:
    """Linking writes a credential, so the write list applies to it."""
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 403
        assert await storage.list_user_identities("ruthn") == []


async def test_a_stolen_ticket_cannot_be_redeemed_in_another_browser(
    temp_db_path: Path,
) -> None:
    """The URL alone must not bind an identity to the thief's own account.

    Otherwise a stolen link lets an attacker attach the victim's IdP identity to
    the attacker's account, so every later victim login lands there.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        # The thief's browser: same ticket, no binding cookie.
        client.cookies.clear()

        assert (
            client.post("/api/auth/sso/ticket", json={"ticket": ticket}).status_code
            == 404
        )

        await storage.create_user("attacker")
        attacker_key = await storage.rotate_own_key("attacker")
        link = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "attacker", "api_key": attacker_key},
        )
        assert link.status_code == 404
        assert await storage.get_user_identity("google", "sub-1") is None

        # The browser that started the flow still holds the binding and can use it.
        client.cookies.set(sso_state.TICKET_COOKIE, sso_state.ticket_binding(ticket))
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
            ).status_code
            == 200
        )


async def test_linking_twice_reports_a_conflict_at_the_endpoint(
    temp_db_path: Path,
) -> None:
    """A subject already owned by another account is a 409, not a silent relink."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-1")

        await storage.create_user("bob")
        bob_key = await storage.rotate_own_key("bob")
        ticket = await _ticket(client, subject="sub-1")
        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "bob", "api_key": bob_key},
        )

        assert response.status_code == 409
        # The refusal does not name the account that owns the subject.
        assert "alice" not in response.json()["detail"]
        assert await storage.list_user_identities("bob") == []


async def test_a_stale_link_that_raced_a_delete_is_reported_as_missing(
    temp_db_path: Path,
) -> None:
    """A vanished account is 404, never "already linked"."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        with patch(
            "rootcoz.storage.add_user_identity",
            side_effect=ValueError("User 'ruthn' not found"),
        ):
            response = client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            )

        assert response.status_code == 404
        assert "no longer exists" in response.json()["detail"]


async def test_the_bootstrap_admin_is_told_it_has_no_account_to_link(
    temp_db_path: Path,
) -> None:
    """A valid ADMIN_KEY must not be reported as an invalid credential."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={
                "ticket": ticket,
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )

        assert response.status_code == 400
        assert "bootstrap admin" in response.json()["detail"]
        # And the ticket is untouched, so the person can still create an account.
        assert await storage.peek_sso_ticket(ticket) is not None


async def test_a_bad_key_counts_towards_the_lockout(temp_db_path: Path) -> None:
    """The link endpoint verifies the same credential, so it needs the same guard."""
    env = dict(_SSO_ENV, LOGIN_LOCKOUT_ENABLED="true", LOGIN_LOCKOUT_THRESHOLD="2")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        ticket = await _ticket(client)

        for _attempt in range(2):
            response = client.post(
                "/api/auth/sso/link",
                json={
                    "ticket": ticket,
                    "username": "ruthn",
                    # A deliberately wrong credential, not a secret.
                    "api_key": "wrong-key-long",  # pragma: allowlist secret
                },
            )
            assert response.status_code == 401

        # The account is locked, so even the right key cannot claim it now.
        raw_key = await storage.rotate_own_key("ruthn")
        blocked = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )
        assert blocked.status_code == 403


async def test_creating_with_an_invalid_name_keeps_the_ticket(
    temp_db_path: Path,
) -> None:
    """A rejected name must not cost the person another trip through the IdP."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        for bad in ("admin", "rootcoz-ai", "a", "-bad"):
            response = client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": bad}
            )
            assert response.status_code == 400, bad

        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )


async def test_creating_under_approval_lands_on_pending(temp_db_path: Path) -> None:
    """The approval gate applies to a choice-screen account exactly as before."""
    for client in make_app_client(
        temp_db_path, dict(_SSO_ENV, REQUIRE_APPROVAL="true")
    ):
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert await storage.get_user_status("alice") == "pending"
        assert client.get("/api/auth/me").status_code == 403


async def test_creating_respects_the_allow_list(temp_db_path: Path) -> None:
    """``ALLOWED_USERS`` bounds who may be created, even by choosing a name."""
    env = dict(_SSO_ENV, ALLOWED_USERS="ruthn")
    for client in make_app_client(temp_db_path, env):
        ticket = await _ticket(client)

        refused = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert refused.status_code == 400

        allowed = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )
        assert allowed.status_code == 200


async def test_linking_an_existing_account_with_its_key(temp_db_path: Path) -> None:
    """The credential proves ownership; the name never does."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, raw_key = await storage.create_user("ruthn")
        before = await storage.get_user_by_username("ruthn")
        assert before is not None
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "username": "ruthn",
            "status": "active",
            # The ticket's destination rides back so the SPA does not guess it.
            "next": "/",
        }
        identity = await storage.get_user_identity("google", "sub-1")
        assert identity is not None
        assert identity["username"] == "ruthn"
        after = await storage.get_user_by_username("ruthn")
        assert after is not None
        assert after["id"] == before["id"]
        assert await storage.get_user_by_key(raw_key) is not None
        assert client.get("/api/auth/me").json()["username"] == "ruthn"


async def test_a_wrong_key_cannot_claim_an_account(temp_db_path: Path) -> None:
    """Without the credential the ticket entitles the holder to nothing."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={
                "ticket": ticket,
                "username": "ruthn",
                # A deliberately wrong credential, not a secret.
                "api_key": "wrong-key-long",  # pragma: allowlist secret
            },
        )

        assert response.status_code == 401
        assert await storage.list_user_identities("ruthn") == []
        assert await storage.peek_sso_ticket(ticket) is not None


async def test_a_disabled_account_cannot_be_claimed(temp_db_path: Path) -> None:
    """A refused account is refused before the link is written."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, raw_key = await storage.create_user("ruthn")
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE users SET disabled = 1 WHERE username = 'ruthn'")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 403
        assert await storage.list_user_identities("ruthn") == []


async def test_a_second_login_uses_the_link_and_skips_the_screen(
    temp_db_path: Path,
) -> None:
    """Once linked, the callback signs in directly and stamps the session."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
            ).status_code
            == 200
        )
        client.cookies.clear()

        sealed, state = _sealed(next_path="/report/42")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/report/42"
        token = response.cookies.get("rootcoz_session")
        assert token
        session = await storage.get_session(token)
        assert session is not None
        assert session["auth_provider"] == "google"


async def test_a_linked_pending_account_lands_on_pending(temp_db_path: Path) -> None:
    """The approval gate still decides where a linked login lands."""
    env = dict(_SSO_ENV, REQUIRE_APPROVAL="true")
    for client in make_app_client(temp_db_path, env):
        ticket = await _ticket(client)
        created = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
        )
        assert created.json()["status"] == "pending"
        client.cookies.clear()

        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/pending"
        assert client.get("/api/auth/me").status_code == 403


async def test_an_anonymous_logout_writes_no_audit_row(temp_db_path: Path) -> None:
    """The route is public; a call that cleared nothing must not fill the trail."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["idp_logout_url"] is None

    assert await _audit_actions(temp_db_path) == []


async def test_logout_of_an_api_key_session_has_no_idp_url(
    temp_db_path: Path,
) -> None:
    """Nothing external to end for a session this deployment issued itself."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _name, raw_key = await storage.create_user("alice")
        client.post("/api/auth/login", json={"username": "alice", "api_key": raw_key})

        response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["idp_logout_url"] is None
        # The local session is gone either way.
        assert client.get("/api/auth/me").status_code == 401


async def test_logout_of_an_sso_session_ends_the_provider_session(
    temp_db_path: Path,
) -> None:
    """The IdP session is only endable when the provider advertises a URL."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        with patch.object(
            OIDCProvider,
            "metadata",
            return_value={"end_session_endpoint": "https://accounts.google.com/logout"},
            autospec=True,
        ):
            response = client.post("/api/auth/logout")

        assert response.status_code == 200
        url = response.json()["idp_logout_url"]
        assert url is not None
        assert url.startswith("https://accounts.google.com/logout")
        assert "post_logout_redirect_uri=https%3A%2F%2Frootcoz.example%2Flogin" in url
        # Keycloak needs the client id to validate that redirect, since no ID
        # token hint is sent.
        assert "client_id=test-client-id" in url
        # The IdP URL is a bonus: the local session is already gone.
        assert client.get("/api/auth/me").status_code == 401

    rows = [
        r for r in await _audit_actions(temp_db_path) if r["action"] == "auth.logout"
    ]
    assert rows[-1]["details"] == {"session_cleared": True, "idp_logout": "offered"}


@pytest.mark.parametrize(
    "endpoint",
    [
        "javascript:alert(1)",
        "http://sso.example.com/logout",
        # userinfo in a discovered URL must be refused, not navigated to
        "https://user:pass@sso.example.com/logout",  # pragma: allowlist secret
        "https://sso.example.com/logout#frag",
        "",
    ],
)
async def test_a_discovered_end_session_endpoint_must_be_a_plain_https_url(
    endpoint: str,
) -> None:
    """Discovery is trusted for identity, not for navigation."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    provider = OIDCProvider(client_id="cid", client_secret="s", issuer="https://i")
    with patch.object(
        OIDCProvider,
        "metadata",
        return_value={"end_session_endpoint": endpoint},
        autospec=True,
    ):
        assert await provider.end_session_url("https://rootcoz.example/login") is None


async def test_an_endpoint_that_already_has_a_query_keeps_it() -> None:
    """A discovered endpoint may carry parameters; a second '?' would corrupt it."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    provider = OIDCProvider(client_id="cid", client_secret="s", issuer="https://i")
    with patch.object(
        OIDCProvider,
        "metadata",
        return_value={"end_session_endpoint": "https://sso.example.com/logout?realm=x"},
        autospec=True,
    ):
        url = await provider.end_session_url("https://rootcoz.example/login")

    assert url is not None
    assert "?realm=x&" in url
    assert "?realm=x?" not in url


async def test_a_loopback_dev_endpoint_is_allowed() -> None:
    """A local Keycloak over http is a real development setup."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    provider = OIDCProvider(client_id="cid", client_secret="s", issuer="https://i")
    with patch.object(
        OIDCProvider,
        "metadata",
        return_value={"end_session_endpoint": "http://localhost:8443/logout"},
        autospec=True,
    ):
        url = await provider.end_session_url("https://rootcoz.example/login")
    assert url is not None and url.startswith("http://localhost:8443/logout")


async def test_a_provider_without_end_session_logs_out_locally(
    temp_db_path: Path,
) -> None:
    """GitHub has no end-session endpoint at all; the answer is null, not a URL."""
    env = dict(
        _SSO_ENV,
        SSO_GITHUB_CLIENT_ID="gh-id",
        SSO_GITHUB_CLIENT_SECRET="gh-secret",  # pragma: allowlist secret
    )
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="github"
        )
        client.cookies.set("rootcoz_session", token)

        response = client.post("/api/auth/logout")

        assert response.json()["idp_logout_url"] is None
        # The local logout always happened first.
        assert client.get("/api/auth/me").status_code == 401

    assert await _last_logout_reason(temp_db_path) == "local_only"


async def test_sso_logout_deletes_the_session_row(temp_db_path: Path) -> None:
    """The row is gone, not merely the cookie: a copied cookie must not survive."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        with patch.object(OIDCProvider, "metadata", return_value={}, autospec=True):
            client.post("/api/auth/logout")

        # Neither lookup sees it any more — the cookie was not the only copy.
        assert await storage.get_session(token) is None
        assert await storage.get_session_identity(token) is None


async def test_a_provider_whose_settings_were_removed_logs_out_locally(
    temp_db_path: Path,
) -> None:
    """`provider_unavailable`: the session names a provider the deployment dropped."""
    env = {
        # No Google client pair: the provider is not offered any more.
        "PUBLIC_BASE_URL": "https://rootcoz.example",
    }
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        response = client.post("/api/auth/logout")

        assert response.json()["idp_logout_url"] is None
        assert client.get("/api/auth/me").status_code == 401

    assert await _last_logout_reason(temp_db_path) == "provider_unavailable"


async def test_a_provider_bug_does_not_fail_the_logout(
    temp_db_path: Path,
) -> None:
    """An unexpected provider error is logged, and the logout still succeeds."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        with patch.object(
            OIDCProvider,
            "end_session_url",
            side_effect=RuntimeError("provider bug"),
            autospec=True,
        ):
            response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["idp_logout_url"] is None
        # Local logout is authoritative even when the provider misbehaves.
        assert await storage.get_session(token) is None


async def test_an_expired_session_still_ends_the_provider_session(
    temp_db_path: Path,
) -> None:
    """A cookie can outlive its row's TTL; the IdP session it opened has not."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        # Expire the row: get_session no longer sees it, a cookie would linger.
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE sessions SET expires_at = '2000-01-01 00:00:00' WHERE 1"
            )
            await db.commit()
        client.cookies.set("rootcoz_session", token)

        with patch.object(
            OIDCProvider,
            "metadata",
            return_value={"end_session_endpoint": "https://accounts.google.com/logout"},
            autospec=True,
        ):
            response = client.post("/api/auth/logout")

        assert response.json()["idp_logout_url"].startswith(
            "https://accounts.google.com/logout"
        )


async def test_an_unreachable_provider_still_logs_out_locally(
    temp_db_path: Path,
) -> None:
    """A discovery failure during logout must not keep the local session alive."""
    from rootcoz.auth.sso.base import AuthError
    from rootcoz.auth.sso.oidc import OIDCProvider

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        with patch.object(
            OIDCProvider,
            "metadata",
            side_effect=AuthError("discovery_failed", "boom"),
            autospec=True,
        ):
            response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["idp_logout_url"] is None
        assert client.get("/api/auth/me").status_code == 401


async def test_logout_without_a_public_base_url_stays_local(
    temp_db_path: Path,
) -> None:
    """No configured origin means no absolute redirect can be trusted."""
    from rootcoz.auth.sso.oidc import OIDCProvider

    env = {
        "SSO_GOOGLE_CLIENT_ID": "test-client-id",
        "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    }
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice")
        token = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        client.cookies.set("rootcoz_session", token)

        with patch.object(
            OIDCProvider,
            "metadata",
            return_value={"end_session_endpoint": "https://accounts.google.com/logout"},
            autospec=True,
        ):
            response = client.post("/api/auth/logout")

        assert response.json()["idp_logout_url"] is None
        assert client.get("/api/auth/me").status_code == 401

    assert await _last_logout_reason(temp_db_path) == "no_public_base_url"


def test_login_redirects_to_the_provider_with_a_sealed_state(
    temp_db_path: Path,
) -> None:
    """The start route hands the browser the provider URL and the state cookie."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            response = client.get(
                "/api/auth/sso/google/login?next=/report/1", follow_redirects=False
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("https://accounts.google.com/")
        sealed = response.cookies.get(sso_state.STATE_COOKIE)
        assert sealed
        assert (
            sso_state.open_attempt(sealed, provider="google").next_path == "/report/1"
        )


def test_login_without_public_base_url_fails_cleanly(temp_db_path: Path) -> None:
    """No registered redirect can be built, so the attempt must be refused."""
    env = {
        "SSO_GOOGLE_CLIENT_ID": "test-client-id",
        "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    }
    for client in make_app_client(temp_db_path, env):
        with patch.object(GoogleProvider, "authorization_url", autospec=True) as start:
            response = client.get("/api/auth/sso/google/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?sso_error=")
        assert start.call_count == 0


def test_an_unknown_provider_sends_the_browser_back_to_login(
    temp_db_path: Path,
) -> None:
    """Unknown and unconfigured answer the same way, so nobody can enumerate."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        start = client.get("/api/auth/sso/github/login", follow_redirects=False)
        assert start.status_code == 303
        assert start.headers["location"] == "/login?sso_error=unknown_provider"

        callback = client.get("/api/auth/sso/github/callback", follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"] == "/login?sso_error=unknown_provider"


def test_callback_refuses_a_state_that_does_not_match(temp_db_path: Path) -> None:
    """The CSRF nonce is compared, and a mismatch authenticates nobody."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, _state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=not-the-state",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_mismatch"


def test_a_non_ascii_state_is_a_mismatch_not_a_crash(temp_db_path: Path) -> None:
    """The state comes from the query string; latin-1 must not raise a 500."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, _state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=%C3%A9",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_mismatch"


def test_callback_refuses_a_missing_state_cookie(temp_db_path: Path) -> None:
    """A callback without our sealed cookie is refused, not trusted."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.get(
            "/api/auth/sso/google/callback?code=abc&state=whatever",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=state_invalid"


def test_a_failed_exchange_is_refused_and_creates_nothing(
    temp_db_path: Path,
) -> None:
    """A provider error must not create an account."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider,
            "exchange",
            side_effect=AuthError("exchange_failed", "boom"),
            autospec=True,
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=exchange_failed"
        assert response.cookies.get("rootcoz_session") is None


def test_an_idp_error_parameter_is_refused_without_exchanging(
    temp_db_path: Path,
) -> None:
    """A cancelled consent screen is a refusal, not an exception."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(GoogleProvider, "exchange", autospec=True) as exchange:
            response = client.get(
                f"/api/auth/sso/google/callback?error=access_denied&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=provider_error"
        assert exchange.call_count == 0


async def test_the_first_login_choice_is_audited(temp_db_path: Path) -> None:
    """A held-open ticket is an attempt, so the trail records it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"].startswith("auth.sso")
    ]
    assert [r["action"] for r in rows] == ["auth.sso_choice_required"]
    assert rows[0]["details"]["suggested_username"] == "alice"


async def test_create_and_link_are_audited_without_credentials(
    temp_db_path: Path,
) -> None:
    """The rows name the account and the provider — never a key or a ticket."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        created = await _ticket(client, subject="sub-create", suggested="alice")
        client.post(
            "/api/auth/sso/create", json={"ticket": created, "username": "alice"}
        )

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"] == "auth.sso_create"
    ]
    assert len(rows) == 1
    assert rows[0]["actor"] == "alice"
    assert rows[0]["details"] == {
        "provider": "google",
        "created": True,
        "status": "active",
    }
    assert created not in json.dumps(rows)


async def test_an_unknown_provider_refusal_is_audited(temp_db_path: Path) -> None:
    """A refusal reaches the trail even when no provider exists."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        client.get("/api/auth/sso/github/login", follow_redirects=False)

    rows = [
        r
        for r in await _audit_actions(temp_db_path)
        if r["action"].startswith("auth.sso")
    ]
    assert [(r["action"], r["outcome"]) for r in rows] == [
        ("auth.sso_login_failed", "denied")
    ]
    assert rows[0]["details"]["reason"] == "unknown_provider"


def _signed_id_token(
    *,
    issuer: str = GoogleProvider.issuer,
    audience: str = "test-client-id",
    expires_in: int = 300,
    nonce: str = "attempt-nonce",
) -> tuple[str, object]:
    """Build a real RS256 ID token plus the key set that verifies it.

    The token is genuinely signed, so the verification code under test runs for
    real — the point of these tests is that ``oidc.py``'s strongest claim
    ("verified before any claim is trusted") is executed, not assumed.

    Args:
        issuer: ``iss`` claim.
        audience: ``aud`` claim.
        expires_in: Seconds until ``exp``.
        nonce: ``nonce`` claim — the value the attempt sent.

    Returns:
        The compact JWS and a key set usable by ``joserfc``.
    """
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "sub-1",
        "email": "alice@example.com",
        "nonce": nonce,
        "exp": int(_time.time()) + expires_in,
        "iat": int(_time.time()),
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    return token, KeySet.import_key_set({"keys": [key.as_dict(private=False)]})


async def _verify(token: str, key_set: object, nonce: str = "attempt-nonce") -> dict:
    """Run ``_verify_id_token`` with the JWKS fetch stubbed out."""
    provider = GoogleProvider(
        client_id="test-client-id",
        client_secret="s",
        issuer=GoogleProvider.issuer,
    )
    with patch.object(GoogleProvider, "_jwks_for", return_value=key_set, autospec=True):
        return await provider._verify_id_token(
            {"jwks_uri": "https://accounts.google.com/jwks"}, token, nonce
        )


async def test_a_signed_id_token_from_the_issuer_is_accepted() -> None:
    """A token signed by the issuer's key, for this client, is trusted."""
    token, key_set = _signed_id_token()
    claims = await _verify(token, key_set)
    assert claims["sub"] == "sub-1"
    assert claims["email"] == "alice@example.com"


async def test_a_token_from_another_issuer_is_refused() -> None:
    """A well-signed token from a different issuer must not be trusted."""
    token, key_set = _signed_id_token(issuer="https://evil.example")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "issuer" in str(excinfo.value)


async def test_a_token_for_another_client_is_refused() -> None:
    """Someone else's token for the same issuer must not authenticate here."""
    token, key_set = _signed_id_token(audience="another-client")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "audience" in str(excinfo.value)


async def test_an_expired_token_is_refused() -> None:
    """Expiry is enforced by the same validation that checks the signature."""
    token, key_set = _signed_id_token(expires_in=-60)
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_for_another_attempt_is_refused() -> None:
    """A token minted for a different login must not complete this one."""
    token, key_set = _signed_id_token(nonce="the-other-attempt")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_without_exp_is_refused() -> None:
    """``exp`` is required, not merely checked when present."""
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {"iss": GoogleProvider.issuer, "aud": "test-client-id", "sub": "s"}
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    key_set = KeySet.import_key_set({"keys": [key.as_dict(private=False)]})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"


async def test_a_token_with_several_audiences_needs_azp() -> None:
    """A token minted for a sibling client of the same issuer is refused."""
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims = {
        "iss": GoogleProvider.issuer,
        "aud": ["test-client-id", "sibling-client"],
        "sub": "s",
        "nonce": "attempt-nonce",
        "exp": int(_time.time()) + 300,
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    key_set = KeySet.import_key_set({"keys": [key.as_dict(private=False)]})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert "another party" in str(excinfo.value)


async def test_a_token_signed_by_a_foreign_key_is_refused() -> None:
    """A valid shape with the wrong signing key is a forgery."""
    token, _key_set = _signed_id_token()
    _other_token, other_keys = _signed_id_token(issuer="https://elsewhere.example")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, other_keys)
    assert excinfo.value.reason == "id_token_invalid"


# -- Linking another provider to a signed-in account --------------------------


async def test_link_mode_attaches_the_identity_to_the_signed_in_account(
    temp_db_path: Path,
) -> None:
    """``mode=link`` never creates or claims an account: it attaches one."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            next_path="/settings", mode=sso_state.MODE_LINK, link_to="ruthn"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings?sso_linked=google"
        # The sealed attempt is single-use: a finished link clears it, while a
        # refused one keeps it so a transient provider error can be retried.
        assert response.cookies.get(sso_state.STATE_COOKIE) in (None, "")
        identities = await storage.list_user_identities("ruthn")
        assert [identity["provider"] for identity in identities] == ["google"]
        # The account it attached to is the one that was signed in.
        assert identities[0]["username"] == "ruthn"


async def test_link_mode_refuses_a_session_that_ended_mid_flow(
    temp_db_path: Path,
) -> None:
    """No session at the callback means nothing to link to — never a login."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        sealed, state = _sealed(mode=sso_state.MODE_LINK, link_to="ruthn")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=link_requires_session"
        assert await storage.list_user_identities("ruthn") == []


async def test_link_mode_refuses_an_identity_that_belongs_to_someone_else(
    temp_db_path: Path,
) -> None:
    """The answer must not confirm that an address already has an account."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.create_user("ruthn")
        await storage.add_user_identity(
            "alice", "google", "sub-1", email="alice@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.headers["location"] == "/settings?sso_error=identity_conflict"
        assert [
            i["provider"] for i in await storage.list_user_identities("ruthn")
        ] == []
        assert len(await storage.list_user_identities("alice")) == 1


async def test_a_refused_start_is_recorded_as_a_refusal(temp_db_path: Path) -> None:
    """A start that never started must not appear as a start.

    The start route is public and its action is volume-guarded; a refusal that
    reused the "started" action would both mislead the trail and escape the guard.
    """
    from rootcoz.auth.sso.base import AuthError

    for client in make_app_client(temp_db_path, _SSO_ENV):
        with patch.object(
            GoogleProvider,
            "authorization_url",
            side_effect=AuthError("login_start_failed", "boom"),
            autospec=True,
        ):
            response = client.get("/api/auth/sso/google/login", follow_redirects=False)
        assert response.status_code == 303

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT action, outcome FROM audit_events"
                " WHERE action LIKE 'auth.sso_login%' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        assert row["action"] == "auth.sso_login_failed"
        assert row["outcome"] == "denied"


async def test_a_signed_in_person_can_start_a_link(temp_db_path: Path) -> None:
    """The start route is public by shape, so the session must be read explicitly.

    Reading ``request.state.username`` there always yields "" — the middleware
    never resolves a public path — which would refuse every link, signed in or
    not.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            response = client.post(
                "/api/auth/sso/google/login?mode=link&next=/settings",
                headers=_SAME_ORIGIN,
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("https://accounts.google.com/")
        assert response.cookies.get(sso_state.STATE_COOKIE)

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT actor, details FROM audit_events"
                " WHERE action = 'auth.sso_login_start' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        # The actor is the caller, not "anonymous", and the mode is recorded.
        assert row["actor"] == "ruthn"
        assert json.loads(row["details"])["mode"] == "link"


async def test_a_read_only_provider_cannot_create_an_account(
    temp_db_path: Path,
) -> None:
    """`can_provision_user=False` is honoured where accounts are made."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        with patch.object(GoogleProvider, "can_provision_user", False):
            response = client.post(
                "/api/auth/sso/create",
                json={"ticket": ticket, "username": "ruthn"},
            )
        assert response.status_code == 403
        # Nothing was written, and the ticket is still usable for another try.
        assert await storage.get_user_by_username("ruthn") is None


async def test_a_viewer_cannot_start_a_link(temp_db_path: Path) -> None:
    """Adding a method is the mirror of removing one, which needs a reviewer."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("vicky")
        session = await storage.create_session("vicky", is_admin=False, role="viewer")
        client.cookies.set("rootcoz_session", session)

        response = client.post(
            "/api/auth/sso/google/login?mode=link",
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == ("/login?sso_error=link_forbidden_role")


async def test_link_mode_needs_a_session_before_it_starts(temp_db_path: Path) -> None:
    """Starting a link anonymously is refused rather than turned into a login."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.post(
            "/api/auth/sso/google/login?mode=link",
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=link_requires_session"


async def test_link_mode_must_be_started_with_a_post(temp_db_path: Path) -> None:
    """A bare GET cannot start a link: a cross-site navigation is not consent.

    Nothing is minted and nothing is sealed — the refusal happens before the
    flow exists, so there is no attempt a callback could finish.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        with patch.object(
            GoogleProvider, "authorization_url", autospec=True
        ) as authorization_url:
            response = client.get(
                "/api/auth/sso/google/login?mode=link&next=/settings",
                follow_redirects=False,
            )

        assert authorization_url.await_count == 0
        assert response.status_code == 303
        assert response.headers["location"] == "/login?sso_error=link_requires_post"
        assert not response.cookies.get(sso_state.STATE_COOKIE)
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "link_requires_post"
        # The row names the account whose cookie rode along.
        assert refusal["actor"] == "ruthn"


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        # A sibling subdomain is another origin, and this is a write.
        {"sec-fetch-site": "same-site"},
        {"origin": "https://evil.example"},
        {"referer": "https://evil.example/settings"},
        {"origin": "null"},
        # Nothing proves the origin, so it is treated as cross-site.
        {},
    ],
)
async def test_a_link_cannot_be_started_cross_site(
    temp_db_path: Path, headers: dict[str, str]
) -> None:
    """A signed-in browser is not consent: the POST must prove its origin."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        with patch.object(
            GoogleProvider, "authorization_url", autospec=True
        ) as authorization_url:
            response = client.post(
                "/api/auth/sso/google/login?mode=link&next=/settings",
                headers=headers,
                follow_redirects=False,
            )

        assert authorization_url.await_count == 0
        assert response.status_code == 303
        # The person is still signed in, so the reason renders on the settings
        # page rather than on /login, which bounces them away.
        assert response.headers["location"] == "/settings?sso_error=link_cross_site"
        assert not response.cookies.get(sso_state.STATE_COOKIE)
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "link_cross_site"
        assert refusal["actor"] == "ruthn"


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://rootcoz.example"},
        {"referer": "https://rootcoz.example/settings"},
    ],
)
async def test_the_origin_header_also_proves_a_link_is_same_origin(
    temp_db_path: Path, headers: dict[str, str]
) -> None:
    """Clients that send no Sec-Fetch-Site are covered by Origin/Referer."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            response = client.post(
                "/api/auth/sso/google/login?mode=link&next=/settings",
                headers=headers,
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("https://accounts.google.com/")
        assert response.cookies.get(sso_state.STATE_COOKIE)


async def test_an_ordinary_login_needs_neither_a_post_nor_an_origin(
    temp_db_path: Path,
) -> None:
    """The same-origin rule is the link's, not the login's: sign-in stays a GET."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            response = client.get(
                "/api/auth/sso/google/login?next=/dashboard", follow_redirects=False
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("https://accounts.google.com/")
        assert response.cookies.get(sso_state.STATE_COOKIE)


async def test_an_unknown_mode_is_refused(temp_db_path: Path) -> None:
    """The mode is sealed, so it is also validated before anything is minted."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.get(
            "/api/auth/sso/google/login?mode=attach", follow_redirects=False
        )
        assert response.headers["location"] == "/login?sso_error=unknown_mode"


async def test_a_link_refusal_before_the_callback_lands_on_the_destination(
    temp_db_path: Path,
) -> None:
    """A domain rejection happens in resolve(), before _sso_finish_link runs.

    The person is still signed in, so /login would bounce them away before they
    could read the reason.
    """
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.headers["location"] == "/settings?sso_error=domain_not_allowed"
        assert await storage.list_user_identities("ruthn") == []


async def test_a_role_demoted_mid_flow_cannot_finish_a_link(
    temp_db_path: Path,
) -> None:
    """The role is re-read with the session: ten minutes is long enough to change."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        # Demoted while the IdP round-trip was in progress.
        async with storage._connect_db() as db:
            await db.execute("UPDATE sessions SET role = 'viewer' WHERE 1")
            await db.commit()

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.headers["location"] == "/settings?sso_error=link_forbidden_role"
        assert await storage.list_user_identities("ruthn") == []


@pytest.mark.parametrize(
    ("user_status", "disabled", "reason"),
    [
        # ``disabled`` is applied as the row change alone: disabling through
        # storage also drops the account's sessions, and the surviving session is
        # exactly the case this gate must not depend on the middleware for.
        ("active", True, "account_disabled"),
        ("pending", False, "account_pending"),
        ("rejected", False, "account_rejected"),
    ],
)
async def test_a_blocked_account_cannot_finish_a_link(
    temp_db_path: Path, user_status: str, disabled: bool, reason: str
) -> None:
    """The public SSO routes skip the middleware's gate, so it is applied here.

    The state is re-read at the finish, never taken from the start: ten minutes
    and an admin action can sit in between.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        # The status changed while the IdP round-trip was in progress.
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET status = ?, disabled = ? WHERE username = ?",
                (user_status, 1 if disabled else 0, "ruthn"),
            )
            await db.commit()
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        # The account state never reaches the URL; the audit row carries it.
        assert response.headers["location"] == "/settings?sso_error=denied"
        assert await storage.list_user_identities("ruthn") == []
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == reason
        assert refusal["actor"] == "ruthn"


async def test_a_link_cannot_be_finished_into_another_account(
    temp_db_path: Path,
) -> None:
    """The cookie binds the attempt to a browser — not to an account.

    A flow started by one person and finished with somebody else's session would
    attach the first person's provider to the second person's account.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.create_user("bob")
        bob_session = await storage.create_session(
            "bob", is_admin=False, role="reviewer"
        )
        client.cookies.set("rootcoz_session", bob_session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="alice", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert (
            response.headers["location"] == "/settings?sso_error=link_session_changed"
        )
        assert await storage.list_user_identities("bob") == []
        assert await storage.list_user_identities("alice") == []


async def test_an_expired_session_cannot_start_or_finish_a_link(
    temp_db_path: Path,
) -> None:
    """A dead credential names its owner — it must not prove they are there."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "github", "gh-a")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE sessions SET expires_at = '2000-01-01 00:00:00' WHERE 1"
            )
            await db.commit()
        client.cookies.set("rootcoz_session", session)

        started = client.post(
            "/api/auth/sso/google/login?mode=link",
            headers=_SAME_ORIGIN,
            follow_redirects=False,
        )
        assert started.headers["location"] == "/login?sso_error=link_requires_session"

        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="alice", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )
        assert response.headers["location"] == "/login?sso_error=link_requires_session"
        assert [i["provider"] for i in await storage.list_user_identities("alice")] == [
            "github"
        ]


async def test_an_internal_link_reason_never_reaches_the_url(
    temp_db_path: Path,
) -> None:
    """Only coarse public codes travel in a redirect; internal ones map to denied."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        # The account disappears between the callback and the write.
        with (
            patch.object(
                GoogleProvider, "exchange", return_value=_claims(), autospec=True
            ),
            patch.object(
                storage,
                "add_user_identity",
                side_effect=ValueError("User 'ruthn' not found"),
                autospec=True,
            ),
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        location = response.headers["location"]
        assert "account_missing" not in location
        assert location == "/settings?sso_error=denied"


async def test_linking_the_same_identity_twice_is_a_no_op(temp_db_path: Path) -> None:
    """A double click must not fail, and must not duplicate the row."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            next_path="/settings", mode=sso_state.MODE_LINK, link_to="ruthn"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.headers["location"] == "/settings?sso_linked=google"
        assert len(await storage.list_user_identities("ruthn")) == 1


# -- Sign-in methods API ------------------------------------------------------


async def test_listing_and_removing_linked_identities(temp_db_path: Path) -> None:
    """Self-service listing shows providers, and removal is one provider wide."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        await storage.add_user_identity(
            "ruthn", "github", "gh-1", email="ruthn@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        listed = client.get("/api/user/identities")
        assert listed.status_code == 200
        body = listed.json()
        assert [i["provider"] for i in body["identities"]] == ["github", "google"]
        assert body["unlinkable"] is True

        removed = client.delete("/api/user/identities/github")
        assert removed.status_code == 200
        assert [i["provider"] for i in await storage.list_user_identities("ruthn")] == [
            "google"
        ]
        # With one left, the client can say so before the person clicks.
        assert client.get("/api/user/identities").json()["unlinkable"] is False


async def test_the_last_sign_in_method_cannot_be_removed(temp_db_path: Path) -> None:
    """An SSO-created account never saw its API key, so this would lock it out."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        response = client.delete("/api/user/identities/google")
        assert response.status_code == 409
        assert "only way to sign in" in response.json()["detail"]
        assert len(await storage.list_user_identities("ruthn")) == 1


async def test_a_viewer_cannot_unlink_a_sign_in_method(temp_db_path: Path) -> None:
    """Removing one is a write on the account, like removing your own AI key."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("vicky")
        await storage.add_user_identity("vicky", "google", "sub-v")
        await storage.add_user_identity("vicky", "github", "gh-v")
        session = await storage.create_session("vicky", is_admin=False, role="viewer")
        client.cookies.set("rootcoz_session", session)

        response = client.delete("/api/user/identities/github")
        assert response.status_code == 403
        assert len(await storage.list_user_identities("vicky")) == 2


async def test_removing_an_unconnected_provider_is_a_404(temp_db_path: Path) -> None:
    """A provider the account never linked is not silently "removed"."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        assert client.delete("/api/user/identities/github").status_code == 404


async def test_an_unknown_provider_name_is_a_client_error(
    temp_db_path: Path,
) -> None:
    """A name outside SSO_PROVIDERS is "not connected", never a 500."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-x")
        await storage.add_user_identity("ruthn", "github", "gh-x")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        assert client.delete("/api/user/identities/bogus").status_code == 404
        login = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert login.status_code == 200
        assert (
            client.delete("/api/admin/users/ruthn/identities/bogus").status_code == 404
        )
        # Nothing was removed by either refusal.
        assert len(await storage.list_user_identities("ruthn")) == 2


async def test_an_admin_can_inspect_and_unlink_any_account(temp_db_path: Path) -> None:
    """Admins get the same shape, scoped by username, with an audit row each."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-9")
        await storage.add_user_identity("alice", "github", "gh-9")
        login = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert login.status_code == 200

        listed = client.get("/api/admin/users/alice/identities")
        assert listed.status_code == 200
        assert len(listed.json()["identities"]) == 2

        removed = client.delete("/api/admin/users/alice/identities/google")
        assert removed.status_code == 200
        assert [i["provider"] for i in await storage.list_user_identities("alice")] == [
            "github"
        ]

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT actor FROM audit_events WHERE action = 'admin.identity_unlinked'"
            )
            assert [row["actor"] for row in await cursor.fetchall()] == ["admin"]


async def test_an_admin_cannot_remove_the_last_sign_in_method_either(
    temp_db_path: Path,
) -> None:
    """Disabling is the access cut; removing the only way in is a lockout."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "google", "sub-9")
        login = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert login.status_code == 200

        response = client.delete("/api/admin/users/alice/identities/google")
        assert response.status_code == 409
        assert len(await storage.list_user_identities("alice")) == 1


# -- CLI device flow ----------------------------------------------------------


def _start_device(client, **body: object) -> dict:
    """Start a device flow and return the server's answer."""
    response = client.post("/api/auth/sso/device/start", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_device_start_needs_a_configured_provider(temp_db_path: Path) -> None:
    """With no IdP configured there is nothing a code could be approved against."""
    for client in make_app_client(temp_db_path):
        response = client.post("/api/auth/sso/device/start", json={})
        assert response.status_code == 404


def test_device_start_refuses_an_unknown_or_oversized_input(temp_db_path: Path) -> None:
    """The provider must be configured, and the label a human reads is bounded."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        assert (
            client.post(
                "/api/auth/sso/device/start", json={"provider": "nope"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/auth/sso/device/start", json={"client_name": "x" * 201}
            ).status_code
            == 400
        )


async def test_a_client_name_cannot_forge_a_log_line(temp_db_path: Path) -> None:
    """The label is anonymous input: a newline in it must not split the log."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        started = client.post(
            "/api/auth/sso/device/start",
            json={"client_name": "laptop\n[AUDIT] forged entry\twith tabs"},
        )
        assert started.status_code == 200
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT details FROM audit_events"
                " WHERE action = 'auth.sso_device_started' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        stored = json.loads(row["details"])["client_name"]
        assert "\n" not in stored and "\t" not in stored
        # The control characters became spaces: the label survives as one line.
        assert stored == "laptop [AUDIT] forged entry with tabs"


async def test_one_caller_cannot_reserve_every_device_code(temp_db_path: Path) -> None:
    """The ceiling is per caller as well as global: anonymous means anyone."""
    from rootcoz import main as app_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(app_module, "_MAX_LIVE_DEVICE_CODES_PER_CALLER", 2)
    try:
        for client in make_app_client(temp_db_path, _SSO_ENV):
            assert client.post("/api/auth/sso/device/start", json={}).status_code == 200
            assert client.post("/api/auth/sso/device/start", json={}).status_code == 200
            # The global ceiling is far away; the caller's own is reached.
            assert client.post("/api/auth/sso/device/start", json={}).status_code == 429
            assert await storage.count_live_device_codes() == 2
    finally:
        monkeypatch.undo()


async def test_a_failed_session_leaves_the_device_code_redeemable(
    temp_db_path: Path,
) -> None:
    """A code is spent only once the terminal actually has a credential."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        client.cookies.clear()

        # The test client re-raises a server error, which is the failure the
        # terminal would see as a 500.
        with (
            patch.object(
                storage, "create_session", side_effect=RuntimeError("storage down")
            ),
            pytest.raises(RuntimeError),
        ):
            client.post(
                "/api/auth/sso/device/poll",
                json={"device_code": started["device_code"]},
            )

        # The code survived the failure, so the same terminal can retry — after
        # the advertised interval, which the poll throttle enforces.
        assert await storage.get_device_code(started["device_code"]) is not None
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE sso_device_codes SET last_polled_at = '2000-01-01 00:00:00'"
            )
            await db.commit()
        retried = client.post(
            "/api/auth/sso/device/poll",
            json={"device_code": started["device_code"]},
        )
        assert retried.json()["status"] == "approved"
        assert retried.json()["token"]


async def test_a_flood_of_pending_codes_is_refused_before_the_insert(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start route is anonymous: its table needs a cap, not just a log budget."""
    from rootcoz import main as app_module

    monkeypatch.setattr(app_module, "_MAX_LIVE_DEVICE_CODES", 2)
    for client in make_app_client(temp_db_path, _SSO_ENV):
        assert client.post("/api/auth/sso/device/start", json={}).status_code == 200
        assert client.post("/api/auth/sso/device/start", json={}).status_code == 200
        refused = client.post("/api/auth/sso/device/start", json={})
        assert refused.status_code == 429
        # The refusal costs no row: the cap is checked before the insert.
        assert await storage.count_live_device_codes() == 2


async def test_the_approval_url_is_a_path_when_no_origin_is_configured(
    temp_db_path: Path,
) -> None:
    """No PUBLIC_BASE_URL means no absolute URL can be trusted, so none is built."""
    env = {
        "SSO_GOOGLE_CLIENT_ID": "test-client-id",
        "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    }
    for client in make_app_client(temp_db_path, env):
        started = _start_device(client, provider="google", client_name="laptop")
        assert started["verification_uri"] == "/login/device"
        assert started["interval"] == 5
        assert started["expires_in"] == 600


async def test_device_poll_is_pending_and_then_throttled(temp_db_path: Path) -> None:
    """An unapproved code answers 'pending', and a too-fast poll is refused."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        started = _start_device(client, provider="google")
        first = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        assert first.json()["status"] == "pending"
        second = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        # Inside the advertised interval: the server refuses rather than serves.
        assert second.json()["status"] == "slow_down"


async def test_an_unknown_device_code_reports_expiry(temp_db_path: Path) -> None:
    """A code the server never issued is 'expired', not an error page."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        response = client.post(
            "/api/auth/sso/device/poll", json={"device_code": "never-issued"}
        )
        assert response.json() == {"status": "expired"}


async def test_the_approved_code_hands_out_one_working_session(
    temp_db_path: Path,
) -> None:
    """Approval names the account; redemption mints a session, exactly once."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        started = _start_device(client, client_name="laptop")

        client.cookies.set("rootcoz_session", session)
        info = client.post(
            "/api/auth/sso/device/approve/lookup",
            json={"user_code": started["user_code"]},
        )
        assert info.json()["client_name"] == "laptop"
        approved = client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        assert approved.status_code == 200
        assert approved.json()["provider"] == "google"
        client.cookies.clear()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        body = polled.json()
        assert body["status"] == "approved"
        assert body["username"] == "alice"
        # The token is a session, so it names its issuer and expires on its own.
        assert await storage.get_session(body["token"]) is not None
        assert (await storage.get_session_identity(body["token"]))[
            "auth_provider"
        ] == "google"

        # Redemption consumed the code: the same device code yields nothing more.
        again = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        assert again.json()["status"] == "expired"


async def test_the_token_from_the_device_flow_works_as_a_bearer(
    temp_db_path: Path,
) -> None:
    """The CLI's existing credential slot accepts it without new machinery."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        client.cookies.clear()
        token = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        ).json()["token"]

        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["username"] == "alice"


async def test_an_account_that_became_unusable_is_denied_at_redemption(
    temp_db_path: Path,
) -> None:
    """Approval is minutes older than redemption; the account's state is not."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        client.cookies.clear()
        async with storage._connect_db() as db:
            await db.execute("UPDATE users SET disabled = 1 WHERE username = 'alice'")
            await db.commit()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        assert polled.json()["status"] == "denied"


async def test_an_approval_cannot_stamp_a_provider_the_code_was_not_opened_for(
    temp_db_path: Path,
) -> None:
    """A flow pinned to one IdP cannot be approved with another person's issuer."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="github"
        )
        started = _start_device(client, provider="google")
        client.cookies.set("rootcoz_session", session)

        response = client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        assert response.status_code == 400


async def test_an_unknown_user_code_is_not_approvable(temp_db_path: Path) -> None:
    """A mistyped code answers like an expired one, and never as a 500."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        response = client.post(
            "/api/auth/sso/device/approve", json={"user_code": "ZZZZ-ZZZZ"}
        )
        assert response.status_code == 404


async def test_the_device_flow_is_audited_where_it_changes_anything(
    temp_db_path: Path,
) -> None:
    """Start, approve and redeem each leave one row naming the actor."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        started = _start_device(client, client_name="laptop")
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve", json={"user_code": started["user_code"]}
        )
        client.cookies.clear()
        client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT action, actor, details FROM audit_events"
                " WHERE action LIKE 'auth.sso_device%' ORDER BY id"
            )
            rows = [tuple(row) for row in await cursor.fetchall()]
        assert [(action, actor) for action, actor, _ in rows] == [
            ("auth.sso_device_started", "anonymous"),
            ("auth.sso_device_approved", "alice"),
            ("auth.sso_device_redeemed", "alice"),
        ]
        # The user code is a credential a human types: the trail records who and
        # which client, never the code itself (#379).
        for action, _actor, details in rows:
            assert "user_code" not in json.loads(details or "{}"), action


async def test_oidc_discovery_must_claim_the_configured_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery document is trusted for an hour: pin what it claims."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._METADATA_CACHE.clear()
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    monkeypatch.setattr(
        oidc_module,
        "request_json",
        _returning(
            {
                "issuer": "https://evil.example",
                "authorization_endpoint": "https://idp.example/auth",
                "token_endpoint": "https://idp.example/token",
                "jwks_uri": "https://idp.example/jwks",
            }
        ),
    )
    with pytest.raises(AuthError) as excinfo:
        await provider.metadata()
    assert excinfo.value.reason == "discovery_failed"


async def test_oidc_discovery_rejects_a_plaintext_userinfo_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The userinfo URL receives the access token on the no-id_token fallback."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._METADATA_CACHE.clear()
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    monkeypatch.setattr(
        oidc_module,
        "request_json",
        _returning(
            {
                "issuer": "https://idp.example",
                "authorization_endpoint": "https://idp.example/auth",
                "token_endpoint": "https://idp.example/token",
                "jwks_uri": "https://idp.example/jwks",
                "userinfo_endpoint": "http://third-party.example/userinfo",
            }
        ),
    )
    with pytest.raises(AuthError) as excinfo:
        await provider.metadata()
    assert excinfo.value.reason == "discovery_failed"


async def test_oidc_discovery_rejects_a_plaintext_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoints we call must be https; loopback http is the documented exception."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._METADATA_CACHE.clear()
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    monkeypatch.setattr(
        oidc_module,
        "request_json",
        _returning(
            {
                "issuer": "https://idp.example",
                "authorization_endpoint": "https://idp.example/auth",
                "token_endpoint": "http://idp.example/token",
                "jwks_uri": "https://idp.example/jwks",
            }
        ),
    )
    with pytest.raises(AuthError) as excinfo:
        await provider.metadata()
    assert excinfo.value.reason == "discovery_failed"


def test_an_azp_naming_another_client_is_refused_even_with_one_audience() -> None:
    """A token minted for a sibling client carries its azp; aud alone is not enough."""
    import asyncio
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey

    from rootcoz.auth.sso.oidc import OIDCProvider

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})

    class _Provider(OIDCProvider):
        name = "testoidc"
        display_name = "Test"

        @classmethod
        def from_settings(cls, settings):
            return None

        async def _jwks_for(self, jwks_uri: str, *, force_refresh: bool = False):
            from joserfc.jwk import KeySet

            return KeySet.import_key_set({"keys": [key.as_dict(private=False)]})

    provider = _Provider(
        client_id="our-client",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    claims = {
        "iss": "https://idp.example",
        "aud": ["our-client"],
        "azp": "another-client",
        "sub": "s",
        "nonce": "n1",
        "exp": int(_time.time()) + 300,
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)

    with pytest.raises(AuthError) as excinfo:
        asyncio.run(
            provider._verify_id_token({"jwks_uri": "https://idp/jwks"}, token, "n1")
        )
    assert "another party" in str(excinfo.value)


def test_the_provider_names_are_declared_once() -> None:
    """One list, asserted at import: adding a provider is a class, not two edits."""
    from rootcoz import storage
    from rootcoz.auth.sso import PROVIDER_BY_NAME
    from rootcoz.sso_providers import SSO_PROVIDERS

    assert storage.SSO_PROVIDERS == SSO_PROVIDERS
    assert set(PROVIDER_BY_NAME) == set(SSO_PROVIDERS)


async def test_a_spent_ticket_leaves_no_account_behind(temp_db_path: Path) -> None:
    """The ticket is spent in the account's own transaction (#Qodo round 3)."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        # A second browser consumed it first — the classic expiry race.
        assert await storage.consume_sso_ticket(ticket) is not None

        response = client.post(
            "/api/auth/sso/create", json={"ticket": ticket, "username": "ruthn"}
        )

        assert response.status_code in (404, 409)
        # Nothing durable: no account, no undisclosed key.
        assert await storage.get_user_by_username("ruthn") is None
