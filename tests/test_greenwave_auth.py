"""Unit tests for the Greenwave auth strategies (issue #183).

The kerberos tests use a FAKE ``spnego`` module (no KDC in CI); the OIDC
tests use ``httpx.MockTransport``. No test ever needs a real Kerberos or
OIDC infrastructure.
"""

import base64
import os
import sys
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from rootcoz.config import Settings
from rootcoz.exporters.base import ExporterPrerequisiteError
from rootcoz.exporters.greenwave_auth import (
    GreenwaveAuthError,
    build_greenwave_auth,
    build_greenwave_waiver_auth,
)
from tests.conftest import (
    failing_spnego_module,
    fake_spnego_module,
    fresh_spnego_record,
)

RESULTSDB_URL = "https://resultsdb.example.com"
TOKEN_URL = "https://sso.example.com/protocol/openid-connect/token"


def _settings(**kwargs: Any) -> Settings:
    """Build Settings with a cleared environment (deterministic defaults)."""
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **kwargs)


def _bearer_settings() -> Settings:
    return _settings(
        greenwave_url=RESULTSDB_URL,
        greenwave_auth_mode="bearer",
        greenwave_api_token=SecretStr("rdb-token"),  # pragma: allowlist secret
    )


def _transport_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _negotiate_handler() -> Any:
    """Handler that challenges once with Negotiate, then answers 200."""
    server_token = base64.b64encode(b"server-challenge").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if "Authorization" not in request.headers:
            return httpx.Response(
                401, headers={"WWW-Authenticate": f"Negotiate {server_token}"}
            )
        return httpx.Response(200, json={})

    return handler


async def _get_with(strategy_auth: httpx.Auth | None, handler) -> httpx.Response:
    async with _transport_client(handler) as client:
        return await client.get(f"{RESULTSDB_URL}/results", auth=strategy_auth)


class TestBearerMode:
    async def test_bearer_header_and_client_kwargs(self):
        strategy = build_greenwave_auth(_bearer_settings())
        assert strategy.mode == "bearer"
        assert strategy.client_kwargs() == {"verify": True}
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        response = await _get_with(strategy.auth, handler)
        assert response.status_code == 200
        assert seen["authorization"] == "Bearer rdb-token"

    async def test_bearer_verify_ssl_false_passed_through(self):
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="bearer",
            greenwave_api_token=SecretStr("rdb-token"),  # pragma: allowlist secret
            greenwave_verify_ssl=False,
        )
        strategy = build_greenwave_auth(settings)
        assert strategy.client_kwargs() == {"verify": False}


class TestBasicMode:
    async def test_sends_basic_header(self):
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="basic",
            greenwave_basic_user="svc-rootcoz",
            greenwave_basic_password=SecretStr(
                "not-a-real-password"
            ),  # pragma: allowlist secret
        )
        strategy = build_greenwave_auth(settings)
        assert strategy.mode == "basic"
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        response = await _get_with(strategy.auth, handler)
        assert response.status_code == 200
        expected = base64.b64encode(b"svc-rootcoz:not-a-real-password").decode()
        assert seen["authorization"] == f"Basic {expected}"


class TestMtlsMode:
    def test_client_kwargs_cert_and_ca_bundle(self):
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="mtls",
            greenwave_client_cert="/certs/client.pem",
            greenwave_client_key="/certs/client.key",
            greenwave_ca_bundle="/certs/ca.pem",
        )
        strategy = build_greenwave_auth(settings)
        assert strategy.mode == "mtls"
        assert strategy.auth is None
        assert strategy.client_kwargs() == {
            "cert": ("/certs/client.pem", "/certs/client.key"),
            "verify": "/certs/ca.pem",
        }

    def test_client_kwargs_without_ca_bundle_uses_verify_ssl(self):
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="mtls",
            greenwave_client_cert="/certs/client.pem",
            greenwave_client_key="/certs/client.key",
        )
        strategy = build_greenwave_auth(settings)
        assert strategy.client_kwargs() == {
            "cert": ("/certs/client.pem", "/certs/client.key"),
            "verify": True,
        }


class TestNoneMode:
    async def test_sends_no_auth_header(self):
        settings = _settings(
            greenwave_url="http://resultsdb.example.com",
            greenwave_auth_mode="none",
        )
        strategy = build_greenwave_auth(settings)
        assert strategy.mode == "none"
        assert strategy.auth is None
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        response = await _get_with(None, handler)
        assert response.status_code == 200
        assert seen["authorization"] == ""


