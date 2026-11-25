"""Direct unit tests for the deepened auth/identity/SSO/lifecycle modules (#662).

Exercises :mod:`rootcoz.auth.principal`, :mod:`rootcoz.auth.lifecycle`,
:mod:`rootcoz.auth.sso.flows`, :mod:`rootcoz.auth.approval`, and
:mod:`rootcoz.sso_store` directly without booting the FastAPI application.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rootcoz import sso_store, storage
from rootcoz.auth import identity as auth_identity
from rootcoz.auth import lifecycle as auth_lifecycle
from rootcoz.auth import principal as auth_principal
from rootcoz.auth.base import (
    ProxyProvisionedEvent,
    ProxyProvisionRefusedEvent,
)
from rootcoz.auth.sso import flows as auth_sso_flows
from rootcoz.auth.sso.base import AuthError
from rootcoz.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def setup_test_db(temp_db_path: Path) -> AsyncIterator[Path]:
    """Initialize a fresh SQLite DB without booting the FastAPI app."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


def _make_request(
    *,
    method: str = "GET",
    path: str = "/api/results",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    query_string: str = "",
    body: bytes = b"",
) -> Request:
    """Build a bare Starlette Request without booting FastAPI."""
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", b"app.example.test")]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode()))
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string.encode(),
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
        },
        receive=_receive,
    )


def _make_sso_hooks(
    audit_log: list[dict[str, Any]] | None = None,
    json_body: dict[str, Any] | None = None,
) -> auth_sso_flows.SSOFlowHooks:
    """Build in-memory SSOFlowHooks for direct unit testing."""
    recorded = audit_log if audit_log is not None else []

    async def _record_audit(**kwargs: Any) -> None:
        recorded.append(kwargs)

    async def _read_json(_request: Request) -> dict[str, Any]:
        return dict(json_body or {})

    async def _lock_after_failed(
        _username: str, _settings: Settings, _request: Request
    ) -> None:
        return None

    return auth_sso_flows.SSOFlowHooks(
        record_audit=_record_audit,
        notify_pending_count_changed=lambda: None,
        lock_after_failed_login=_lock_after_failed,
        read_json_object=_read_json,
    )


# ---------------------------------------------------------------------------
# 1. Dependency direction & seam hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/rootcoz/storage.py",
        "src/rootcoz/sso_store.py",
    ],
)
def test_storage_layer_never_imports_auth(rel_path: str) -> None:
    """Neither storage.py nor sso_store.py may import from rootcoz.auth."""
    tree = ast.parse((REPO_ROOT / rel_path).read_text())
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("rootcoz.auth"):
                    offending.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("rootcoz.auth"):
                offending.append(mod)
    assert not offending, f"{rel_path} must not import rootcoz.auth: {offending}"


def test_sso_store_never_imports_config() -> None:
    """sso_store.py receives policy callbacks rather than importing config."""
    tree = ast.parse((REPO_ROOT / "src/rootcoz/sso_store.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "rootcoz.config"
        ):
            pytest.fail(f"sso_store.py must not import {node.module}")


# ---------------------------------------------------------------------------
# 2. Public-path contract, typed Principal & grant checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", False),
        ("/login", True),
        ("/login/", True),
        ("/login/device", True),
        ("/docs/", True),
        ("/api/auth/sso/providers", True),
        ("/api/auth/sso/google/login", True),
        ("/api/auth/sso/redhat/callback", True),
        ("/api/auth/sso/google/login/", True),
        ("/api/auth/sso/google/login/extra", False),
        ("/api/auth/sso/device/approve", False),
        ("/api/auth/sso/device/approve/lookup", False),
        ("/api/user/identities", False),
        ("/api/admin/users", False),
    ],
)
def test_explicit_public_path_contract(path: str, expected: bool) -> None:
    assert auth_principal.is_public_path(path) is expected


