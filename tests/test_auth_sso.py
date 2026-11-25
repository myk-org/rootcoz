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
import hashlib
import inspect
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from rootcoz import storage
from rootcoz.auth import identity as auth_identity
from rootcoz.auth.sso import AuthError, ClaimSet, configured_providers, get_provider
from rootcoz.auth.sso import state as sso_state
from rootcoz.auth.sso.google import GoogleProvider
from rootcoz.auth.sso.oidc import OIDCProvider
from rootcoz.auth.sso.redhat import RedHatSSOProvider
from rootcoz.config import (
    clear_settings_cache,
    get_settings,
    normalize_domain_list,
    update_db_settings_cache,
)
from rootcoz.encryption import encrypt_value
from tests.conftest import admin_login, build_test_env, make_app_client
from tests.test_auth import _assert_spa_page

_SSO_ENV = {
    "SSO_GOOGLE_CLIENT_ID": "test-client-id",
    "SSO_GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
    "PUBLIC_BASE_URL": "https://rootcoz.example",
}

#: A second configured provider, so a mismatch has somewhere to come from.
_GITHUB_ENV = {
    "SSO_GITHUB_CLIENT_ID": "test-github-client-id",
    "SSO_GITHUB_CLIENT_SECRET": "test-github-client-secret",  # pragma: allowlist secret
}

#: What the settings page's own POST carries — the proof a link start needs.
_SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

#: A trusted-proxy deployment whose write allow-list names somebody else, so a
#: name under test is provably outside it.
_PROXY_ALLOW_LIST_ENV = {"TRUST_PROXY_HEADERS": "true", "ALLOWED_USERS": "someone-else"}

#: OpenShift configured (it needs both of the cluster's origins), so a ticket it
#: issued is still redeemable: redemption re-resolves the provider against the
#: current settings, which is what stops a removed provider's tickets working.
_OPENSHIFT_ENV = {
    "SSO_OPENSHIFT_CLIENT_ID": "test-openshift-client-id",
    "SSO_OPENSHIFT_CLIENT_SECRET": "test-openshift-client-secret",  # pragma: allowlist secret
    "SSO_OPENSHIFT_ISSUER": "https://oauth-openshift.apps.example.com",
    "SSO_OPENSHIFT_API_URL": "https://api.example.com:6443",
    "PUBLIC_BASE_URL": "https://rootcoz.example",
}


def _claims(
    subject: str = "sub-1",
    username_hint: str = "alice",
    email: str | None = "alice@example.com",
    *,
    email_is_authoritative: bool = False,
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
        email_is_authoritative=email_is_authoritative,
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
    attached too — the ticket alone is deliberately not enough. The binding is
    random and independent of the ticket, so it is kept on the client's cookie
    jar rather than recomputed from the ticket.
    """
    binding = sso_state.new_ticket_binding()
    ticket = await storage.create_sso_ticket(
        provider=provider,
        subject=subject,
        email=email,
        suggested_username=suggested,
        binding=binding,
    )
    if client is not None:
        client.cookies.set(sso_state.TICKET_COOKIE, binding)
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


@pytest.mark.parametrize(
    "raw",
    [
        "/%09/evil",
        "/\t/evil",
        "//evil",
        "\\\\evil",
        "/a\tb",
        "/report/\x7f1",
        "/a\nb",
        "/a\rb",
        "/report/1\n",
        "\n",
        "\r",
    ],
)
def test_a_destination_carrying_a_control_character_is_refused(raw: str) -> None:
    """A tab is dropped by the browser's URL parser: a path becomes ``//host``.

    The first case is the query-string form and the second its decoded form —
    the route reads ``next`` through ``request.query_params``, so ``/%09/evil``
    is what the guard is handed. Qodo found that tab surviving into the cookie,
    the ticket and the JSON ``next``, leaving only the sinks to stop the jump.
    """
    assert sso_state.safe_next(unquote(raw)) == "/"


@pytest.mark.parametrize("path", ["/report/1?x=1", "/settings", "/"])
def test_an_ordinary_destination_survives_the_control_character_rule(
    path: str,
) -> None:
    """The refusal is aimed at control characters, not at deep links."""
    assert sso_state.safe_next(path) == path


async def test_a_tabbed_destination_never_reaches_a_sink(temp_db_path: Path) -> None:
    """The end-to-end case Qodo described, guarded at the one point they share.

    ``?next=/%09/evil`` arrives decoded as ``/\t/evil``; a browser dropping that
    tab would read it as the scheme-relative ``//evil``. The sealed attempt must
    already say ``/``, so the 303, the cookie, the ticket and ``navigate()`` all
    get the same safe value instead of one that only the sink happens to stop.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        # A returning identity, so the callback signs in and redirects straight
        # to the destination instead of minting a first-login ticket.
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")

        with patch.object(
            GoogleProvider,
            "authorization_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
            autospec=True,
        ):
            started = client.get(
                "/api/auth/sso/google/login?next=/%09/evil", follow_redirects=False
            )

        assert started.status_code == 303
        sealed = started.cookies.get(sso_state.STATE_COOKIE)
        assert sealed
        attempt = sso_state.open_attempt(sealed, provider="google")
        assert attempt.next_path == "/"

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={attempt.state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        # The real header: ``//evil`` is what the tab would have produced.
        assert response.headers["location"] == "/"


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


def test_only_openshift_declares_an_authoritative_address() -> None:
    """The allow-list exception is declared by the provider it is for."""
    from rootcoz.auth.sso import PROVIDER_BY_NAME

    assert {
        name
        for name, cls in PROVIDER_BY_NAME.items()
        if getattr(cls, "email_is_authoritative", False)
    } == {"openshift"}


async def test_an_authoritative_address_is_admitted_without_a_verification_claim(
    temp_db_path: Path,
) -> None:
    """OpenShift writes the address through the cluster admin: the domain counts."""
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
            email_is_authoritative=True,
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
            email_is_authoritative=True,
        )
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("openshift", wrong, get_settings())
        assert excinfo.value.reason == "domain_not_allowed"


async def test_a_provider_with_neither_verification_nor_an_authoritative_address_is_refused(
    temp_db_path: Path,
) -> None:
    """Publishing no `email_verified` claim is not itself a pass."""
    from rootcoz.auth.sso.base import ClaimSet as BaseClaimSet

    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        silent = BaseClaimSet(
            subject="silent-1",
            username_hint="alice",
            email="alice@redhat.com",
            claims={"sub": "silent-1", "email": "alice@redhat.com"},
            asserts_email_verified=False,
        )
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve("openshift", silent, get_settings())
        assert excinfo.value.reason == "email_not_verified"


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
    # The domain allow-list needs this: the cluster never sends `email_verified`,
    # but the address is written by the cluster admin.
    assert claims.email_is_authoritative is True
    assert httpx  # the transport itself is the proof the helper ran

    token_request = next(r for r in seen if r.url.path == "/oauth/token")
    assert token_request.url.host == "oauth-openshift.apps.example.com"
    user_request = next(r for r in seen if r.url.path.endswith("/users/~"))
    assert user_request.url.host == "api.example.com"
    assert user_request.url.port == 6443
    assert user_request.headers["authorization"] == "Bearer sha256~token"
    identity_request = next(r for r in seen if "/identities/" in r.url.path)
    assert identity_request.url.host == "api.example.com"


async def test_an_identity_name_carrying_url_syntax_is_encoded_as_one_segment() -> None:
    """A name with '#', '?' or '/' must not change the shape of the URL.

    Unencoded, the request would be cut at the '#' and the cluster would answer 404
    for a name it never received — a real account that simply cannot sign in.
    """
    name = "oidc:b#o?b/x"
    user = {"metadata": {"uid": "u-2", "name": "bob"}, "identities": [name]}
    transport, seen = _openshift_transport(
        user, {name: {"extra": {"email": ["bob@example.com"]}}}
    )
    with _github_client(transport):
        claims = await _openshift_provider().exchange(
            code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
        )

    assert claims.email == "bob@example.com"
    identity_request = next(r for r in seen if "/identities/" in r.url.path)
    # The escape is what keeps the name one segment: httpx re-encodes on the wire.
    assert b"%23" in identity_request.url.raw_path
    assert b"%3F" in identity_request.url.raw_path
    assert b"%2F" in identity_request.url.raw_path
    assert identity_request.url.raw_path.endswith(b"oidc%3Ab%23o%3Fb%2Fx")


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
    for client in make_app_client(temp_db_path, _OPENSHIFT_ENV):
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


#: A Keycloak deployment: the realm *is* the issuer, and everything — discovery,
#: token, keys — hangs off it.
_REDHAT_REALM = "https://sso.redhat.com/auth/realms/redhat-external"

_REDHAT_ENV = {
    "SSO_REDHAT_ISSUER": _REDHAT_REALM,
    "SSO_REDHAT_CLIENT_ID": "redhat-client",
    "SSO_REDHAT_CLIENT_SECRET": "redhat-secret",  # pragma: allowlist secret
    "PUBLIC_BASE_URL": "https://rootcoz.example",
}


async def test_a_red_hat_login_runs_end_to_end_against_its_realm(
    temp_db_path: Path,
) -> None:
    """Red Hat's whole configuration is the realm issuer, so pin the flow.

    It was covered only by its name and its issuer, which means a regression in
    its discovery path — Keycloak serves it *under* the realm — would reach a
    cluster before any test noticed.
    """
    import httpx
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    now = int(time.time())
    id_token = jose_jwt.encode(
        {"alg": "RS256", "kid": "k1"},
        {
            "iss": _REDHAT_REALM,
            "aud": "redhat-client",
            "sub": "rh-1",
            "email": "alice@redhat.com",
            "preferred_username": "alice",
            "nonce": "n-1",
            "exp": now + 300,
            "iat": now,
        },
        key,
    )
    jwks = KeySet.import_key_set({"keys": [key.as_dict(private=False)]}).as_dict(
        private=False
    )
    document = {
        "issuer": _REDHAT_REALM,
        "authorization_endpoint": f"{_REDHAT_REALM}/protocol/openid-connect/auth",
        "token_endpoint": f"{_REDHAT_REALM}/protocol/openid-connect/token",
        "jwks_uri": f"{_REDHAT_REALM}/protocol/openid-connect/certs",
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=document)
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"id_token": id_token})
        if path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)

    for _client in make_app_client(temp_db_path, _REDHAT_ENV):
        with _github_client(transport):
            claims = await get_provider("redhat", get_settings()).exchange(
                code="c",
                code_verifier="v",
                redirect_uri="https://rootcoz.example/api/auth/sso/redhat/callback",
                nonce="n-1",
            )

        assert claims.subject == "rh-1"
        assert claims.email == "alice@redhat.com"
        choice = await auth_identity.resolve("redhat", claims, get_settings())
        assert isinstance(choice, auth_identity.LoginChoice)

    # Keycloak's shape, not Google's: discovery lives under the realm.
    discovered = next(
        r for r in seen if r.url.path.endswith("/.well-known/openid-configuration")
    )
    assert discovered.url.path == (
        "/auth/realms/redhat-external/.well-known/openid-configuration"
    )
    token_request = next(
        r for r in seen if r.url.path.endswith("/protocol/openid-connect/token")
    )
    assert token_request.url.host == "sso.redhat.com"
    oidc_module._reset_discovery_cache()


