"""Tests for the auth strategy chain (``rootcoz.auth.registry``).

Stage 1 of issue #197 extracts the strategies out of ``AuthMiddleware``; these
tests pin the plugin contract (order, ``is_applicable`` cost, precedence) and
the middleware behaviours that must survive the extraction. The display-only
``rootcoz_username`` cookie is pinned in ``tests/test_auth.py`` — it is not a
chain member and must never become one.
"""

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from rootcoz import storage
from rootcoz.auth import registry
from rootcoz.auth.api_key import ApiKeyStrategy
from rootcoz.auth.base import AuthContext, AuthIdentity, matches_bootstrap_admin
from rootcoz.auth.proxy import ProxyStrategy
from rootcoz.auth.session import SessionStrategy
from rootcoz.config import Settings, get_settings
from tests.conftest import build_test_env, make_app_client

#: The refusal body, spelled out on purpose: importing
#: ``auth.approval.UNAUTHENTICATED_DETAIL`` would make this assertion true by
#: construction and let a leaking message slip through. ``tests/test_audit.py``
#: pins the same literal from its side.
_GENERIC_401 = {"detail": "Authentication required. Please log in with your API key."}


def _request(
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a bare GET request with the given credentials."""
    raw_headers = [(b"host", b"testserver")]
    if cookies:
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        raw_headers.append((b"cookie", cookie_value.encode()))
    raw_headers.extend(
        (name.lower().encode(), value.encode())
        for name, value in (headers or {}).items()
    )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/me",
            "query_string": b"",
            "headers": raw_headers,
        }
    )


@pytest.fixture
def auth_settings(temp_db_path: Path) -> Iterator[Settings]:
    """Settings from the shared test baseline; the chain reads no DB here."""
    env = build_test_env(SECURE_COOKIES="false", DB_PATH=str(temp_db_path))
    with patch.dict(os.environ, env, clear=True):
        get_settings.cache_clear()
        yield get_settings()
    get_settings.cache_clear()


def _register(client, username: str) -> str:
    """Register an active user and return its API key."""
    response = client.post("/api/auth/register", json={"username": username})
    assert response.status_code == 200, response.text
    return response.json()["api_key"]


def _expire_soon(username: str) -> None:
    """Move a session inside the renewal window (under half its TTL)."""
    soon = (datetime.now(UTC) + timedelta(hours=1)).strftime(
        storage.SQL_TIMESTAMP_FORMAT
    )
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE username = ?", (soon, username)
        )


def _disable(username: str) -> None:
    """Take an account out of service (#379)."""
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute("UPDATE users SET disabled = 1 WHERE username = ?", (username,))


def test_the_chain_runs_session_then_api_key_then_proxy() -> None:
    """The chain order is declared once, in ``AUTH_CHAIN``, and never reordered."""
    assert registry.AUTH_CHAIN == (SessionStrategy, ApiKeyStrategy, ProxyStrategy)
    assert [strategy.name for strategy in registry.AUTH_CHAIN] == [
        "session",
        "api_key",
        "proxy",
    ]
    assert [strategy.name for strategy in registry.build_chain()] == [
        "session",
        "api_key",
        "proxy",
    ]
    # The override policy assumes the session is probed before the key; a
    # reorder would make the pair unreachable and silently change precedence.
    assert registry.AUTH_CHAIN.index(SessionStrategy) < registry.AUTH_CHAIN.index(
        ApiKeyStrategy
    )


async def test_a_credential_free_request_touches_no_database(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_applicable`` stays a header/cookie check — every request pays for it."""
    probes = {
        name: AsyncMock(return_value=None)
        for name in (
            "get_session",
            "renew_session",
            "get_user_by_key",
            "get_user_by_username",
        )
    }
    for name, spy in probes.items():
        monkeypatch.setattr(storage, name, spy)

    request = _request()
    ctx = AuthContext(request=request, settings=auth_settings)
    for strategy in registry.build_chain():
        assert strategy.is_applicable(ctx) is False, strategy.name

    assert await registry.resolve_identity(request, auth_settings) is None
    assert [name for name, spy in probes.items() if spy.await_count] == []


async def test_a_non_admin_session_is_overridden_by_a_bearer_key(
    temp_db_path: Path,
) -> None:
    """A key is an equally explicit credential, so it may replace a session."""
    for client in make_app_client(temp_db_path):
        api_key = _register(client, "alice")
        session_token = await storage.create_session(
            "alice", is_admin=False, role="reviewer"
        )
        settings = get_settings()

        session_identity = await registry.resolve_identity(
            _request(cookies={"rootcoz_session": session_token}), settings
        )
        assert session_identity is not None
        assert session_identity.strategy_name == "session"

        identity = await registry.resolve_identity(
            _request(
                cookies={"rootcoz_session": session_token},
                headers={"authorization": f"Bearer {api_key}"},
            ),
            settings,
        )
        assert identity is not None
        assert identity.strategy_name == "api_key"
        assert identity.username == "alice"


async def test_the_bearer_key_names_the_caller_and_not_the_session_user(
    temp_db_path: Path,
) -> None:
    """Session for one user + another user's key resolves as the key's owner.

    Preserved from the pre-refactor middleware, where the Bearer branch ran
    whenever the session was not an administrator. The caller holds both
    credentials, so it is not an escalation — but the identity the request
    carries is the key's, and this is what pins that.
    """
    for client in make_app_client(temp_db_path):
        alice_key = _register(client, "alice")
        bob_key = _register(client, "bob")
        alice_session = await storage.create_session(
            "alice", is_admin=False, role="reviewer"
        )

        identity = await registry.resolve_identity(
            _request(
                cookies={"rootcoz_session": alice_session},
                headers={"authorization": f"Bearer {bob_key}"},
            ),
            get_settings(),
        )
        assert identity is not None
        assert identity.strategy_name == "api_key"
        assert identity.username == "bob"

        # Alice's own key still resolves her, so the test is about the key.
        alice_identity = await registry.resolve_identity(
            _request(
                cookies={"rootcoz_session": alice_session},
                headers={"authorization": f"Bearer {alice_key}"},
            ),
            get_settings(),
        )
        assert alice_identity is not None
        assert alice_identity.username == "alice"


async def test_a_bearer_credential_that_cannot_authenticate_keeps_the_session(
    temp_db_path: Path,
) -> None:
    """A strategy returning ``None`` never clears what an earlier one resolved."""
    for _client in make_app_client(temp_db_path):
        session_token = await storage.create_session(
            "alice", is_admin=False, role="reviewer"
        )

        identity = await registry.resolve_identity(
            _request(
                cookies={"rootcoz_session": session_token},
                headers={"authorization": "Bearer not-a-key-anybody-has"},
            ),
            get_settings(),
        )
        assert identity is not None
        assert identity.strategy_name == "session"
        assert identity.username == "alice"


async def test_a_renewal_token_is_reissued_even_when_a_bearer_key_wins(
    temp_db_path: Path,
) -> None:
    """The refreshed cookie must go out whoever wins, or a live session dies."""
    for client in make_app_client(temp_db_path):
        api_key = _register(client, "alice")
        session_token = await storage.create_session(
            "alice", is_admin=False, role="reviewer"
        )
        _expire_soon("alice")

        request = _request(
            cookies={"rootcoz_session": session_token},
            headers={"authorization": f"Bearer {api_key}"},
        )
        identity = await registry.resolve_identity(request, get_settings())

        assert identity is not None
        assert identity.strategy_name == "api_key"
        assert request.state.renew_session_token == session_token


async def test_an_admin_session_is_final(temp_db_path: Path) -> None:
    """An admin session is not replaced by a Bearer key, admin or not."""
    for client in make_app_client(temp_db_path):
        api_key = _register(client, "bob")
        admin_session = await storage.create_session(
            "admin", is_admin=True, role="admin"
        )

        identity = await registry.resolve_identity(
            _request(
                cookies={"rootcoz_session": admin_session},
                headers={"authorization": f"Bearer {api_key}"},
            ),
            get_settings(),
        )
        assert identity is not None
        assert identity.strategy_name == "session"
        assert identity.username == "admin"
        assert identity.is_admin is True


def test_the_bootstrap_check_never_raises_on_a_non_ascii_secret(
    auth_settings: Settings,
) -> None:
    """A non-ASCII credential or ADMIN_KEY answers "no match", never a 500."""
    settings = auth_settings.model_copy(update={"admin_key": "admín-key"})
    assert matches_bootstrap_admin("admín-key", settings) is True
    assert matches_bootstrap_admin("é", settings) is False


def test_the_override_policy_is_stated_in_one_place() -> None:
    """Only a Bearer key may take over an identity, and never an admin's."""
    session = AuthIdentity(
        username="alice", strategy_name="session", is_admin=False, role="reviewer"
    )
    admin_session = AuthIdentity(
        username="admin", strategy_name="session", is_admin=True, role="admin"
    )
    assert registry.may_replace(None, SessionStrategy()) is True
    assert registry.may_replace(session, ApiKeyStrategy()) is True
    assert registry.may_replace(session, ProxyStrategy()) is False
    assert registry.may_replace(admin_session, ApiKeyStrategy()) is False
    assert registry.may_replace(admin_session, ProxyStrategy()) is False


async def test_the_proxy_header_never_replaces_another_identity(
    temp_db_path: Path,
) -> None:
    """The trusted header applies only when nothing else authenticated."""
    for _client in make_app_client(temp_db_path, {"TRUST_PROXY_HEADERS": "true"}):
        session_token = await storage.create_session(
            "carol", is_admin=False, role="reviewer"
        )
        settings = get_settings()
        request = _request(
            cookies={"rootcoz_session": session_token},
            headers={"x-forwarded-user": "mallory"},
        )
        ctx = AuthContext(request=request, settings=settings)
        current = AuthIdentity(
            username="carol",
            strategy_name="session",
            is_admin=False,
            role="reviewer",
        )
        # The proxy does hold a credential here; the policy is what keeps the
        # session identity, so the rule under test is may_replace.
        assert ProxyStrategy().is_applicable(ctx) is True
        assert registry.may_replace(current, ProxyStrategy()) is False

        identity = await registry.resolve_identity(request, settings)
        assert identity is not None
        assert identity.strategy_name == "session"
        assert identity.username == "carol"

        # The reserved bootstrap admin is not resolvable from a header.
        assert (
            await registry.resolve_identity(
                _request(headers={"x-forwarded-user": "admin"}), settings
            )
            is None
        )


def test_needs_key_echoes_the_normalised_proxy_username() -> None:
    """The proxy name is lower-cased everywhere, including this echo."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "results.db"
        for client in make_app_client(db_path, {"TRUST_PROXY_HEADERS": "true"}):
            response = client.get(
                "/api/auth/needs-key", headers={"X-Forwarded-User": "SomeUser"}
            )
            assert response.status_code == 200
            assert response.json() == {"needs_key": False, "username": "someuser"}


@pytest.mark.parametrize("credential", ["session", "bearer", "proxy"])
async def test_a_disabled_account_is_refused_however_it_authenticated(
    temp_db_path: Path, credential: str
) -> None:
    """A key, a session and a proxy name are only as good as the row they name."""
    env = {"TRUST_PROXY_HEADERS": "true"} if credential == "proxy" else None
    for client in make_app_client(temp_db_path, env):
        api_key = _register(client, "dave")
        _disable("dave")

        client.cookies.clear()
        if credential == "session":
            session_token = await storage.create_session(
                "dave", is_admin=False, role="reviewer"
            )
            client.cookies.set("rootcoz_session", session_token)
            response = client.get("/api/auth/me")
        elif credential == "bearer":
            response = client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {api_key}"}
            )
        else:
            response = client.get("/api/auth/me", headers={"X-Forwarded-User": "dave"})

        assert response.status_code == 401
        assert response.json() == _GENERIC_401