def test_attach_and_get_principal_roundtrip() -> None:
    req = _make_request()
    principal = auth_principal.Principal(
        username="alice",
        role="operator",
        is_admin=False,
        is_ai_session=True,
        can_view_reports=True,
        can_use_server_providers=True,
        can_use_free_providers=True,
        authenticated=True,
        auth_session_token="tok-123",
        auth_strategy="session",
        auth_provider="redhat",
    )
    auth_principal.attach_principal(req, principal)

    assert auth_principal.get_principal(req) == principal
    # Compatibility mirrors stay synchronized in one place.
    assert req.state.username == "alice"
    assert req.state.role == "operator"
    assert req.state.is_admin is False
    assert req.state.is_ai_session is True
    assert req.state.can_view_reports is True
    assert req.state.can_use_server_providers is True
    assert req.state.can_use_free_providers is True
    assert req.state.auth_session_token == "tok-123"
    assert req.state.auth_strategy == "session"
    assert req.state.auth_provider == "redhat"


def test_grant_guards_enforce_roles_and_allow_list() -> None:
    anon = _make_request()
    auth_principal.attach_principal(anon, auth_principal.ANONYMOUS_PRINCIPAL)
    with pytest.raises(HTTPException) as exc:
        auth_principal.require_authenticated(anon)
    assert exc.value.status_code == 401

    viewer = _make_request()
    auth_principal.attach_principal(
        viewer,
        auth_principal.Principal(username="vicky", role="viewer", authenticated=True),
    )
    with pytest.raises(HTTPException) as exc:
        auth_principal.require_reviewer(viewer)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        auth_principal.require_can_view_reports(viewer)
    assert exc.value.status_code == 403

    reviewer = _make_request()
    auth_principal.attach_principal(
        reviewer,
        auth_principal.Principal(username="rev", role="reviewer", authenticated=True),
    )
    assert auth_principal.require_reviewer(reviewer).username == "rev"
    with pytest.raises(HTTPException) as exc:
        auth_principal.require_operator(reviewer)
    assert exc.value.status_code == 403

    # ALLOWED_USERS blocks off-list non-admins and bypasses admins.
    restricted = Settings(allowed_users="alice,bob")
    with pytest.raises(HTTPException) as exc:
        auth_principal.check_allow_list(reviewer, restricted)
    assert exc.value.status_code == 403

    admin = _make_request()
    auth_principal.attach_principal(
        admin,
        auth_principal.Principal(
            username="boss",
            role="admin",
            is_admin=True,
            can_view_reports=True,
            authenticated=True,
        ),
    )
    assert auth_principal.require_admin(admin).username == "boss"
    assert auth_principal.check_allow_list(admin, restricted).username == "boss"


async def test_authenticate_request_and_sse_revocation_epoch(
    setup_test_db: Path,
) -> None:
    """Direct authenticate_request + live SSE revocation-epoch invalidation."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_admin_user("boss")
        token = await storage.create_session(
            "boss", is_admin=True, role="admin", auth_provider="redhat"
        )
        settings = Settings(secure_cookies=False)
        req = _make_request(cookies={"rootcoz_session": token})

        outcome = await auth_principal.authenticate_request(req, settings)
        assert outcome.disabled_block_reason is None
        assert outcome.principal.authenticated is True
        assert outcome.principal.username == "boss"
        assert outcome.principal.is_admin is True
        assert outcome.principal.auth_strategy == "session"
        assert outcome.principal.auth_provider == "redhat"
        assert outcome.principal.auth_session_token == token

        auth_principal.attach_principal(req, outcome.principal)

        check = auth_principal.make_admin_sse_auth_check(
            req,
            authorize_fn=lambda r: auth_principal.is_admin_sse_authorized(
                r, settings_getter=lambda: settings
            ),
            recheck_interval=60.0,
        )
        assert await check() is True

        # Demoting bumps auth_revocation_version so the SSE check immediately
        # re-queries SQLite instead of waiting 60s.
        await storage.change_user_role("boss", "reviewer")
        assert await check() is False


async def test_authenticate_request_proxy_emits_typed_side_effects(
    setup_test_db: Path,
) -> None:
    """ProxyStrategy emits typed AuthSideEffects rather than mutating request.state."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        settings = Settings(
            trust_proxy_headers=True,
            require_approval=True,
            allowed_users="alice",
        )
        # Allowed new proxy user -> typed ProxyProvisionedEvent
        req_ok = _make_request(headers={"x-forwarded-user": "alice"})
        outcome_ok = await auth_principal.authenticate_request(req_ok, settings)
        assert outcome_ok.principal.authenticated is True
        assert outcome_ok.side_effects.set_proxy_cookie == "alice"
        assert outcome_ok.side_effects.proxy_provisioned == ProxyProvisionedEvent(
            username="alice", status="pending", role=settings.default_user_role
        )
        assert not hasattr(req_ok.state, "proxy_provisioned")

        # Off-list proxy user -> typed ProxyProvisionRefusedEvent
        req_refused = _make_request(headers={"x-forwarded-user": "mallory"})
        outcome_ref = await auth_principal.authenticate_request(req_refused, settings)
        assert outcome_ref.principal.authenticated is False
        assert outcome_ref.side_effects.proxy_provision_refused == (
            ProxyProvisionRefusedEvent(username="mallory", reason="not_allowed")
        )
        assert not hasattr(req_refused.state, "proxy_provision_refused")