def test_a_non_ascii_state_cookie_is_refused_as_state_invalid() -> None:
    """A malformed cookie is a refusal, never an exception out of the flow.

    The check is explicit rather than left to the ASCII encode: a cookie is
    decoded from the header, so a non-ASCII value is bad input, and the refusal
    has to be StateError (state_invalid downstream) whatever Python raises.
    """
    from rootcoz.auth.sso.state import StateError, _unpack

    for value in ("héllo", "中文", "abc\u00ff"):
        with pytest.raises(StateError) as excinfo:
            _unpack(value)
        assert "unreadable" in str(excinfo.value)


async def test_a_signed_out_visitor_keeps_the_device_approval_destination(
    temp_db_path: Path,
) -> None:
    """The CLI prints /login/device; it must answer with the shell, not a 303.

    A redirect to bare /login throws the path away, so the person is returned to /
    after signing in instead of the code-entry screen they were sent to.
    """
    for client in make_app_client(temp_db_path, {}):
        response = client.get(
            "/login/device",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code != 303, response.headers.get("location")
        _assert_spa_page(response)
        # The APIs the page calls stay authenticated.
        assert client.get("/api/auth/sso/device/approve/lookup").status_code == 401


async def test_an_orphaned_identity_is_healed_so_it_cannot_block_relinking(
    temp_db_path: Path,
) -> None:
    """An identity whose account is gone must not hold its unique slot forever.

    ``get_user_identity`` inner-joins ``users``, so the orphan is invisible to the
    resolver — but the row still owns ``(provider, subject)``, and the create/link
    path would then refuse the very person whose identity it is.
    """
    for _client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user_with_identity(
            "alice",
            provider="google",
            subject="sub-1",
            email="alice@example.com",
            status="active",
            role="reviewer",
            created_via="google",
        )
        # A row that outlived its account: the user goes, the identity stays (a
        # crashed delete, or one from before delete_user cleaned identities up).
        async with storage._connect_db() as db:
            await db.execute("DELETE FROM users WHERE username = 'alice'")
            await db.commit()

        choice = await auth_identity.resolve("google", _claims(), get_settings())

        assert isinstance(choice, auth_identity.LoginChoice)
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM user_identities"
                " WHERE provider = 'google' AND subject = 'sub-1'"
            )
            assert (await cursor.fetchone())["n"] == 0

        # The slot is free again: the owner can claim the identity.
        await storage.create_user_with_identity(
            "alice",
            provider="google",
            subject="sub-1",
            email="alice@example.com",
            status="active",
            role="reviewer",
            created_via="google",
        )


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


@pytest.mark.parametrize("authoritative", [False, True])
async def test_a_missing_email_is_refused_when_the_list_is_set(
    temp_db_path: Path, authoritative: bool
) -> None:
    """No address means nothing to check: fail closed, do not guess.

    An authoritative provider gets no exception here — there is still no domain.
    """
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="redhat.com")
    for _client in make_app_client(temp_db_path, env):
        with pytest.raises(AuthError) as excinfo:
            await auth_identity.resolve(
                "google",
                _claims(email=None, email_is_authoritative=authoritative),
                get_settings(),
            )
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


async def test_ticket_username_exists_check_existing(temp_db_path: Path) -> None:
    """A bound ticket with an existing username reports username_exists: True."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("existing-user")
        ticket = await _ticket(client)

        body = client.post(
            "/api/auth/sso/ticket",
            json={"ticket": ticket, "username": "existing-user"},
        ).json()

        assert body["username_exists"] is True
        assert body["provider"] == "google"
        # Ticket is not spent — still usable.
        body2 = client.post("/api/auth/sso/ticket", json={"ticket": ticket}).json()
        assert body2["provider"] == "google"
        assert "username_exists" not in body2


async def test_ticket_username_exists_check_nonexistent(temp_db_path: Path) -> None:
    """A bound ticket with a non-existent username reports username_exists: False."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)

        body = client.post(
            "/api/auth/sso/ticket",
            json={"ticket": ticket, "username": "no-such-user"},
        ).json()

        assert body["username_exists"] is False
        assert body["provider"] == "google"


async def test_ticket_username_check_needs_binding(temp_db_path: Path) -> None:
    """Without a valid ticket or binding cookie, the probe is refused."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        # No binding cookie: refused (404 — same as expired, by design).
        ticket = await _ticket()  # no client → no cookie
        response = client.post(
            "/api/auth/sso/ticket",
            json={"ticket": ticket, "username": "alice"},
        )
        assert response.status_code == 404

        # Bogus ticket: refused.
        client.cookies.set(sso_state.TICKET_COOKIE, sso_state.new_ticket_binding())
        response = client.post(
            "/api/auth/sso/ticket",
            json={"ticket": "nonexistent-ticket", "username": "alice"},
        )
        assert response.status_code == 404


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


async def test_both_link_paths_refuse_the_same_account(temp_db_path: Path) -> None:
    """The ticket route and the live callback are one rule, not two.

    Both doors write the same credential onto the same account, so a name outside
    ``ALLOWED_USERS`` must be refused at both, with the same audited reason — and
    an account inside the list must still link through the browser flow (the
    control below), or the refusal would just be "linking is broken".
    """
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        # Door one: the credential-verified ticket. A 403, as before.
        ticket_refusal = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )
        assert ticket_refusal.status_code == 403

        # Door two: the live callback, for the same account and provider.
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            callback_refusal = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert callback_refusal.status_code == 303
        assert callback_refusal.headers["location"] == "/settings?sso_error=denied"
        assert await storage.list_user_identities("ruthn") == []
        # One rule, so one reason: both doors left the same row for the same account.
        refusals = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.sso_login_failed"
            and row["details"]["reason"] == "not_allowed"
        ]
        assert [row["actor"] for row in refusals] == ["ruthn", "ruthn"]
        assert [row["details"]["provider"] for row in refusals] == ["google", "google"]
        assert {row["outcome"] for row in refusals} == {"denied"}


async def test_a_browser_link_works_for_an_allow_listed_account(
    temp_db_path: Path,
) -> None:
    """The control: inside ``ALLOWED_USERS`` the same callback does link."""
    env = dict(_SSO_ENV, ALLOWED_USERS="ruthn")
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

        assert response.status_code == 303
        assert response.headers["location"] == "/settings?sso_linked=google"
        linked = await storage.list_user_identities("ruthn")
        assert [(row["provider"], row["subject"]) for row in linked] == [
            ("google", "sub-1")
        ]


async def test_admin_outside_allow_list_can_link_via_ticket(
    temp_db_path: Path,
) -> None:
    """Registered admins always bypass ``ALLOWED_USERS`` on first-login ticket link."""
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        _, raw_key = await storage.create_user("boss", role="admin")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "boss", "api_key": raw_key},
        )

        assert response.status_code == 200
        linked = await storage.list_user_identities("boss")
        assert [(row["provider"], row["subject"]) for row in linked] == [
            ("google", "sub-1")
        ]


async def test_admin_outside_allow_list_can_link_via_browser_callback(
    temp_db_path: Path,
) -> None:
    """Registered admins bypass ``ALLOWED_USERS`` on Settings link callback (live row)."""
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        # Live row is admin; even a stale non-admin session bit must see the live role.
        await storage.create_user("boss", role="admin")
        session = await storage.create_session("boss", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="boss", next_path="/settings"
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
        linked = await storage.list_user_identities("boss")
        assert [(row["provider"], row["subject"]) for row in linked] == [
            ("google", "sub-1")
        ]


async def test_demoted_admin_stale_session_cannot_bypass_link_allow_list(
    temp_db_path: Path,
) -> None:
    """``_sso_finish_link`` reads admin status from the live users row, not the session."""
    env = dict(_SSO_ENV, ALLOWED_USERS="someone-else")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn", role="reviewer")
        # Stale session still has is_admin=True, but live row is reviewer.
        session = await storage.create_session("ruthn", is_admin=True, role="admin")
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

        assert response.status_code == 303
        assert response.headers["location"] == "/settings?sso_error=denied"
        assert await storage.list_user_identities("ruthn") == []
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "not_allowed"
        assert refusal["actor"] == "ruthn"


async def test_a_stolen_ticket_cannot_be_redeemed_in_another_browser(
    temp_db_path: Path,
) -> None:
    """The URL alone must not bind an identity to the thief's own account.

    Otherwise a stolen link lets an attacker attach the victim's IdP identity to
    the attacker's account, so every later victim login lands there.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        # What this browser was handed; the thief never sees it.
        binding = client.cookies.get(sso_state.TICKET_COOKIE)
        assert binding
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
        client.cookies.set(sso_state.TICKET_COOKIE, binding)
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
            ).status_code
            == 200
        )


async def test_a_cookie_derived_from_the_ticket_does_not_redeem_it(
    temp_db_path: Path,
) -> None:
    """The exact attack: the fragment alone must not yield the second half.

    Anyone who saw the link can compute ``sha256(ticket)``. If that were the
    binding, a leaked link would be redeemable anywhere.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        client.cookies.clear()
        client.cookies.set(
            sso_state.TICKET_COOKIE,
            hashlib.sha256(ticket.encode("utf-8")).hexdigest(),
        )

        assert (
            client.post("/api/auth/sso/ticket", json={"ticket": ticket}).status_code
            == 404
        )
        assert (
            client.post(
                "/api/auth/sso/create", json={"ticket": ticket, "username": "alice"}
            ).status_code
            == 404
        )


async def test_a_ticket_without_any_binding_cookie_is_refused(
    temp_db_path: Path,
) -> None:
    """No cookie is the same 404 as a wrong one: the answer names neither."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        ticket = await _ticket(client)
        client.cookies.clear()

        response = client.post("/api/auth/sso/ticket", json={"ticket": ticket})
        assert response.status_code == 404
        assert response.json()["detail"] == "This sign-in link has expired"