def _oidc_settings() -> Settings:
    return _settings(
        greenwave_url=RESULTSDB_URL,
        greenwave_auth_mode="oidc",
        greenwave_oidc_token_url=TOKEN_URL,
        greenwave_oidc_client_id="rootcoz",
        greenwave_oidc_client_secret=SecretStr(
            "not-a-real-secret"
        ),  # pragma: allowlist secret
    )


def _patched_token_client(handler) -> Any:
    """Route the OIDC flow's internal token client through a MockTransport."""
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    return patch("rootcoz.exporters.greenwave_auth.httpx.AsyncClient", factory)


class TestOidcMode:
    async def test_fetches_token_and_authenticates(self):
        strategy = build_greenwave_auth(_oidc_settings())
        assert strategy.mode == "oidc"
        token_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                token_requests.append(request)
                return httpx.Response(
                    200,
                    json={"access_token": "oidc-token", "expires_in": 3600},
                )
            return httpx.Response(200, json={})

        with _patched_token_client(handler):
            response = await _get_with(strategy.auth, handler)
        assert response.status_code == 200
        assert len(token_requests) == 1
        body = token_requests[0].read().decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=rootcoz" in body
        assert "client_secret=not-a-real-secret" in body
        assert token_requests[0].headers["Accept"] == "application/json"

        seen: dict[str, str] = {}

        def auth_handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        with _patched_token_client(auth_handler):
            response = await _get_with(strategy.auth, auth_handler)
        assert seen["authorization"] == "Bearer oidc-token"

    async def test_token_cached_until_near_expiry(self):
        strategy = build_greenwave_auth(_oidc_settings())
        token_fetches: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                token_fetches.append(1)
                return httpx.Response(
                    200,
                    json={"access_token": "oidc-token", "expires_in": 3600},
                )
            return httpx.Response(200, json={})

        with _patched_token_client(handler):
            for _ in range(3):
                response = await _get_with(strategy.auth, handler)
                assert response.status_code == 200
        # One fetch, three authenticated requests — the cache held.
        assert len(token_fetches) == 1

    async def test_refresh_on_401_retries_once(self):
        strategy = build_greenwave_auth(_oidc_settings())
        token_fetches: list[int] = []
        results_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                token_fetches.append(1)
                token = "oidc-token-new" if len(token_fetches) > 1 else "oidc-token"
                return httpx.Response(
                    200, json={"access_token": token, "expires_in": 3600}
                )
            results_calls.append(request.headers.get("Authorization", ""))
            if len(results_calls) == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={})

        with _patched_token_client(handler):
            response = await _get_with(strategy.auth, handler)
        assert response.status_code == 200
        assert len(token_fetches) == 2  # initial + forced refresh
        assert results_calls == [
            "Bearer oidc-token",
            "Bearer oidc-token-new",
        ]

    async def test_token_response_missing_access_token_is_clear_error(self):
        strategy = build_greenwave_auth(_oidc_settings())

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"nope": True})
            return httpx.Response(200, json={})

        with (
            _patched_token_client(handler),
            pytest.raises(GreenwaveAuthError, match="access_token"),
        ):
            await _get_with(strategy.auth, handler)

    async def test_token_endpoint_http_failure_is_clear_error(self):
        strategy = build_greenwave_auth(_oidc_settings())

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(500, json={})
            return httpx.Response(200, json={})

        with (
            _patched_token_client(handler),
            pytest.raises(GreenwaveAuthError, match="HTTP 500"),
        ):
            await _get_with(strategy.auth, handler)

    async def test_error_messages_never_contain_the_secret(self):
        strategy = build_greenwave_auth(_oidc_settings())

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(401, json={})
            return httpx.Response(200, json={})

        with (
            _patched_token_client(handler),
            pytest.raises(GreenwaveAuthError) as excinfo,
        ):
            await _get_with(strategy.auth, handler)
        assert "not-a-real-secret" not in str(excinfo.value)


def _kerberos_settings() -> Settings:
    return _settings(
        greenwave_url=RESULTSDB_URL,
        greenwave_auth_mode="kerberos",
        greenwave_keytab_path="/secrets/rootcoz.keytab",
        greenwave_keytab_principal="rootcoz@EXAMPLE.COM",
    )