# ---------------------------------------------------------------------------
# 3. Atomic user deletion/revocation lifecycle
# ---------------------------------------------------------------------------


async def test_delete_user_atomic_ordering_and_cleanup(setup_test_db: Path) -> None:
    """Full atomic purge sequence executes in strict order and bumps revocation."""
    events: list[str] = []

    @asynccontextmanager
    async def _lock(scope: str) -> AsyncIterator[None]:
        events.append(f"lock:{scope}")
        try:
            yield
        finally:
            events.append(f"unlock:{scope}")

    hooks = auth_lifecycle.UserDeleteHooks(
        hold_scope_lock=_lock,
        write_owner_delete_marker=AsyncMock(
            side_effect=lambda oid: events.append(f"marker_write:{oid}") or True
        ),
        remove_owner_delete_marker=AsyncMock(
            side_effect=lambda oid: events.append(f"marker_remove:{oid}")
        ),
        cleanup_owner_attachments=AsyncMock(
            side_effect=lambda oid: events.append(f"cleanup_attach:{oid}")
        ),
        cleanup_admin_artifacts=AsyncMock(
            side_effect=lambda u, oid: events.append(f"cleanup_artifacts:{u}:{oid}")
        ),
        notify_pending_count_changed=lambda: events.append("notify_pending"),
        cleanup_revoked_ai_sessions=AsyncMock(
            side_effect=lambda: events.append("cleanup_ai")
        ),
    )

    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")
        await storage.add_user_identity("alice", "redhat", "sub-alice")
        row = await storage.get_user_by_username("alice")
        assert row is not None
        owner_id = int(row["id"])
        rev_before = storage.get_auth_revocation_version()

        # Self-delete refused
        with pytest.raises(HTTPException) as exc:
            await auth_lifecycle.delete_user_atomic(
                "alice", actor_username="alice", hooks=hooks
            )
        assert exc.value.status_code == 400

        result = await auth_lifecycle.delete_user_atomic(
            "alice", actor_username="admin", hooks=hooks
        )
        assert result.username == "alice"
        assert result.owner_id == owner_id
        assert storage.get_auth_revocation_version() > rev_before
        assert await storage.get_user_by_username("alice") is None
        assert await storage.get_user_identity("redhat", "sub-alice") is None

    assert events == [
        f"lock:owner:{owner_id}",
        f"marker_write:{owner_id}",
        f"cleanup_attach:{owner_id}",
        f"cleanup_artifacts:alice:{owner_id}",
        f"unlock:owner:{owner_id}",
        "notify_pending",
        "cleanup_ai",
    ]


async def test_delete_user_atomic_rolls_back_marker_on_error(
    setup_test_db: Path,
) -> None:
    """When storage.delete_user fails while row still exists, the marker is removed."""
    removed: list[int] = []

    @asynccontextmanager
    async def _lock(_scope: str) -> AsyncIterator[None]:
        yield

    hooks = auth_lifecycle.UserDeleteHooks(
        hold_scope_lock=_lock,
        write_owner_delete_marker=AsyncMock(return_value=True),
        remove_owner_delete_marker=AsyncMock(
            side_effect=lambda oid: removed.append(oid)
        ),
        cleanup_owner_attachments=AsyncMock(),
        cleanup_admin_artifacts=AsyncMock(),
        notify_pending_count_changed=lambda: None,
        cleanup_revoked_ai_sessions=AsyncMock(),
    )

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch.object(storage, "delete_user", side_effect=ValueError("protected")),
    ):
        await storage.create_user("alice")
        row = await storage.get_user_by_username("alice")
        assert row is not None
        with pytest.raises(HTTPException) as exc:
            await auth_lifecycle.delete_user_atomic(
                "alice", actor_username="admin", hooks=hooks
            )
        assert exc.value.status_code == 400
        assert removed == [int(row["id"])]