async def test_the_binding_cookie_is_not_derivable_from_the_ticket(
    temp_db_path: Path,
) -> None:
    """The cookie the callback sets is a fresh random value, not a ticket digest."""
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
        ticket = response.headers["location"].partition("#sso_ticket=")[2]
        assert ticket
        binding = response.cookies.get(sso_state.TICKET_COOKIE)
        assert binding
        assert binding != ticket
        assert binding != hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        # And it is the value the mint actually stored, so the pair works.
        assert await storage.sso_ticket_binding_hash(ticket) == (
            sso_state.ticket_binding_hash(binding)
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


async def test_a_second_identity_for_the_same_provider_is_still_refused(
    temp_db_path: Path,
) -> None:
    """Only the identity the account already holds is a no-op, not another one."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        ticket = await _ticket(client, subject="sub-2")

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 409
        assert [i["subject"] for i in await storage.list_user_identities("ruthn")] == [
            "sub-1"
        ]


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


async def test_a_block_committed_during_the_link_is_denied_with_its_reason(
    temp_db_path: Path,
) -> None:
    """Storage's eligibility verdict answers like the route's own refusal.

    The route checks the account minutes before the ticket comes back, so the
    block an admin commits in that window lands in the write itself. The reason
    storage names is audited as-is and answered 403 with the same detail the
    route's own gate uses — not the generic "that sign-in cannot be used".
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        with patch(
            "rootcoz.storage.add_user_identity",
            side_effect=ValueError("account_pending"),
        ):
            response = client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "Account cannot be used to sign in"
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "account_pending"
        assert refusal["actor"] == "ruthn"


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


async def test_a_cancelled_link_returns_to_its_destination(
    temp_db_path: Path,
) -> None:
    """The attempt is opened before ``error`` is handled, so a link keeps its page.

    ``/login`` would bounce the still-signed-in person away before the banner
    renders, so a cancelled link must land where the attempt was headed.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        response = client.get(
            f"/api/auth/sso/google/callback?error=access_denied&state={state}",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings?sso_error=provider_error"
        assert await storage.list_user_identities("ruthn") == []


async def test_a_cancelled_link_without_a_valid_attempt_keeps_login(
    temp_db_path: Path,
) -> None:
    """No destination comes from an attempt that did not validate."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        sealed, state = _sealed(
            mode=sso_state.MODE_LINK, link_to="ruthn", next_path="/settings"
        )

        # The sealed cookie never arrived: nothing can be opened, so the
        # destination it would have carried is not trusted.
        no_cookie = client.get(
            f"/api/auth/sso/google/callback?error=access_denied&state={state}",
            follow_redirects=False,
        )
        assert no_cookie.headers["location"] == "/login?sso_error=state_invalid"

        # The cookie arrived, but the state is not the one it sealed.
        client.cookies.set(sso_state.STATE_COOKIE, sealed)
        mismatched = client.get(
            "/api/auth/sso/google/callback?error=access_denied&state=not-the-state",
            follow_redirects=False,
        )
        assert mismatched.headers["location"] == "/login?sso_error=state_mismatch"


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
    overrides: dict[str, object] | None = None,
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
        overrides: Claims to replace, or to drop when the value is ``None``.

    Returns:
        The compact JWS and a key set usable by ``joserfc``.
    """
    import time as _time

    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": "sub-1",
        "email": "alice@example.com",
        "nonce": nonce,
        "exp": int(_time.time()) + expires_in,
        "iat": int(_time.time()),
    }
    for name, value in (overrides or {}).items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    token = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    return token, KeySet.import_key_set({"keys": [key.as_dict(private=False)]})


async def _verify(
    token: str,
    key_set: object,
    nonce: str = "attempt-nonce",
    provider: OIDCProvider | None = None,
) -> dict:
    """Run ``_verify_id_token`` with the JWKS fetch stubbed out.

    Args:
        token: The compact JWS to verify.
        key_set: The key set the token was signed with.
        nonce: The nonce the attempt sent.
        provider: The provider to verify with; Google when omitted.

    Returns:
        The verified claims.
    """
    provider = provider or GoogleProvider(
        client_id="test-client-id",
        client_secret="s",
        issuer=GoogleProvider.issuer,
    )
    with patch.object(type(provider), "_jwks_for", return_value=key_set, autospec=True):
        return await provider._verify_id_token(
            {"jwks_uri": f"{provider.issuer}/jwks"}, token, nonce
        )


@pytest.mark.parametrize(
    "issuer",
    [
        "https://accounts.google.com",
        "https://accounts.google.com/",
        # Google documents the bare host as an equally valid `iss` for its ID
        # tokens, so a correctly signed token carrying it must sign the person in.
        "accounts.google.com",
    ],
)
async def test_every_issuer_form_google_documents_is_accepted(issuer: str) -> None:
    """A valid Google login must not be refused over the spelling of ``iss``."""
    token, key_set = _signed_id_token(issuer=issuer)
    claims = await _verify(token, key_set)
    assert claims["sub"] == "sub-1"


async def test_another_provider_refuses_the_bare_form_of_its_issuer() -> None:
    """Keycloak documents no second form, so its issuer must match exactly."""
    provider = RedHatSSOProvider(
        client_id="test-client-id",
        client_secret="s",
        issuer="https://sso.example.com/auth/realms/x",
    )
    token, key_set = _signed_id_token(issuer="sso.example.com/auth/realms/x")
    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set, provider=provider)
    assert excinfo.value.reason == "id_token_invalid"
    assert "issuer" in str(excinfo.value)


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


@pytest.mark.parametrize("issuer", ["https://evil.example/", "accounts.evil.example"])
async def test_no_prefix_or_trailing_slash_lets_a_foreign_issuer_in(
    issuer: str,
) -> None:
    """Only a trailing slash is normalised: a near-miss issuer is still refused."""
    token, key_set = _signed_id_token(issuer=issuer)
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
    token, key_set = _signed_id_token(overrides={"exp": None})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "exp" in str(excinfo.value)


@pytest.mark.parametrize("issued_at", [None, "yesterday"])
async def test_a_token_without_a_numeric_iat_is_refused(issued_at: object) -> None:
    """``iat`` is required by OIDC Core: a missing one is not a pass."""
    token, key_set = _signed_id_token(overrides={"iat": issued_at})

    with pytest.raises(AuthError) as excinfo:
        await _verify(token, key_set)
    assert excinfo.value.reason == "id_token_invalid"
    assert "iat" in str(excinfo.value)


async def test_a_valid_token_within_the_clock_skew_is_still_accepted() -> None:
    """The iat rule must not turn small clock differences into a refused login."""
    import time as _time

    token, key_set = _signed_id_token(overrides={"iat": int(_time.time()) - 60})
    claims = await _verify(token, key_set)
    assert claims["sub"] == "sub-1"


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
        "iat": int(_time.time()),
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


async def test_the_link_start_is_its_own_operation(temp_db_path: Path) -> None:
    """One operation id per handler, and the id is the handler's own name.

    Both routes used to sit on one ``sso_login`` handler, so the generated
    client named the account write after the sign-in and vice versa.
    """
    from rootcoz.main import app

    routes = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/auth/sso/{provider}/login"
    ]
    assert {
        route.operation_id: route.endpoint.__name__
        for route in routes
        if getattr(route, "operation_id", None)
    } == {"ssoLogin": "sso_login", "ssoLinkStart": "sso_link_start"}


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


async def test_a_viewer_cannot_finish_a_link_but_a_reviewer_can(
    temp_db_path: Path,
) -> None:
    """The same role rule at the tail: a proven credential does not skip it.

    A viewer who may not remove a sign-in method must not be able to attach one,
    and the two halves of the flow must not disagree about it — while a reviewer
    claiming with their own credential still gets through, on the very ticket the
    viewer's refusal left unspent.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("vicky", role="viewer")
        viewer_key = await storage.rotate_own_key("vicky")
        await storage.create_user("rita", role="reviewer")
        reviewer_key = await storage.rotate_own_key("rita")
        ticket = await _ticket(client)

        refused = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "vicky", "api_key": viewer_key},
        )

        assert refused.status_code == 403
        # The same refusal the link-start route and the callback give.
        assert refused.json()["detail"] == (
            "Reviewer access required. Viewers cannot perform this action."
        )
        assert await storage.list_user_identities("vicky") == []
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "link_forbidden_role"
        assert refusal["actor"] == "vicky"
        # The refusal costs nothing: the ticket is still there for the next try.
        assert await storage.peek_sso_ticket(ticket) is not None

        linked = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "rita", "api_key": reviewer_key},
        )
        assert linked.status_code == 200, linked.text
        identities = await storage.list_user_identities("rita")
        assert [identity["provider"] for identity in identities] == ["google"]


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
        # Keyed user with one identity left: still unlinkable (local login remains).
        body_after = client.get("/api/user/identities").json()
        assert body_after["unlinkable"] is True
        assert body_after["has_api_key"] is True


async def test_keyed_user_can_unlink_sole_identity(temp_db_path: Path) -> None:
    """An account with a local API key can unlink its only SSO provider."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        response = client.delete("/api/user/identities/google")
        assert response.status_code == 200
        assert len(await storage.list_user_identities("ruthn")) == 0


async def test_the_last_sign_in_method_cannot_be_removed(temp_db_path: Path) -> None:
    """A keyless SSO-created account cannot unlink its only sign-in method."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_keyless_user("ruthn", created_via="proxy")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        body = client.get("/api/user/identities").json()
        assert body["unlinkable"] is False
        assert body["has_api_key"] is False

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


@pytest.mark.parametrize(
    ("username", "role", "is_admin", "allowed_users", "expected_status"),
    [
        ("bob", "reviewer", False, "someoneelse", 403),
        ("boss", "admin", True, "someoneelse", 200),
        ("alice", "reviewer", False, "alice", 200),
    ],
)
async def test_unlink_identity_enforces_allow_list(
    temp_db_path: Path,
    username: str,
    role: str,
    is_admin: bool,
    allowed_users: str,
    expected_status: int,
) -> None:
    """``DELETE /api/user/identities/{provider}`` enforces ``ALLOWED_USERS``; admins bypass."""
    env = dict(_SSO_ENV, ALLOWED_USERS=allowed_users)
    for client in make_app_client(temp_db_path, env):
        await storage.create_user(username, role=role)
        await storage.add_user_identity(
            username, "google", "sub-1", email=f"{username}@example.com"
        )
        session = await storage.create_session(username, is_admin=is_admin, role=role)
        client.cookies.set("rootcoz_session", session)

        response = client.delete("/api/user/identities/google")

        assert response.status_code == expected_status
        remaining = await storage.list_user_identities(username)
        assert len(remaining) == (1 if expected_status == 403 else 0)


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