class TestKerberosMode:
    async def test_negotiate_dance_and_spn(self):
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        server_token = base64.b64encode(b"server-challenge").decode()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if len(seen) == 1:
                return httpx.Response(
                    401, headers={"WWW-Authenticate": f"Negotiate {server_token}"}
                )
            return httpx.Response(200, json={})

        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(_kerberos_settings())
            assert strategy.mode == "kerberos"
            # The keytab reaches GSSAPI through the explicit credential, never
            # through a process-global env var: a global KRB5_CLIENT_KTNAME
            # would outlive a mode switch and leak the path into other code.
            assert os.environ.get("KRB5_CLIENT_KTNAME") is None
            response = await _get_with(strategy.auth, handler)

        assert response.status_code == 200
        # First attempt unauthenticated, retry carries the fresh client token.
        expected = base64.b64encode(
            b"client-token-" + base64.b64decode(server_token)
        ).decode()
        assert seen == ["", f"Negotiate {expected}"]
        # The SPN is service/host@realm-from-principal; the keytab and
        # principal reach GSSAPI through the KerberosKeytab credential.
        assert len(record["client_kwargs"]) == 1
        spn_kwargs = record["client_kwargs"][0]
        assert spn_kwargs["protocol"] == "negotiate"
        assert spn_kwargs["service"] == "HTTP"
        assert spn_kwargs["hostname"] == "resultsdb.example.com@EXAMPLE.COM"
        assert record["keytabs"] == [("/secrets/rootcoz.keytab", "rootcoz@EXAMPLE.COM")]

    async def test_spn_targets_the_waiverdb_host_for_waiver_writes(self):
        """A waiver goes to WaiverDB: its SPN must name THAT service.

        The SPN is per-service (HTTP/<host>), so reusing the ResultsDB
        strategy for a WaiverDB write would ask the KDC for a principal that
        does not exist on the waiver host.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_waiver_url="https://waiverdb.example.com/api/v1.0",
            greenwave_auth_mode="kerberos",
            greenwave_keytab_path="/secrets/rootcoz.keytab",
            greenwave_keytab_principal="rootcoz@EXAMPLE.COM",
        )
        with patch.dict(sys.modules, {"spnego": fake}):
            results_auth = build_greenwave_auth(settings)
            waiver_auth = build_greenwave_waiver_auth(settings)
            assert results_auth is not waiver_auth
            await _get_with(results_auth.auth, _negotiate_handler())
            await _get_with(waiver_auth.auth, _negotiate_handler())

        hosts = [kwargs["hostname"] for kwargs in record["client_kwargs"]]
        assert hosts == [
            "resultsdb.example.com@EXAMPLE.COM",
            "waiverdb.example.com@EXAMPLE.COM",
        ]

    async def test_explicit_service_realm_overrides_the_principal_realm(self):
        """GREENWAVE_KRB5_REALM pins the SERVICE realm.

        The realm of the client principal is not necessarily the realm the
        service principal lives in; an explicit knob keeps that decision out
        of the keytab filename/principal.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="kerberos",
            greenwave_keytab_path="/secrets/rootcoz.keytab",
            greenwave_keytab_principal="rootcoz@EXAMPLE.COM",
            greenwave_krb5_realm="SERVICE.EXAMPLE.COM",
        )
        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(settings)
            await _get_with(strategy.auth, _negotiate_handler())

        assert record["client_kwargs"][0]["hostname"] == (
            "resultsdb.example.com@SERVICE.EXAMPLE.COM"
        )

    async def test_no_realm_leaves_the_spn_to_krb5_conf(self):
        """Without any realm the bare host is used (krb5.conf decides).

        Appending a realm that krb5 must then canonicalize is the one case
        where dns_canonicalize_hostname can fight the SPN, so an unset realm
        must not invent one.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="kerberos",
            greenwave_keytab_path="/secrets/rootcoz.keytab",
            greenwave_keytab_principal="rootcoz",
        )
        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(settings)
            await _get_with(strategy.auth, _negotiate_handler())

        assert record["client_kwargs"][0]["hostname"] == "resultsdb.example.com"

    async def test_multi_scheme_challenge_is_understood(self):
        """`Negotiate, Basic realm="x"` must not be read as "no Negotiate"."""
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        server_token = base64.b64encode(b"server-challenge").decode()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if len(seen) == 1:
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            f'Negotiate {server_token}, Basic realm="fallback"'
                        )
                    },
                )
            return httpx.Response(200, json={})

        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(_kerberos_settings())
            response = await _get_with(strategy.auth, handler)

        assert response.status_code == 200
        assert seen[1].startswith("Negotiate ")

    async def test_fresh_context_per_request(self):
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        server_token = base64.b64encode(b"server-challenge").decode()

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.headers.get("Authorization"):
                return httpx.Response(
                    401, headers={"WWW-Authenticate": f"Negotiate {server_token}"}
                )
            return httpx.Response(200, json={})

        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(_kerberos_settings())
            for _ in range(3):
                response = await _get_with(strategy.auth, handler)
                assert response.status_code == 200
        # One NEW security context per request — never reused.
        assert len(record["contexts"]) == 3

    async def test_retried_401_is_a_clear_error(self):
        """A server that keeps challenging past the bound fails clearly.

        A 401 answering a Negotiate token with ANOTHER Negotiate challenge
        is a continuation exchange, so the client keeps stepping the
        context — but a server that never stops challenging is broken, and
        the loop is bounded so it cannot spin forever.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        server_token = base64.b64encode(b"server-challenge").decode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers={"WWW-Authenticate": f"Negotiate {server_token}"}
            )

        with (
            patch.dict(sys.modules, {"spnego": fake}),
            pytest.raises(GreenwaveAuthError, match="exchanges"),
        ):
            strategy = build_greenwave_auth(_kerberos_settings())
            await _get_with(strategy.auth, handler)
        # The bound is 5 exchanges, not an unbounded loop.
        assert len(record["steps"]) == 5

    async def test_continuation_exchange_completes(self):
        """A continuation Negotiate challenge finishes the same context.

        Some servers split the SPNEGO exchange across multiple 401 rounds;
        the client must feed each continuation token back into the SAME
        security context (a context is a multi-step exchange) instead of
        treating the second challenge as a rejection.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)
        first = base64.b64encode(b"challenge-one").decode()
        second = base64.b64encode(b"challenge-two").decode()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if len(seen) == 1:
                return httpx.Response(
                    401, headers={"WWW-Authenticate": f"Negotiate {first}"}
                )
            if len(seen) == 2:
                return httpx.Response(
                    401, headers={"WWW-Authenticate": f"Negotiate {second}"}
                )
            return httpx.Response(200, json={})

        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(_kerberos_settings())
            response = await _get_with(strategy.auth, handler)

        assert response.status_code == 200
        # Both server tokens went through ONE context, in order.
        assert record["steps"] == [b"challenge-one", b"challenge-two"]
        assert len(record["contexts"]) == 1
        # The final request carries the token produced by the last step.
        expected = base64.b64encode(b"client-token-" + b"challenge-two").decode()
        assert seen[-1] == f"Negotiate {expected}"

    async def test_gssapi_error_scrubs_configured_file_paths(self):
        """A GSSAPI error names the keytab FILE — the path must not leak.

        ``GREENWAVE_KEYTAB_PATH`` is a credential location; the sanitized
        error keeps the useful GSSAPI text but replaces every configured
        Greenwave file path with a stable placeholder.
        """
        keytab = "/secrets/rootcoz.keytab"
        cert = "/certs/client.pem"
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="kerberos",
            greenwave_keytab_path=keytab,
            greenwave_keytab_principal="rootcoz@EXAMPLE.COM",
            greenwave_client_cert=cert,
        )
        fake = failing_spnego_module(
            ValueError(f"cannot open keytab {keytab} (cert {cert})")
        )
        with (
            patch.dict(sys.modules, {"spnego": fake}),
            pytest.raises(GreenwaveAuthError) as excinfo,
        ):
            strategy = build_greenwave_auth(settings)
            await _get_with(strategy.auth, _negotiate_handler())
        message = str(excinfo.value)
        assert "cannot open keytab" in message  # useful GSSAPI text kept
        assert "<keytab>" in message
        assert "<client-cert>" in message
        assert keytab not in message
        assert cert not in message

    async def test_spnego_exchange_completed_flag(self):
        """The strategy records whether a Negotiate exchange completed.

        The connection-test message reports "authenticated via kerberos"
        only when the keytab was actually exercised.
        """
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)

        def plain_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        with patch.dict(sys.modules, {"spnego": fake}):
            strategy = build_greenwave_auth(_kerberos_settings())
            # No 401 challenge: the keytab was never exercised.
            await _get_with(strategy.auth, plain_handler)
            assert strategy.spnego_exchange_completed is False
            # A completed challenge flips the flag.
            response = await _get_with(strategy.auth, _negotiate_handler())
        assert response.status_code == 200
        assert strategy.spnego_exchange_completed is True

    async def test_non_negotiate_challenge_is_a_clear_error(self):
        record = fresh_spnego_record()
        fake = fake_spnego_module(record)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, headers={"WWW-Authenticate": "Basic realm=x"})

        with (
            patch.dict(sys.modules, {"spnego": fake}),
            pytest.raises(GreenwaveAuthError, match="Negotiate"),
        ):
            strategy = build_greenwave_auth(_kerberos_settings())
            await _get_with(strategy.auth, handler)

    async def test_missing_pyspnego_is_a_configuration_error(self):
        # sys.modules["spnego"] = None makes `import spnego` raise ImportError.
        with (
            patch.dict(sys.modules, {"spnego": None}),
            pytest.raises(ExporterPrerequisiteError, match="pyspnego"),
        ):
            build_greenwave_auth(_kerberos_settings())


class TestBuildErrors:
    def test_unset_mode_rejected(self):
        with pytest.raises(ExporterPrerequisiteError, match="No Greenwave auth method"):
            build_greenwave_auth(_settings(greenwave_url=RESULTSDB_URL))

    @pytest.mark.parametrize(
        ("mode", "missing"),
        [
            ("bearer", ["greenwave_api_token"]),
            (
                "oidc",
                [
                    "greenwave_oidc_token_url",
                    "greenwave_oidc_client_id",
                    "greenwave_oidc_client_secret",
                ],
            ),
            (
                "kerberos",
                ["greenwave_keytab_path", "greenwave_keytab_principal"],
            ),
            ("mtls", ["greenwave_client_cert", "greenwave_client_key"]),
            ("basic", ["greenwave_basic_user", "greenwave_basic_password"]),
        ],
    )
    def test_missing_mode_fields_rejected(self, mode: str, missing: list[str]):
        settings = _settings(greenwave_url=RESULTSDB_URL, greenwave_auth_mode=mode)
        with pytest.raises(ExporterPrerequisiteError) as excinfo:
            build_greenwave_auth(settings)
        for name in missing:
            assert name in str(excinfo.value)


class TestHttpSchemeRefusedAtBuild:
    """The credentials-over-plain-http rule lives here, not in Settings.

    Settings construction must not raise on a URL-scheme/auth-mode
    mismatch: the URL may come from the environment while the mode lives
    only in the database, and an unvalidatable merged config makes
    ``get_settings()`` fall back to env-only — silently dropping every DB
    override. So the scheme rule is enforced where a credential would
    actually be sent: the auth build (and the settings write path).
    """

    def test_http_url_with_credential_mode_refused(self):
        """Bearer mode over plain HTTP never sends the token — refuse."""
        settings = _settings(
            greenwave_url="http://resultsdb.example.com",
            greenwave_auth_mode="bearer",
            greenwave_api_token=SecretStr("rdb-token"),  # pragma: allowlist secret
        )
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_URL"):
            build_greenwave_auth(settings)

    def test_http_url_allowed_in_none_mode(self):
        """The credential-less none mode may target a public HTTP instance."""
        settings = _settings(
            greenwave_url="http://resultsdb.example.com",
            greenwave_auth_mode="none",
        )
        assert build_greenwave_auth(settings).mode == "none"

    def test_http_waiver_url_refused_even_in_none_mode(self):
        """A waiver lifts a gate — the waiver endpoint stays HTTPS-only."""
        settings = _settings(
            greenwave_url="http://resultsdb.example.com",
            greenwave_waiver_url="http://waiverdb.example.com",
            greenwave_auth_mode="none",
        )
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_WAIVER_URL"):
            build_greenwave_waiver_auth(settings)

    def test_http_oidc_token_url_refused(self):
        """The client secret is POSTed to the token endpoint — HTTPS only."""
        settings = _settings(
            greenwave_url=RESULTSDB_URL,
            greenwave_auth_mode="oidc",
            greenwave_oidc_token_url="http://sso.example.com/token",
            greenwave_oidc_client_id="rootcoz",
            greenwave_oidc_client_secret=SecretStr("sec"),  # pragma: allowlist secret
        )
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_OIDC_TOKEN_URL"):
            build_greenwave_auth(settings)