# ---------------------------------------------------------------------------
# 4. Typed storage exceptions & DeviceCodeExchangeResult
# ---------------------------------------------------------------------------


async def test_typed_storage_errors_and_identity_translation(
    setup_test_db: Path,
) -> None:
    """sso_store raises concrete typed exceptions consumed without string parsing."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_user("alice")

        with pytest.raises(sso_store.BootstrapAdminLinkError) as exc:
            await storage.add_user_identity("admin", "google", "sub-admin")
        assert exc.value.reason == "bootstrap_admin_cannot_link"

        with pytest.raises(sso_store.AccountNotFoundError) as exc:
            await storage.add_user_identity("ghost", "google", "sub-ghost")
        assert exc.value.reason == "account_missing"

        with pytest.raises(sso_store.TicketExpiredError) as exc:
            await storage.add_user_identity(
                "alice", "google", "sub-1", ticket="no-such-ticket"
            )
        assert exc.value.reason == "ticket_expired"

        # auth.identity.link_existing maps typed AccountNotFoundError -> AuthError
        with pytest.raises(AuthError) as auth_exc:
            await auth_identity.link_existing(
                username="ghost", provider="google", subject="sub-ghost", email=None
            )
        assert auth_exc.value.reason == "account_missing"

        # auth.identity.create_new maps UsernameTakenError -> invalid_username
        with pytest.raises(AuthError) as auth_exc2:
            await auth_identity.create_new(
                provider="google",
                subject="sub-2",
                email="a@example.com",
                username="alice",
                settings=Settings(),
            )
        assert auth_exc2.value.reason == "invalid_username"


# ---------------------------------------------------------------------------
# 5. Deep SSO flow orchestration without FastAPI routes
# ---------------------------------------------------------------------------


async def test_sso_flows_identities_payload_and_unlink_guards(
    setup_test_db: Path,
) -> None:
    """identities_payload + unlink_identity_flow enforce keyless sole-SSO 409."""
    payload_keyless = auth_sso_flows.identities_payload(
        [{"provider": "redhat", "email": "a@example.com", "created_at": "2026-01-01"}],
        has_api_key=False,
    )
    assert payload_keyless["unlinkable"] is False
    assert payload_keyless["identities"][0]["display_name"] == "Red Hat SSO"

    payload_keyed = auth_sso_flows.identities_payload(
        [{"provider": "redhat", "email": "a@example.com", "created_at": "2026-01-01"}],
        has_api_key=True,
    )
    assert payload_keyed["unlinkable"] is True

    audit_log: list[dict[str, Any]] = []
    hooks = _make_sso_hooks(audit_log)
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_keyless_user(
            "keyless", status="active", created_via="redhat"
        )
        await storage.add_user_identity("keyless", "redhat", "sub-k")

        req = _make_request(method="DELETE", path="/api/user/identities/redhat")
        auth_principal.attach_principal(
            req,
            auth_principal.Principal(
                username="keyless", role="reviewer", authenticated=True
            ),
        )
        with pytest.raises(HTTPException) as exc:
            await auth_sso_flows.unlink_identity_flow(
                req,
                "keyless",
                "redhat",
                audit_action="auth.identity_unlinked",
                as_admin=False,
                hooks=hooks,
            )
        assert exc.value.status_code == 409
        assert audit_log[-1]["action"] == "auth.identity_unlink_refused"
        assert audit_log[-1]["details"]["reason"] == "last_method"


async def test_sso_start_flow_refuses_bootstrap_admin_and_cross_site(
    setup_test_db: Path,
) -> None:
    """sso_start_flow enforces same-origin POST and bootstrap admin 400 directly."""
    audit_log: list[dict[str, Any]] = []
    hooks = _make_sso_hooks(audit_log)
    settings = Settings(
        public_base_url="https://app.example.test",
        sso_google_client_id="cid",
        sso_google_client_secret="csec",  # pragma: allowlist secret
    )
    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch("rootcoz.auth.sso.flows.get_settings", return_value=settings),
    ):
        admin_session = await storage.create_session(
            "admin", is_admin=True, role="admin"
        )
        # Cross-site POST refused before session check
        cross_req = _make_request(
            method="POST",
            path="/api/auth/sso/google/login",
            cookies={"rootcoz_session": admin_session},
            headers={"sec-fetch-site": "cross-site"},
        )
        res = await auth_sso_flows.sso_start_flow(
            cross_req, "google", "link", hooks=hooks
        )
        assert res.status_code == 303
        assert "sso_error=link_cross_site" in res.headers["location"]

        # Same-origin POST by bootstrap admin refused with 400
        admin_req = _make_request(
            method="POST",
            path="/api/auth/sso/google/login",
            cookies={"rootcoz_session": admin_session},
            headers={"sec-fetch-site": "same-origin"},
        )
        with pytest.raises(HTTPException) as exc:
            await auth_sso_flows.sso_start_flow(
                admin_req, "google", "link", hooks=hooks
            )
        assert exc.value.status_code == 400
        assert audit_log[-1]["details"]["reason"] == "bootstrap_admin_cannot_link"


async def test_device_approve_flow_uses_principal_provider_and_origin_guard(
    setup_test_db: Path,
) -> None:
    """Device approval checks origin before body read and stamps Principal.auth_provider."""
    audit_log: list[dict[str, Any]] = []
    settings = Settings(public_base_url="https://app.example.test")

    with (
        patch.object(storage, "DB_PATH", setup_test_db),
        patch("rootcoz.auth.sso.flows.get_settings", return_value=settings),
    ):
        await storage.create_user("alice")
        code = await storage.create_device_code(ttl_seconds=300, interval_seconds=5)

        # Ambient session cookie without same-origin proof -> 403 before reading body
        cross_req = _make_request(
            method="POST",
            path="/api/auth/sso/device/approve",
            headers={"sec-fetch-site": "cross-site"},
        )
        auth_principal.attach_principal(
            cross_req,
            auth_principal.Principal(
                username="alice",
                role="reviewer",
                authenticated=True,
                auth_strategy="session",
                auth_provider="redhat",
            ),
        )
        body_reader = AsyncMock(return_value={"user_code": code["user_code"]})
        hooks_cross = auth_sso_flows.SSOFlowHooks(
            record_audit=AsyncMock(side_effect=lambda **kw: audit_log.append(kw)),
            notify_pending_count_changed=lambda: None,
            lock_after_failed_login=AsyncMock(return_value=None),
            read_json_object=body_reader,
        )
        with pytest.raises(HTTPException) as exc:
            await auth_sso_flows.sso_device_approve_flow(cross_req, hooks=hooks_cross)
        assert exc.value.status_code == 403
        body_reader.assert_not_awaited()

        # Bearer credential skips origin check and stamps auth_provider=None
        bearer_req = _make_request(method="POST", path="/api/auth/sso/device/approve")
        auth_principal.attach_principal(
            bearer_req,
            auth_principal.Principal(
                username="alice",
                role="reviewer",
                authenticated=True,
                auth_strategy="api_key",
                auth_provider=None,
            ),
        )
        hooks_ok = _make_sso_hooks(
            audit_log, json_body={"user_code": code["user_code"]}
        )
        resp = await auth_sso_flows.sso_device_approve_flow(bearer_req, hooks=hooks_ok)
        assert resp.status_code == 200
        assert audit_log[-1]["action"] == "auth.sso_device_approved"
        assert audit_log[-1]["details"]["provider"] is None


async def test_idp_logout_url_reasons() -> None:
    """idp_logout_url returns explicit audit reasons across all branches."""
    settings = Settings(public_base_url="")
    assert await auth_sso_flows.idp_logout_url(None, settings) == (None, "not_sso")
    assert await auth_sso_flows.idp_logout_url("redhat", settings) == (
        None,
        "no_public_base_url",
    )
    settings_pub = Settings(public_base_url="https://app.example.test")
    assert await auth_sso_flows.idp_logout_url("redhat", settings_pub) == (
        None,
        "provider_unavailable",
    )