async def test_a_refused_removal_is_audited(temp_db_path: Path) -> None:
    """A refusal is a probe: retrying the last removal must leave a trace.

    The status codes are unchanged — only what the trail sees is.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_keyless_user("ruthn", created_via="proxy")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        session = await storage.create_session("ruthn", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)

        assert client.delete("/api/user/identities/github").status_code == 404
        assert client.delete("/api/user/identities/google").status_code == 409

        rows = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.identity_unlink_refused"
        ]
        # Most recent first: the last-method attempt, then the probe.
        assert [
            (row["details"]["reason"], row["details"]["provider"]) for row in rows
        ] == [
            ("last_method", "google"),
            ("not_connected", "github"),
        ]
        for row in rows:
            assert row["actor"] == "ruthn"
            assert row["target_type"] == "user"
            assert row["target_id"] == "ruthn"
            assert row["outcome"] == "denied"
            assert row["details"]["as_admin"] is False
        # Nothing was removed by either refusal.
        assert len(await storage.list_user_identities("ruthn")) == 1


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
    """A keyless account's sole identity cannot be removed even by an admin."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_keyless_user("alice", created_via="proxy")
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
        # The admin's attempt is recorded too, and marked as acting on another
        # account — a lockout attempt is exactly what this row has to show.
        assert (
            client.delete("/api/admin/users/nobody/identities/google").status_code
            == 404
        )
        rows = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.identity_unlink_refused"
        ]
        assert [
            (row["details"]["reason"], row["details"]["as_admin"]) for row in rows
        ] == [
            ("not_connected", True),
            ("last_method", True),
        ]
        assert [row["actor"] for row in rows] == ["admin", "admin"]


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
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        client.cookies.clear()

        # The test client re-raises a server error, which is the failure the
        # terminal would see as a 500.
        with (
            patch.object(
                storage,
                "exchange_device_code_for_session",
                side_effect=RuntimeError("storage down"),
            ),
            pytest.raises(RuntimeError),
        ):
            client.post(
                "/api/auth/sso/device/poll",
                json={"device_code": started["device_code"]},
            )

        # The code survived the failure, so the same terminal can retry — after
        # the advertised interval, which the poll throttle enforces. No session
        # was left behind either: the pair is one transaction.
        assert await storage.get_device_code(started["device_code"]) is not None
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE username = 'alice'"
            )
            # Only the approver's own session, minted before the flow started.
            assert (await cursor.fetchone())["n"] == 1
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
            headers=_SAME_ORIGIN,
        )
        assert info.json()["client_name"] == "laptop"
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
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
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
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
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        client.cookies.clear()
        async with storage._connect_db() as db:
            await db.execute("UPDATE users SET disabled = 1 WHERE username = 'alice'")
            await db.commit()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        assert polled.json()["status"] == "denied"


async def test_a_revoked_domain_does_not_mint_a_device_session(
    temp_db_path: Path,
) -> None:
    """A domain removed while the browser session lives must reach the CLI too.

    Approval runs on a live session, so without a re-check here a removed domain
    would keep minting fresh device sessions until that session expired — the
    opposite of "removing a domain cuts off an already-linked account".
    """
    env = {**_SSO_ENV, "SSO_ALLOWED_DOMAINS": "redhat.com"}
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice", status="active")
        await storage.add_user_identity(
            "alice", "google", "sub-1", email="alice@elsewhere.example"
        )
        session = await storage.create_session("alice", auth_provider="google")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200, approved.text
        client.cookies.clear()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )

        assert polled.json()["status"] == "denied"
        rows = [
            r
            for r in await _audit_actions(temp_db_path)
            if r["action"].startswith("auth.sso_")
        ]
        assert any("domain_not_allowed" in json.dumps(r) for r in rows), rows


async def test_an_allowed_domain_still_mints_the_device_session(
    temp_db_path: Path,
) -> None:
    """The control: the same flow with an admitted domain hands over a token."""
    env = {**_SSO_ENV, "SSO_ALLOWED_DOMAINS": "redhat.com"}
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice", status="active")
        await storage.add_user_identity(
            "alice", "google", "sub-1", email="alice@redhat.com"
        )
        session = await storage.create_session("alice", auth_provider="google")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        client.cookies.clear()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )

        assert polled.json()["status"] == "approved"


async def test_a_downgrade_after_approval_is_not_undone_by_redemption(
    temp_db_path: Path,
) -> None:
    """Approval captures no rights: redemption reads the account as it is now.

    A demotion also invalidates the account's sessions. If redemption minted from
    the role the poll read, that invalidation would lose to a code approved
    before it.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_admin_user("boss")
        session = await storage.create_session("boss", is_admin=True, role="admin")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        client.cookies.clear()
        # Demoted while the code was still waiting for the terminal to poll.
        await storage.change_user_role("boss", "reviewer")

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        body = polled.json()
        assert body["status"] == "approved"
        minted = await storage.get_session(body["token"])
        assert minted is not None
        assert minted["role"] == "reviewer"
        assert bool(minted["is_admin"]) is False


async def test_a_downgrade_between_the_polls_read_and_the_exchange_wins(
    temp_db_path: Path,
) -> None:
    """The demotion lands in the window the poll leaves open, and still wins.

    ``sso_device_poll`` reads the account before it spends the code; this test
    commits the demotion *after* that read (the poll's own read is forced to
    "admin", the value a stale implementation would have minted) and *before*
    the exchange transaction. The exchange re-reads the row, so the session and
    the audit row name the rights the account actually holds now.
    """
    from rootcoz import main as main_module

    real_exchange = storage.exchange_device_code_for_session

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_admin_user("boss")
        session = await storage.create_session("boss", is_admin=True, role="admin")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        client.cookies.clear()

        async def exchange_after_the_downgrade(device_code: str, **kwargs: object):
            # The window between the poll's read and the exchange transaction.
            await storage.change_user_role("boss", "reviewer")
            return await real_exchange(device_code, **kwargs)

        with (
            # The poll sees the old rights; a mint from this value would be the bug.
            patch.object(main_module, "role_and_admin", return_value=("admin", True)),
            patch.object(
                storage,
                "exchange_device_code_for_session",
                exchange_after_the_downgrade,
            ),
        ):
            polled = client.post(
                "/api/auth/sso/device/poll",
                json={"device_code": started["device_code"]},
            )

        body = polled.json()
        assert body["status"] == "approved"
        minted = await storage.get_session(body["token"])
        assert minted is not None
        assert minted["role"] == "reviewer"
        assert bool(minted["is_admin"]) is False
        # The row names the session's real rights, not the poll's stale read.
        redeemed = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.sso_device_redeemed"
        ]
        assert redeemed and redeemed[0]["actor_role"] == "reviewer"
        assert redeemed[0]["details"]["role"] == "reviewer"


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
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert response.status_code == 400


async def test_a_bearer_approval_records_no_provider_from_a_cookie(
    temp_db_path: Path,
) -> None:
    """The provider is the one the winning credential was issued by, not a cookie's.

    A script (or a browser) can send a key and somebody else's live session
    cookie together — or its own expired one. Reading the provider from that
    cookie would stamp the code with an IdP the approver never signed in
    through, the CLI's session would inherit it, and a provider-pinned code
    could then be refused as a mismatch against its own approval.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _, alice_key = await storage.create_user("alice")
        await storage.create_user("bob")
        bob_session = await storage.create_session(
            "bob", is_admin=False, role="reviewer", auth_provider="github"
        )
        # Opened for google, approved with alice's key while bob's github
        # session rides along in the cookie jar.
        started = _start_device(client, provider="google")
        client.cookies.set("rootcoz_session", bob_session)

        response = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers={"Authorization": f"Bearer {alice_key}"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["provider"] is None

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT actor, details FROM audit_events"
                " WHERE action = 'auth.sso_device_approved' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
    assert row["actor"] == "alice"
    assert json.loads(row["details"])["provider"] is None


async def test_an_expired_session_cookie_records_no_provider(
    temp_db_path: Path,
) -> None:
    """A cookie outlives its row's TTL, so it proves nothing about this request.

    ``get_session_identity`` ignores expiry on purpose (logout must still find
    the IdP session of a stale cookie), so reading the provider from the cookie
    would stamp the code with an IdP the request no longer holds a session for.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        _, alice_key = await storage.create_user("alice")
        await storage.create_user("bob")
        bob_session = await storage.create_session(
            "bob", is_admin=False, role="reviewer", auth_provider="github"
        )
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE sessions SET expires_at = '2000-01-01 00:00:00' WHERE 1"
            )
            await db.commit()
        # Opened for google, approved with a key while bob's dead github cookie
        # rides along: read from that cookie the code would be refused as a
        # mismatch against the approval that is in fact valid.
        started = _start_device(client, provider="google")
        client.cookies.set("rootcoz_session", bob_session)

        response = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers={"Authorization": f"Bearer {alice_key}"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["provider"] is None

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT actor, details FROM audit_events"
                " WHERE action = 'auth.sso_device_approved' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
    assert row["actor"] == "alice"
    assert json.loads(row["details"])["provider"] is None


async def test_a_session_carried_as_a_bearer_stamps_its_own_provider(
    temp_db_path: Path,
) -> None:
    """A provider is provenance wherever its session is presented.

    The CLI carries a session token as a Bearer credential, and that token names
    the IdP the approval was signed in through — so a code opened for another
    provider is a mismatch, not an anonymous approval with no provenance at all.
    """
    for client in make_app_client(temp_db_path, {**_SSO_ENV, **_GITHUB_ENV}):
        await storage.create_user("alice")
        alice_session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="google"
        )
        bearer = {"Authorization": f"Bearer {alice_session}"}

        foreign = _start_device(client, provider="github")
        mismatched = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": foreign["user_code"]},
            headers=bearer,
        )
        assert mismatched.status_code == 400, mismatched.text

        started = _start_device(client, provider="google")
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=bearer,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["provider"] == "google"

        redeemed = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        identity = await storage.get_session_identity(redeemed.json()["token"])
    assert identity["auth_provider"] == "google"


def test_the_approval_takes_its_provider_from_the_request_state() -> None:
    """Structural guard: the ambient-cookie re-read must not come back.

    The provider is the one the *winning* credential was issued by, which the
    middleware already resolved onto ``request.state``. Re-reading a cookie or a
    session row here is the bug this replaced, not a shortcut around it.
    """
    from rootcoz import main as app_module

    source = inspect.getsource(app_module.sso_device_approve)
    assert 'getattr(request.state, "auth_provider", None)' in source
    assert "read_cookie" not in source
    assert "get_session_identity" not in source


async def test_an_unknown_user_code_is_not_approvable(temp_db_path: Path) -> None:
    """A mistyped code answers like an expired one, and never as a 500."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        client.cookies.set("rootcoz_session", session)
        response = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": "ZZZZ-ZZZZ"},
            headers=_SAME_ORIGIN,
        )
        assert response.status_code == 404


async def test_every_refused_approval_leaves_a_row(temp_db_path: Path) -> None:
    """A refusal is a decision: it must be readable afterwards, minus the code."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session(
            "alice", is_admin=False, role="reviewer", auth_provider="github"
        )
        pinned = _start_device(client, provider="google")
        open_code = _start_device(client)
        client.cookies.set("rootcoz_session", session)

        unknown = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": "ZZZZ-ZZZZ"},
            headers=_SAME_ORIGIN,
        )
        assert unknown.status_code == 404
        # Opened for google, approved from a github session.
        mismatch = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": pinned["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert mismatch.status_code == 400
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": open_code["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200
        again = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": open_code["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert again.status_code == 409

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT action, actor, outcome, details FROM audit_events"
                " WHERE action = 'auth.sso_device_approve_refused' ORDER BY id"
            )
            rows = [tuple(row) for row in await cursor.fetchall()]

    assert len(rows) == 3
    assert {(action, actor, outcome) for action, actor, outcome, _ in rows} == {
        ("auth.sso_device_approve_refused", "alice", "denied")
    }
    details = [json.loads(row[3]) for row in rows]
    assert [detail["reason"] for detail in details] == [
        "code_not_found",
        "provider_mismatch",
        "already_approved",
    ]
    # The code's provider where there is a code, the caller's where there is not.
    assert [detail["provider"] for detail in details] == [None, "google", "github"]
    # A user code is a credential a human types: no row may carry one.
    for _action, _actor, _outcome, stored in rows:
        for code in (pinned["user_code"], open_code["user_code"], "ZZZZ-ZZZZ"):
            assert code not in stored


@pytest.mark.parametrize("status", ["pending", "rejected"])
async def test_a_pending_or_rejected_account_is_denied_at_redemption(
    temp_db_path: Path, status: str
) -> None:
    """Approving a code is not a standing exemption from the approval gate."""
    env = {**_SSO_ENV, "REQUIRE_APPROVAL": "true"}
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("alice", status="active")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200
        client.cookies.clear()
        # The admin moves the account back to an unapproved state afterwards.
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET status = ? WHERE username = 'alice'", (status,)
            )
            await db.commit()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        )
        assert polled.json() == {"status": "denied"}

        # No credential was minted for it.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE username = 'alice'"
            )
            assert (await cursor.fetchone())["n"] == 1
            cursor = await db.execute(
                "SELECT details FROM audit_events"
                " WHERE action = 'auth.sso_login_failed' ORDER BY id DESC LIMIT 1"
            )
            refusal = json.loads((await cursor.fetchone())["details"])
        assert refusal["reason"] == f"account_{status}"


async def test_a_key_approval_does_not_stamp_the_requested_provider(
    temp_db_path: Path,
) -> None:
    """The code was opened for google; nobody signed in through it."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        # A username-and-key session carries no provider.
        key_session = await storage.create_session(
            "alice", is_admin=False, role="reviewer"
        )
        started = _start_device(client, provider="google")
        client.cookies.set("rootcoz_session", key_session)
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200
        # The pin is what allowed the approval, and it is not what signed in.
        assert approved.json()["provider"] is None
        client.cookies.clear()

        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        ).json()
        assert polled["status"] == "approved"
        assert polled["provider"] is None
        identity = await storage.get_session_identity(polled["token"])
        assert identity is not None and identity["auth_provider"] is None

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT details FROM audit_events"
                " WHERE action = 'auth.sso_device_approved' ORDER BY id DESC LIMIT 1"
            )
            details = json.loads((await cursor.fetchone())["details"])
        assert details["provider"] is None
        assert details["requested_provider"] == "google"


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
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
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


@pytest.mark.parametrize(
    "headers",
    [
        # Nothing proves the origin, so it is treated as cross-site.
        {},
        # A sibling page posts with the browser's own cookies and no preflight.
        {"origin": "https://evil.example"},
        {"sec-fetch-site": "same-site"},
    ],
)
async def test_an_approval_without_an_origin_proof_is_refused(
    temp_db_path: Path, headers: dict[str, str]
) -> None:
    """A sibling origin could borrow the cookie for a preflight-free form POST.

    Nothing proves the request came from this deployment's own approval page, so
    the code is not touched — and the lookup half of the page is refused by the
    same guard, so the pair cannot be split.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)

        refused = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=headers,
        )
        assert refused.status_code == 403
        looked_up = client.post(
            "/api/auth/sso/device/approve/lookup",
            json={"user_code": started["user_code"]},
            headers=headers,
        )
        assert looked_up.status_code == 403

        # The code is still unapproved, so no terminal was bound to this account.
        record = await storage.get_device_code_by_user_code(started["user_code"])
        assert record is not None and record["authorized"] is False

        # The page's own request is the one that approves it, and it is audited as
        # an approval rather than a refusal.
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200, approved.text
        record = await storage.get_device_code_by_user_code(started["user_code"])
        assert record is not None and record["authorized"] is True

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT details FROM audit_events"
                " WHERE action = 'auth.sso_device_approve_refused' ORDER BY id"
            )
            reasons = [
                json.loads(row["details"])["reason"] for row in await cursor.fetchall()
            ]
        assert reasons == ["cross_origin", "cross_origin"]
        assert (
            len(
                [
                    row
                    for row in await _audit_actions(temp_db_path)
                    if row["action"] == "auth.sso_device_approved"
                ]
            )
            == 1
        )


async def test_the_cli_approves_with_its_key_and_no_origin_proof(
    temp_db_path: Path,
) -> None:
    """A Bearer credential cannot be attached by a page, so it needs no proof.

    ``rootcoz auth sso-approve`` approves exactly here, and it must keep working.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        raw_key = await storage.rotate_own_key("alice")
        started = _start_device(client)

        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        assert approved.status_code == 200, approved.text
        assert approved.json()["ok"] is True


async def test_the_bootstrap_admin_can_approve_and_redeem(
    temp_db_path: Path,
) -> None:
    """The reserved admin has no users row and is still a legitimate approver.

    Reading the users table for the approver used to make the write fail, and the
    route blamed a race — 409 "already approved" for a code nobody had approved.
    The genuine second attempt is still 409, and each outcome is audited as what
    it actually was.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        started = _start_device(client, client_name="laptop")
        client.cookies.set("rootcoz_session", admin_login(client)["rootcoz_session"])

        approve = {"user_code": started["user_code"]}
        approved = client.post(
            "/api/auth/sso/device/approve", json=approve, headers=_SAME_ORIGIN
        )
        assert approved.status_code == 200, approved.text

        # The approval records the *account*: a name, with no row behind it.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT user_id, approved_username FROM sso_device_codes"
                " WHERE user_code = ?",
                (started["user_code"],),
            )
            row = await cursor.fetchone()
        assert row["approved_username"] == "admin"
        assert row["user_id"] is None

        # A real second approval is the race the 409 is for — and only that.
        again = client.post(
            "/api/auth/sso/device/approve", json=approve, headers=_SAME_ORIGIN
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "That code was already approved"

        # Nothing was pre-issued: the session is minted at redemption.
        client.cookies.clear()
        polled = client.post(
            "/api/auth/sso/device/poll", json={"device_code": started["device_code"]}
        ).json()
        assert polled["status"] == "approved"
        assert polled["username"] == "admin"
        session = await storage.get_session(polled["token"])
        assert session is not None
        assert session["username"] == "admin"
        assert session["role"] == "admin"
        assert bool(session["is_admin"]) is True

        rows = await _audit_actions(temp_db_path)
        assert [
            row["actor"] for row in rows if row["action"] == "auth.sso_device_approved"
        ] == ["admin"]
        assert [
            row["details"]["reason"]
            for row in rows
            if row["action"] == "auth.sso_device_approve_refused"
        ] == ["already_approved"]
        redeemed = [row for row in rows if row["action"] == "auth.sso_device_redeemed"]
        assert [(row["actor"], row["details"]["role"]) for row in redeemed] == [
            ("admin", "admin")
        ]


def test_one_reset_clears_the_discovery_and_key_set_caches() -> None:
    """Both caches, one call: the discovery tests reuse one issuer.

    Clearing only ``_METADATA_CACHE`` by hand left the JWKS cache leaking a
    previous test's key set into the next one.
    """
    from rootcoz.auth.sso import oidc

    oidc._METADATA_CACHE["https://idp.example"] = (0.0, {})
    oidc._JWKS_CACHE["https://idp.example"] = (0.0, {})
    oidc._reset_discovery_cache()
    assert not oidc._METADATA_CACHE
    assert not oidc._JWKS_CACHE


async def test_oidc_discovery_must_claim_the_configured_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery document is trusted for an hour: pin what it claims."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._reset_discovery_cache()
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


async def test_oidc_discovery_rejects_a_document_with_no_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document that pins nothing would be trusted for an hour anyway."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._reset_discovery_cache()
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
                "authorization_endpoint": "https://idp.example/auth",
                "token_endpoint": "https://idp.example/token",
                "jwks_uri": "https://idp.example/jwks",
            }
        ),
    )
    with pytest.raises(AuthError) as excinfo:
        await provider.metadata()
    assert excinfo.value.reason == "discovery_failed"
    # The message names the cause, so the login page's log is enough to fix it.
    assert "issuer" in str(excinfo.value)
    assert provider.issuer not in oidc_module._METADATA_CACHE


async def test_oidc_discovery_accepts_and_caches_a_matching_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin must admit the real document, and then trust it for the hour."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._reset_discovery_cache()
    # Same issuer, as an IdP is allowed to spell it: a trailing slash is not a
    # different issuer, so the pin must not refuse the deployment's own document.
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    document = {
        "issuer": "https://idp.example/",
        "authorization_endpoint": "https://idp.example/auth",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
    }
    fetches = 0

    async def _counted(*args: object, **kwargs: object) -> dict:
        nonlocal fetches
        fetches += 1
        return document

    monkeypatch.setattr(oidc_module, "request_json", _counted)

    assert await provider.metadata() is document
    # Accepted is cached: the second call is served without another fetch.
    assert await provider.metadata() is document
    assert fetches == 1


async def test_oidc_refuses_a_response_without_an_id_token() -> None:
    """No signature and no nonce is not an identity, so userinfo is not a fallback.

    Google and Red Hat SSO both answer the code exchange with an ``id_token``, so
    a response without one is a downgrade — a replayed access token from another
    attempt would otherwise pass as this one.
    """
    import httpx

    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._reset_discovery_cache()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://idp.example",
                    "authorization_endpoint": "https://idp.example/auth",
                    "token_endpoint": "https://idp.example/token",
                    "jwks_uri": "https://idp.example/jwks",
                    "userinfo_endpoint": "https://idp.example/userinfo",
                },
            )
        if request.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "at", "token_type": "Bearer"}
            )
        return httpx.Response(200, json={"sub": "sub-1"})

    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )
    with (
        _github_client(httpx.MockTransport(handler)),
        pytest.raises(AuthError) as excinfo,
    ):
        await provider.exchange(
            code="c", code_verifier="v", redirect_uri="https://x/cb", nonce="n"
        )

    assert excinfo.value.reason == "no_id_token"
    # The access token is never presented to the userinfo endpoint.
    assert "/userinfo" not in calls


async def test_an_oidc_login_without_an_id_token_is_refused_and_audited(
    temp_db_path: Path,
) -> None:
    """A downgraded token response stops the browser login, naming the cause.

    No provider rootcoz ships opts in to a userinfo-only identity, so a token
    response with no ``id_token`` is a refusal on the normal sign-in path: the
    browser gets ``no_id_token``, the trail records the same reason, and the
    access token is never presented to the userinfo endpoint.
    """
    import httpx

    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.com/auth",
                    "token_endpoint": "https://accounts.google.com/token",
                    "jwks_uri": "https://accounts.google.com/jwks",
                    "userinfo_endpoint": "https://accounts.google.com/userinfo",
                },
            )
        if request.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "at", "token_type": "Bearer"}
            )
        return httpx.Response(200, json={"sub": "sub-1"})

    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with _github_client(httpx.MockTransport(handler)):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        # Public by shape: the step that failed, never an account state.
        assert response.headers["location"] == "/login?sso_error=no_id_token"
        assert response.cookies.get("rootcoz_session") is None
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "no_id_token"
        # The unsigned userinfo answer was never asked for.
        assert "/userinfo" not in seen
        async with storage._connect_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM users")
            assert (await cursor.fetchone())["n"] == 0


async def test_a_key_document_with_no_key_list_is_refused_and_audited(
    temp_db_path: Path,
) -> None:
    """A ``jwks_uri`` answering ``{}`` is a sign-in refusal, not a 500.

    ``KeySet.import_key_set`` raises ``KeyError`` on a document with no ``keys``,
    and nothing on the sign-in path catches that: the browser would get a 500 and
    the trail no row. It is the handled ``jwks_failed`` refusal instead.
    """
    import httpx

    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    token, _key_set = _signed_id_token()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.com/auth",
                    "token_endpoint": "https://accounts.google.com/token",
                    "jwks_uri": "https://accounts.google.com/jwks",
                },
            )
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": token, "token_type": "Bearer"})
        # The broken document under test: a JSON object with no key list.
        return httpx.Response(200, json={})

    for client in make_app_client(temp_db_path, _SSO_ENV):
        sealed, state = _sealed()
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with _github_client(httpx.MockTransport(handler)):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?sso_error=")
        assert response.cookies.get("rootcoz_session") is None
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "jwks_failed"


async def test_a_broken_key_document_is_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document that failed validation must cost one login, not the whole TTL.

    Caching it would answer every login for ``METADATA_TTL_SECONDS`` from the
    same broken document, so the second attempt must refetch.
    """
    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    jwks_uri = "https://idp.example/jwks"
    fetches = 0

    async def _counting(*args: object, **kwargs: object) -> dict:
        nonlocal fetches
        fetches += 1
        return {}

    monkeypatch.setattr(oidc_module, "request_json", _counting)
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )

    for _attempt in range(2):
        with pytest.raises(AuthError) as excinfo:
            await provider._jwks_for(jwks_uri)
        assert excinfo.value.reason == "jwks_failed"
        # The message names the cause, so the audit row is enough to fix it.
        assert "key list" in str(excinfo.value)
    assert fetches == 2
    assert jwks_uri not in oidc_module._JWKS_CACHE


async def test_a_key_list_that_cannot_be_imported_is_refused_and_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list of unknown keys is a ``jwks_failed`` refusal, and not cached either."""
    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    jwks_uri = "https://idp.example/jwks"
    fetches = 0

    async def _counting(*args: object, **kwargs: object) -> dict:
        nonlocal fetches
        fetches += 1
        return {"keys": [{"kty": "nope"}]}

    monkeypatch.setattr(oidc_module, "request_json", _counting)
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )

    for _attempt in range(2):
        with pytest.raises(AuthError) as excinfo:
            await provider._jwks_for(jwks_uri)
        assert excinfo.value.reason == "jwks_failed"
    assert fetches == 2
    assert jwks_uri not in oidc_module._JWKS_CACHE


async def test_a_valid_key_set_verifies_and_a_warm_cache_avoids_a_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape check must admit real keys, and the cache must still serve them."""
    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    jwks_uri = "https://idp.example/jwks"
    token, key_set = _signed_id_token(
        issuer="https://idp.example", audience="cid", nonce="n1"
    )
    document = key_set.as_dict(private=False)
    fetches = 0

    async def _counting(*args: object, **kwargs: object) -> dict:
        nonlocal fetches
        fetches += 1
        return document

    monkeypatch.setattr(oidc_module, "request_json", _counting)
    provider = RedHatSSOProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://idp.example",
    )

    for _attempt in range(2):
        claims = await provider._verify_id_token({"jwks_uri": jwks_uri}, token, "n1")
        assert claims["sub"] == "sub-1"
    # The second verification is served from the cache, not the network.
    assert fetches == 1


async def test_oidc_discovery_rejects_a_plaintext_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoints we call must be https; loopback http is the documented exception."""
    from rootcoz.auth.sso import oidc as oidc_module
    from rootcoz.auth.sso.redhat import RedHatSSOProvider

    oidc_module._reset_discovery_cache()
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
        "iat": int(_time.time()),
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


async def test_linking_spends_the_ticket(temp_db_path: Path) -> None:
    """A ticket left live after a link could re-attach the identity later.

    Once the identity is unlinked from this account, a still-valid ticket would
    let its holder attach the same IdP identity to an account of their own.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        linked = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )
        assert linked.status_code == 200, linked.text
        assert await storage.peek_sso_ticket(ticket) is None

        # The same browser (binding cookie included) presents the same ticket.
        again = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )
        assert again.status_code == 404
        assert [i["provider"] for i in await storage.list_user_identities("ruthn")] == [
            "google"
        ]


async def test_a_repeat_link_is_a_no_op_and_still_spends_its_ticket(
    temp_db_path: Path,
) -> None:
    """Re-linking what the account already holds succeeds and inserts nothing.

    A retried POST (double submit, a re-run IdP flow) must not answer 409 for the
    row it wrote itself — and the fresh ticket must still die, or a later unlink
    would leave it able to re-attach this identity to another account.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        await storage.add_user_identity(
            "ruthn", "google", "sub-1", email="ruthn@example.com"
        )
        ticket = await _ticket(client, email="ruthn@example.com")

        again = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert again.status_code == 200, again.text
        assert again.json()["username"] == "ruthn"
        assert len(await storage.list_user_identities("ruthn")) == 1
        assert await storage.peek_sso_ticket(ticket) is None


async def test_a_link_whose_ticket_died_before_the_write_writes_no_identity(
    temp_db_path: Path,
) -> None:
    """The identity insert and the ticket spend commit together, or neither does."""
    from rootcoz.auth import password as auth_password

    real_verify = auth_password.PasswordLogin.verify

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("ruthn")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        # Bound as a default so the patched method closes over this request's
        # ticket, not the loop's variable.
        async def _verify_and_lose_the_ticket(
            self, username, api_key, settings, doomed: str = ticket
        ):
            verified = await real_verify(self, username, api_key, settings)
            # Another browser redeemed the ticket between the read and the write.
            assert await storage.consume_sso_ticket(doomed) is not None
            return verified

        with patch.object(
            auth_password.PasswordLogin, "verify", _verify_and_lose_the_ticket
        ):
            response = client.post(
                "/api/auth/sso/link",
                json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
            )

        assert response.status_code == 400
        # Nothing durable: the ticket was gone, so the link never landed.
        assert await storage.list_user_identities("ruthn") == []


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


# -- one approval gate for every way in ---------------------------------------


async def test_sso_does_not_hold_a_pending_account_when_approval_is_off(
    temp_db_path: Path,
) -> None:
    """The SSO gate is the login route's gate, condition for condition.

    Below ``REQUIRE_APPROVAL`` a password login of a pending account is allowed,
    so sending the SSO login of that same account to ``/pending`` — with a session
    the middleware then refuses — was one gate disagreeing with the other two.
    """
    for client in make_app_client(temp_db_path, _SSO_ENV):  # REQUIRE_APPROVAL=false
        await storage.create_user("ruthn", status="pending")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 200
        assert response.json()["next"] == "/"
        # The session it minted is usable: this account is an ordinary one here.
        assert client.get("/api/auth/me").status_code == 200
        actions = [row["action"] for row in await _audit_actions(temp_db_path)]
        assert "auth.sso_link" in actions
        assert "auth.sso_login_blocked" not in actions


async def test_an_admin_is_not_held_by_its_own_approval_status(
    temp_db_path: Path,
) -> None:
    """Neither gate ever holds an administrator, so the SSO path must not either."""
    env = {**_SSO_ENV, "REQUIRE_APPROVAL": "true"}
    for client in make_app_client(temp_db_path, env):
        await storage.create_admin_user("boss")
        raw_key = await storage.rotate_own_key("boss")
        # A row left pending — an import, or an admin who never approved itself.
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET status = 'pending' WHERE username = 'boss'"
            )
            await db.commit()
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "boss", "api_key": raw_key},
        )

        assert response.status_code == 200
        assert response.json()["next"] == "/"
        assert client.get("/api/auth/me").status_code == 200
        assert "auth.sso_login_blocked" not in [
            row["action"] for row in await _audit_actions(temp_db_path)
        ]


async def test_a_pending_account_cannot_link_a_provider_through_a_ticket(
    temp_db_path: Path,
) -> None:
    """A valid key proves ownership; approval still decides whether it may act.

    The browser link path and the login route both hold a pending account back,
    and this route writes the same link, so it has to ask the same question —
    otherwise a pending or rejected account could attach a provider identity and
    keep it after an admin acts.
    """
    env = {**_SSO_ENV, "REQUIRE_APPROVAL": "true"}
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("ruthn", status="pending")
        raw_key = await storage.rotate_own_key("ruthn")
        ticket = await _ticket(client)

        response = client.post(
            "/api/auth/sso/link",
            json={"ticket": ticket, "username": "ruthn", "api_key": raw_key},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Account cannot be used to sign in"
        # The link the whole flow exists to write did not happen.
        assert await storage.get_user_identity("google", "sub-1") is None
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "account_pending"
        assert refusal["actor"] == "ruthn"


async def test_above_require_approval_a_pending_account_is_held_with_its_reason(
    temp_db_path: Path,
) -> None:
    """The held half of the pair: approval on, non-admin, pending.

    ``REQUIRE_APPROVAL`` on means the row decides access, so the SSO login lands
    on ``/pending`` and the trail records the state it was held for — the same
    answer the password login of that account gives, from the same gate.
    """
    env = {**_SSO_ENV, "REQUIRE_APPROVAL": "true"}
    for client in make_app_client(temp_db_path, env):
        # Linked while approved, then left pending: an identity that exists on an
        # unapproved account is the case this gate has to hold, and it is the
        # only order that produces it — the link write itself refuses a blocked
        # account now.
        await storage.create_user("ruthn")
        await storage.add_user_identity("ruthn", "google", "sub-1")
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET status = 'pending' WHERE username = ?", ("ruthn",)
            )
            await db.commit()
        raw_key = await storage.rotate_own_key("ruthn")
        sealed, state = _sealed(next_path="/report/1")
        client.cookies.set(sso_state.STATE_COOKIE, sealed)

        with patch.object(
            GoogleProvider, "exchange", return_value=_claims(), autospec=True
        ):
            response = client.get(
                f"/api/auth/sso/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        # Held: not the page the flow was headed for.
        assert response.status_code == 303
        assert response.headers["location"] == "/pending"
        blocked = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.sso_login_blocked"
        ]
        assert [(row["outcome"], row["details"]["status"]) for row in blocked] == [
            ("denied", "pending")
        ]

        # The same account at the password door: the same refusal, same reason.
        login = client.post(
            "/api/auth/login", json={"username": "ruthn", "api_key": raw_key}
        )
        assert login.status_code == 403
        assert login.json()["status"] == "pending"


async def test_a_block_committed_inside_the_exchange_is_denied_with_its_reason(
    temp_db_path: Path,
) -> None:
    """The poll's eligibility read is stale by design; the mint must not be.

    The window between that read and the exchange transaction is exactly where an
    admin blocks the account — and the block has already dropped its sessions, so
    a token minted now would outlive that invalidation.
    """
    real_exchange = storage.exchange_device_code_for_session

    async def exchange_after_the_block(device_code: str, **kwargs: object):
        # The block lands between the poll's checks and the transaction.
        assert await storage.set_user_disabled(str(kwargs["username"]), True)
        return await real_exchange(device_code, **kwargs)

    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("alice")
        session = await storage.create_session("alice", is_admin=False, role="reviewer")
        started = _start_device(client)
        client.cookies.set("rootcoz_session", session)
        approved = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approved.status_code == 200
        client.cookies.clear()

        with patch.object(
            storage, "exchange_device_code_for_session", exchange_after_the_block
        ):
            polled = client.post(
                "/api/auth/sso/device/poll",
                json={"device_code": started["device_code"]},
            )

        assert polled.json() == {"status": "denied"}
        # Disabling dropped the approver's session, and the refused mint added
        # none: the account has no live credential at all.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE username = 'alice'"
            )
            assert (await cursor.fetchone())["n"] == 0
        # The trail names the block instead of reporting the code as expired.
        refusal = await _last_sso_refusal(temp_db_path)
        assert refusal["details"]["reason"] == "account_disabled"


async def test_an_existing_account_outside_the_allow_list_leaves_the_login_page(
    temp_db_path: Path,
) -> None:
    """The login page asks about the row before it judges the name.

    ``ALLOWED_USERS`` bounds *creation*, and a read-only account is outside it
    by design. The proxy strategy lets that account in on every other page, so
    holding it on ``/login`` was the login page refusing a person the rest of
    the app admits.
    """
    for client in make_app_client(temp_db_path, _PROXY_ALLOW_LIST_ENV):
        await storage.create_user("reader", status="active")
        headers = {"X-Forwarded-User": "reader"}

        login = client.get("/login", headers=headers, follow_redirects=False)

        assert login.status_code == 303
        assert login.headers["location"] == "/"
        assert client.get("/api/auth/me", headers=headers).status_code == 200

    actions = [row["action"] for row in await _audit_actions(temp_db_path)]
    assert "auth.proxy_provision_refused" not in actions


async def test_a_name_with_no_account_outside_the_allow_list_stays_refused(
    temp_db_path: Path,
) -> None:
    """The control: with no row there is nothing to create, so the rule applies."""
    for client in make_app_client(temp_db_path, _PROXY_ALLOW_LIST_ENV):
        response = client.get(
            "/login",
            headers={"X-Forwarded-User": "stranger"},
            follow_redirects=False,
        )

        # Served by the SPA shell, which a fresh CI tree has not built yet.
        _assert_spa_page(response)
        assert await storage.get_user_by_username("stranger") is None

    refusals = [
        row
        for row in await _audit_actions(temp_db_path)
        if row["action"] == "auth.proxy_provision_refused"
    ]
    assert len(refusals) == 1
    assert refusals[0]["details"]["reason"] == "not_allowed"


async def test_an_existing_pending_account_still_lands_on_pending(
    temp_db_path: Path,
) -> None:
    """Skipping the name rule must not skip the approval gate."""
    env = dict(_PROXY_ALLOW_LIST_ENV, REQUIRE_APPROVAL="true")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("reader", status="pending")

        login = client.get(
            "/login",
            headers={"X-Forwarded-User": "reader"},
            follow_redirects=False,
        )

        assert login.status_code == 303
        assert login.headers["location"] == "/pending"


async def test_an_existing_disabled_account_gets_no_working_sign_in(
    temp_db_path: Path,
) -> None:
    """Skipping the name rule must not skip the block either."""
    for client in make_app_client(temp_db_path, _PROXY_ALLOW_LIST_ENV):
        await storage.create_user("reader", status="active")
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE users SET disabled = 1 WHERE username = 'reader'")
        headers = {"X-Forwarded-User": "reader"}

        login = client.get("/login", headers=headers, follow_redirects=False)

        _assert_spa_page(login)
        assert client.get("/api/auth/me", headers=headers).status_code == 401


#: A trusted-proxy deployment: the header is the only credential in play.
_PROXY_ENV = {"TRUST_PROXY_HEADERS": "true"}


async def test_a_provisioned_proxy_row_carries_no_api_key(temp_db_path: Path) -> None:
    """Provisioning records the person, not a secret nobody was ever shown.

    A key minted here would be stored and never displayed, so the row would look
    key-bearing while its owner held nothing — and self-service key login would
    be impossible without an administrator.
    """
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        headers = {"X-Forwarded-User": "sso-provisioned"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        # Not one page: the header authenticates every other request too.
        assert client.get("/api/dashboard", headers=headers).status_code == 200

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash, role, status, created_via FROM users"
                " WHERE username = 'sso-provisioned'"
            )
            row = dict(await cursor.fetchone())

        assert row == {
            "api_key_hash": None,
            "role": "reviewer",
            "status": "active",
            "created_via": "proxy",
        }


async def test_registering_a_provisioned_name_is_refused(temp_db_path: Path) -> None:
    """A name is not a credential: registration must not claim the account.

    The row is keyless because the header is its credential, not because it is
    unclaimed. Answering registration with a first key would let anyone who
    knows a provisioned username take that person's account over.
    """
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        assert (
            client.get(
                "/api/auth/me", headers={"X-Forwarded-User": "sso-provisioned"}
            ).status_code
            == 200
        )

        response = client.post(
            "/api/auth/register", json={"username": "sso-provisioned"}
        )

        assert response.status_code == 400
        # One message for every existing name, so the answer says nothing about
        # whether this row holds a key — that difference is what would tell a
        # stranger which names are worth probing.
        assert response.json()["detail"] == (
            "User 'sso-provisioned' already exists. Sign in with your API key, or"
            " ask an administrator to issue one (admin users rotate-key)."
        )
        assert "api_key" not in response.json()

        # Still keyless and still the proxy's account: the refused claim wrote
        # nothing.
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash, created_via FROM users"
                " WHERE username = 'sso-provisioned'"
            )
            assert dict(await cursor.fetchone()) == {
                "api_key_hash": None,
                "created_via": "proxy",
            }


async def test_an_administrator_rotation_restores_key_login(
    temp_db_path: Path,
) -> None:
    """The documented recovery for a person whose proxy went away.

    An administrator rotates the key — an audited act — and the new key logs the
    account in. Self-service by name is never the way back in.
    """
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        assert (
            client.get(
                "/api/auth/me", headers={"X-Forwarded-User": "sso-provisioned"}
            ).status_code
            == 200
        )

        rotated = client.post(
            "/api/admin/users/sso-provisioned/rotate-key", cookies=admin_login(client)
        )
        assert rotated.status_code == 200
        new_key = rotated.json()["new_api_key"]

        client.cookies.clear()
        login = client.post(
            "/api/auth/login",
            json={"username": "sso-provisioned", "api_key": new_key},
        )

        assert login.status_code == 200
        assert login.json()["username"] == "sso-provisioned"


async def test_provisioning_still_audits_and_still_waits_for_approval(
    temp_db_path: Path,
) -> None:
    """Keyless changes nothing about the row's meaning: same status, same trail."""
    env = dict(_PROXY_ENV, REQUIRE_APPROVAL="true")
    for client in make_app_client(temp_db_path, env):
        held = client.get(
            "/api/auth/me", headers={"X-Forwarded-User": "sso-provisioned"}
        )
        assert held.status_code == 403

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT api_key_hash, role, status, created_via FROM users"
                " WHERE username = 'sso-provisioned'"
            )
            assert dict(await cursor.fetchone()) == {
                "api_key_hash": None,
                "role": "reviewer",
                "status": "pending",
                "created_via": "proxy",
            }

        provisioned = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.proxy_provisioned"
        ]
        assert len(provisioned) == 1
        assert provisioned[0]["details"] == {
            "status": "pending",
            "created_via": "proxy",
            "role": "reviewer",
        }


async def test_admin_users_list_includes_identities(temp_db_path: Path) -> None:
    """GET /api/admin/users carries per-user identities with display_name."""
    for client in make_app_client(temp_db_path, _SSO_ENV):
        await storage.create_user("nolinks")
        await storage.create_user("onelink")
        await storage.add_user_identity(
            "onelink", "google", "sub-ol", email="onelink@g.com"
        )
        await storage.create_keyless_user("keyless-one", created_via="proxy")
        await storage.add_user_identity(
            "keyless-one", "google", "sub-kl", email="keyless@g.com"
        )
        await storage.create_user("twolinks")
        await storage.add_user_identity(
            "twolinks", "google", "sub-tl-g", email="twolinks@g.com"
        )
        await storage.add_user_identity(
            "twolinks", "github", "sub-tl-gh", email="twolinks@gh.com"
        )

        cookies = admin_login(client)
        resp = client.get("/api/admin/users", cookies=cookies)
        assert resp.status_code == 200
        users_by_name = {u["username"]: u for u in resp.json()["users"]}

        # 0 identities, keyed
        u0 = users_by_name["nolinks"]
        assert u0["identities"] == []
        assert u0["identities_unlinkable"] is False
        assert u0["has_api_key"] is True

        # 1 identity, keyed — unlinkable because local login remains
        u1 = users_by_name["onelink"]
        assert len(u1["identities"]) == 1
        i1 = u1["identities"][0]
        assert i1["provider"] == "google"
        assert i1["display_name"] == "Google"
        assert i1["email"] == "onelink@g.com"
        assert "created_at" in i1
        assert "subject" not in i1
        assert u1["identities_unlinkable"] is True
        assert u1["has_api_key"] is True

        # 1 identity, keyless — NOT unlinkable
        uk = users_by_name["keyless-one"]
        assert len(uk["identities"]) == 1
        assert uk["identities_unlinkable"] is False
        assert uk["has_api_key"] is False

        # 2 identities
        u2 = users_by_name["twolinks"]
        assert len(u2["identities"]) == 2
        providers = [i["provider"] for i in u2["identities"]]
        assert providers == ["github", "google"]
        assert u2["identities"][0]["display_name"] == "GitHub"
        assert u2["identities"][1]["display_name"] == "Google"
        assert u2["identities_unlinkable"] is True
        assert u2["has_api_key"] is True


# ---------------------------------------------------------------------------
# Qodo #2 — pre-existing rootcoz* names authenticate via proxy
# ---------------------------------------------------------------------------


async def test_a_preexisting_rootcoz_legacy_account_authenticates_via_proxy(
    temp_db_path: Path,
) -> None:
    """A legacy ``rootcoz-legacy`` row created before the prefix restriction
    must still authenticate via ``X-Forwarded-User`` — the prefix restriction
    is enforced only at provisioning time for brand-new accounts."""
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        # Pre-create the legacy account directly in the DB (bypassing
        # _validate_username which would now refuse it).
        import sqlite3 as _sqlite3

        with _sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO users (username, role, status, created_via)"
                " VALUES ('rootcoz-legacy', 'reviewer', 'active', 'admin')"
            )

        response = client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "rootcoz-legacy"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "rootcoz-legacy"


async def test_a_nonexistent_rootcoz_new_name_is_refused_by_proxy(
    temp_db_path: Path,
) -> None:
    """A brand-new ``rootcoz-new`` name with no existing row must be refused
    at provisioning (``reserved_name`` / ``unusable_name``)."""
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        response = client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "rootcoz-new"},
        )
        assert response.status_code == 401
        assert await storage.get_user_by_username("rootcoz-new") is None

    refusals = [
        row
        for row in await _audit_actions(temp_db_path)
        if row["action"] == "auth.proxy_provision_refused"
    ]
    assert len(refusals) >= 1
    assert refusals[0]["details"]["reason"] == "unusable_name"


async def test_admin_and_rootcoz_ai_are_always_refused_by_proxy(
    temp_db_path: Path,
) -> None:
    """``admin`` and ``rootcoz-ai`` must never authenticate via proxy, even if
    someone forged a row."""
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        for name in ("admin", "rootcoz-ai"):
            response = client.get(
                "/api/auth/me",
                headers={"X-Forwarded-User": name},
            )
            assert response.status_code == 401, f"{name} should be refused"


# ---------------------------------------------------------------------------
# Qodo #3 — revoked domain must not mint a CLI session
# ---------------------------------------------------------------------------


async def test_a_domain_revocation_between_the_poll_and_the_exchange_refuses_redemption(
    temp_db_path: Path,
) -> None:
    """A domain removed from ``SSO_ALLOWED_DOMAINS`` after the poll's initial
    read but before the exchange transaction must deny the CLI session."""
    env = dict(_SSO_ENV, SSO_ALLOWED_DOMAINS="example.com")
    for client in make_app_client(temp_db_path, env):
        await storage.create_user("domuser")
        await storage.add_user_identity(
            username="domuser",
            provider="google",
            subject="sub-dom",
            email="domuser@example.com",
        )
        session = await storage.create_session(
            "domuser", is_admin=False, role="reviewer", auth_provider="google"
        )
        started = _start_device(client, provider="google")

        # Approve the code while the domain is still allowed.
        client.cookies.set("rootcoz_session", session)
        approve = client.post(
            "/api/auth/sso/device/approve",
            json={"user_code": started["user_code"]},
            headers=_SAME_ORIGIN,
        )
        assert approve.status_code == 200
        client.cookies.clear()

        real_exchange = storage.exchange_device_code_for_session

        async def exchange_after_revocation(device_code: str, **kwargs: object):
            # Simulate a domain revocation between poll and exchange:
            # update the in-memory settings cache so get_settings() returns
            # the revoked domain list.
            update_db_settings_cache({"sso_allowed_domains": "other.org"})
            return await real_exchange(device_code, **kwargs)  # noqa: B023

        with patch.object(
            storage,
            "exchange_device_code_for_session",
            exchange_after_revocation,
        ):
            polled = client.post(
                "/api/auth/sso/device/poll",
                json={"device_code": started["device_code"]},
            )

        body = polled.json()
        assert body["status"] == "denied"

        # Verify no session was minted.
        refusals = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.sso_login_failed"
            and "domain_not_allowed" in str(row.get("details", {}))
        ]
        assert len(refusals) >= 1


# ---------------------------------------------------------------------------
# Qodo #4 — OIDC endpoint origin validation
# ---------------------------------------------------------------------------


async def test_redhat_sso_discovery_rejects_a_cross_origin_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovery document pointing token_endpoint or jwks_uri to an
    arbitrary private or external origin must be refused."""
    from rootcoz.auth.sso import oidc as oidc_module

    for bad_url, key in [
        ("https://169.254.169.254/latest", "token_endpoint"),
        ("http://127.0.0.1:9100/sessions", "jwks_uri"),
        ("https://evil.example/token", "token_endpoint"),
    ]:
        oidc_module._reset_discovery_cache()
        provider = RedHatSSOProvider(
            client_id="cid",
            client_secret="secret",  # pragma: allowlist secret
            issuer="https://idp.example",
        )
        doc = {
            "issuer": "https://idp.example",
            "authorization_endpoint": "https://idp.example/auth",
            "token_endpoint": "https://idp.example/token",
            "jwks_uri": "https://idp.example/jwks",
        }
        doc[key] = bad_url
        monkeypatch.setattr(oidc_module, "request_json", _returning(doc))
        with pytest.raises(AuthError) as excinfo:
            await provider.metadata()
        assert excinfo.value.reason == "discovery_failed", (
            f"Expected discovery_failed for {key}={bad_url}"
        )


async def test_google_discovery_accepts_its_multi_host_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google's real discovery uses accounts.google.com, oauth2.googleapis.com
    and www.googleapis.com — all must be accepted."""
    from rootcoz.auth.sso import oidc as oidc_module

    oidc_module._reset_discovery_cache()
    provider = GoogleProvider(
        client_id="cid",
        client_secret="secret",  # pragma: allowlist secret
        issuer="https://accounts.google.com",
    )
    doc = {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    }
    monkeypatch.setattr(oidc_module, "request_json", _returning(doc))
    result = await provider.metadata()
    assert result is doc


async def test_google_discovery_rejects_a_loopback_or_evil_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even for Google, an endpoint pointing at loopback or an unknown origin
    must be refused."""
    from rootcoz.auth.sso import oidc as oidc_module

    for bad_url, key in [
        ("https://127.0.0.1/token", "token_endpoint"),
        ("https://evil.example/jwks", "jwks_uri"),
    ]:
        oidc_module._reset_discovery_cache()
        provider = GoogleProvider(
            client_id="cid",
            client_secret="secret",  # pragma: allowlist secret
            issuer="https://accounts.google.com",
        )
        doc = {
            "issuer": "https://accounts.google.com",
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
        }
        doc[key] = bad_url
        monkeypatch.setattr(oidc_module, "request_json", _returning(doc))
        with pytest.raises(AuthError) as excinfo:
            await provider.metadata()
        assert excinfo.value.reason == "discovery_failed", (
            f"Expected discovery_failed for {key}={bad_url}"
        )


# ---------------------------------------------------------------------------
# Qodo #5 — OpenShift identity 403 logs a warning
# ---------------------------------------------------------------------------


async def test_openshift_identity_403_logs_warning_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the cluster returns 403 for an identity lookup, ``_email`` must
    log a WARNING and return ``None`` instead of crashing."""
    import logging

    import httpx

    user = {
        "metadata": {"uid": "u-403", "name": "forbidden-user"},
        "identities": ["oidc:forbidden"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "sha256~token"})
        if request.url.path.endswith("/users/~"):
            return httpx.Response(200, json=user)
        if "/identities/" in request.url.path:
            return httpx.Response(403, json={"kind": "Status", "message": "forbidden"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    from rootcoz.auth.sso import openshift as openshift_module

    # The module logger has propagate=False (get_child_logger convention),
    # so caplog's root-level handler doesn't see it.  Temporarily enable
    # propagation so caplog captures the record.
    openshift_logger = openshift_module.logger
    old_propagate = openshift_logger.propagate
    openshift_logger.propagate = True
    try:
        with (
            _github_client(transport),
            caplog.at_level(logging.WARNING, logger=openshift_logger.name),
        ):
            claims = await _openshift_provider().exchange(
                code="c", code_verifier="", redirect_uri="https://x/cb", nonce=""
            )
    finally:
        openshift_logger.propagate = old_propagate

    assert claims.subject == "u-403"
    assert claims.email is None
    assert any(
        "identity lookup failed" in record.message and "user:info" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


# ---------------------------------------------------------------------------
# Qodo #2 — /login provisions a proxy user so an admin can approve them
# ---------------------------------------------------------------------------


async def test_proxy_login_provisions_pending_account_when_approval_required(
    temp_db_path: Path,
) -> None:
    """GET /login with a proxy header must provision a pending account
    and redirect to /pending, so an administrator can see and approve it."""
    env = dict(_PROXY_ENV, REQUIRE_APPROVAL="true")
    for client in make_app_client(temp_db_path, env):
        resp = client.get(
            "/login", headers={"X-Forwarded-User": "newcomer"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/pending"

        # The account now exists with the right provenance and status.
        user = await storage.get_user_by_username("newcomer")
        assert user is not None
        assert await storage.get_user_status("newcomer") == "pending"

        # Provisioning was audited.
        provisioned = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.proxy_provisioned"
            and row.get("target_id") == "newcomer"
        ]
        assert len(provisioned) == 1
        assert provisioned[0]["details"]["status"] == "pending"


async def test_proxy_login_provisions_active_account_when_approval_not_required(
    temp_db_path: Path,
) -> None:
    """GET /login without REQUIRE_APPROVAL provisions an active account
    and redirects straight to /."""
    for client in make_app_client(temp_db_path, _PROXY_ENV):
        resp = client.get(
            "/login",
            headers={"X-Forwarded-User": "activecomer"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        user = await storage.get_user_by_username("activecomer")
        assert user is not None
        assert await storage.get_user_status("activecomer") == "active"

        provisioned = [
            row
            for row in await _audit_actions(temp_db_path)
            if row["action"] == "auth.proxy_provisioned"
            and row.get("target_id") == "activecomer"
        ]
        assert len(provisioned) == 1
        assert provisioned[0]["details"]["status"] == "active"
