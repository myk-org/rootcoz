"""Tests for admin approval of new user registrations (issue #54)."""

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.config import get_settings


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client_approval_on(_init_db, temp_db_path):
    """Create a test client with REQUIRE_APPROVAL=true."""
    with patch.dict(
        os.environ,
        {
            "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
            "REQUIRE_APPROVAL": "true",
        },
    ):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from rootcoz.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


@pytest.fixture
def client_approval_off(_init_db, temp_db_path):
    """Create a test client with REQUIRE_APPROVAL=false (backward compat)."""
    with patch.dict(
        os.environ,
        {
            "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
            "REQUIRE_APPROVAL": "false",
        },
    ):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from rootcoz.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


def _admin_headers():
    """Return admin auth headers."""
    return {
        "Authorization": "Bearer test-admin-key-16chars"
    }  # pragma: allowlist secret


class TestRegisterWithApproval:
    """Test registration when REQUIRE_APPROVAL is True."""

    def test_register_creates_pending_user(self, client_approval_on):
        """Registering with REQUIRE_APPROVAL=true creates a pending user."""
        resp = client_approval_on.post(
            "/api/auth/register", json={"username": "newuser01"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["username"] == "newuser01"
        assert "api_key" in data
        assert "awaiting admin approval" in data["message"].lower()

    def test_pending_user_cannot_access_protected_endpoints(self, client_approval_on):
        """A pending user should get 403 on protected endpoints."""
        # Register a user (pending)
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "penduser1"}
        )
        assert reg.status_code == 200
        api_key = reg.json()["api_key"]

        # Try to access a protected endpoint with the user's API key
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        assert "awaiting admin approval" in resp.json()["detail"].lower()

    def test_admin_can_list_pending_users(self, client_approval_on):
        """Admin can list pending users."""
        # Register two users
        client_approval_on.post("/api/auth/register", json={"username": "penduser2"})
        client_approval_on.post("/api/auth/register", json={"username": "penduser3"})

        resp = client_approval_on.get(
            "/api/admin/users/pending",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        users = resp.json()["users"]
        usernames = [u["username"] for u in users]
        assert "penduser2" in usernames
        assert "penduser3" in usernames

    def test_admin_can_approve_user(self, client_approval_on):
        """Admin approves a pending user, user becomes active."""
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "toapprove"}
        )
        api_key = reg.json()["api_key"]

        # Approve
        resp = client_approval_on.post(
            "/api/admin/users/toapprove/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

        # Now the user can access protected endpoints
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

    def test_admin_can_reject_user(self, client_approval_on):
        """Admin rejects a pending user."""
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "toreject1"}
        )
        api_key = reg.json()["api_key"]

        # Reject
        resp = client_approval_on.post(
            "/api/admin/users/toreject1/reject",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        # Rejected user still cannot access protected endpoints
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        assert "rejected" in resp.json()["detail"].lower()

    def test_approve_nonexistent_user_returns_404(self, client_approval_on):
        """Approving a non-existent user returns 404."""
        resp = client_approval_on.post(
            "/api/admin/users/ghostuser/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 404

    def test_approve_already_active_user_returns_400(self, client_approval_on):
        """Approving an already active user returns 400."""
        # Register and approve
        client_approval_on.post("/api/auth/register", json={"username": "activeone"})
        client_approval_on.post(
            "/api/admin/users/activeone/approve",
            headers=_admin_headers(),
        )
        # Try to approve again
        resp = client_approval_on.post(
            "/api/admin/users/activeone/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 400
        assert "not pending" in resp.json()["detail"].lower()

    def test_reject_already_active_user_returns_400(self, client_approval_on):
        """Rejecting an already active user returns 400."""
        client_approval_on.post("/api/auth/register", json={"username": "activetwo"})
        client_approval_on.post(
            "/api/admin/users/activetwo/approve",
            headers=_admin_headers(),
        )
        resp = client_approval_on.post(
            "/api/admin/users/activetwo/reject",
            headers=_admin_headers(),
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_approve(self, client_approval_on):
        """Non-admin users cannot access approve endpoint."""
        resp = client_approval_on.post(
            "/api/admin/users/someuser/approve",
        )
        # Should get 401 (no auth) or 403 (not admin)
        assert resp.status_code in (401, 403)

    def test_non_admin_cannot_list_pending(self, client_approval_on):
        """Non-admin users cannot list pending users."""
        resp = client_approval_on.get("/api/admin/users/pending")
        assert resp.status_code in (401, 403)

    def test_admin_bypasses_pending_check(self, client_approval_on):
        """Admin (bootstrap) always bypasses pending user check."""
        resp = client_approval_on.get(
            "/api/dashboard",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200


class TestRegisterWithoutApproval:
    """Test registration when REQUIRE_APPROVAL is False (backward compat)."""

    def test_register_creates_active_user(self, client_approval_off):
        """Registering with REQUIRE_APPROVAL=false creates an active user."""
        resp = client_approval_off.post(
            "/api/auth/register", json={"username": "freeuser1"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["username"] == "freeuser1"
        assert "api_key" in data

    def test_active_user_can_access_protected_endpoints(self, client_approval_off):
        """Active user (no approval required) can access protected endpoints."""
        reg = client_approval_off.post(
            "/api/auth/register", json={"username": "freeuser2"}
        )
        api_key = reg.json()["api_key"]

        resp = client_approval_off.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200


class TestStorageFunctions:
    """Test storage-level functions for user status management."""

    def test_register_user_with_status(self, _init_db, temp_db_path):
        """register_user_with_status creates a user with the given status."""
        with (
            patch.dict(
                os.environ,
                {
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
                },  # pragma: allowlist secret
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            row_id = asyncio.run(
                storage.register_user_with_status(
                    "statususer", "fake_hash_abc", status="pending"
                )
            )
            assert row_id > 0
            status = asyncio.run(storage.get_user_status("statususer"))
            assert status == "pending"

    def test_set_user_status(self, _init_db, temp_db_path):
        """set_user_status changes user status."""
        with (
            patch.dict(
                os.environ,
                {
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
                },  # pragma: allowlist secret
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            asyncio.run(
                storage.register_user_with_status(
                    "statuschange", "fake_hash_def", status="pending"
                )
            )
            result = asyncio.run(storage.set_user_status("statuschange", "active"))
            assert result is True
            status = asyncio.run(storage.get_user_status("statuschange"))
            assert status == "active"

    def test_set_user_status_invalid(self, _init_db, temp_db_path):
        """set_user_status rejects invalid status values."""
        with (
            patch.dict(
                os.environ,
                {
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
                },  # pragma: allowlist secret
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
            pytest.raises(ValueError, match="Invalid status"),
        ):
            asyncio.run(storage.set_user_status("anyone", "bogus"))

    def test_get_user_status_nonexistent(self, _init_db, temp_db_path):
        """get_user_status returns None for non-existent user."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            status = asyncio.run(storage.get_user_status("noone"))
            assert status is None

    def test_list_pending_users(self, _init_db, temp_db_path):
        """list_pending_users returns only pending users."""
        with (
            patch.dict(
                os.environ,
                {
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
                },  # pragma: allowlist secret
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            asyncio.run(
                storage.register_user_with_status("pend01", "hash01", status="pending")
            )
            asyncio.run(
                storage.register_user_with_status("active01", "hash02", status="active")
            )
            asyncio.run(
                storage.register_user_with_status("pend02", "hash03", status="pending")
            )
            pending = asyncio.run(storage.list_pending_users())
            usernames = [u["username"] for u in pending]
            assert "pend01" in usernames
            assert "pend02" in usernames
            assert "active01" not in usernames

    def test_existing_users_default_to_active(self, _init_db, temp_db_path):
        """Users created before the migration default to 'active' status."""
        with (
            patch.dict(
                os.environ,
                {
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
                },  # pragma: allowlist secret
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            asyncio.run(storage.create_user("legacyuser"))
            status = asyncio.run(storage.get_user_status("legacyuser"))
            assert status == "active"


class TestAdminWaitApproveMsg:
    """Tests for the ADMIN_WAIT_APPROVE_MSG feature (issue #90)."""

    @pytest.fixture
    def client_with_custom_msg(self, _init_db, temp_db_path):
        """Create a test client with ADMIN_WAIT_APPROVE_MSG set."""
        with patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "REQUIRE_APPROVAL": "true",
                "ADMIN_WAIT_APPROVE_MSG": "Contact @admin in Slack",
            },
        ):
            get_settings.cache_clear()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from rootcoz.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    @pytest.fixture
    def client_without_custom_msg(self, _init_db, temp_db_path):
        """Create a test client without ADMIN_WAIT_APPROVE_MSG."""
        with patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "REQUIRE_APPROVAL": "true",
                "ADMIN_WAIT_APPROVE_MSG": "",
            },
        ):
            get_settings.cache_clear()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from rootcoz.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    def test_pending_status_returns_custom_message(self, client_with_custom_msg):
        """GET /api/auth/pending-status includes custom_message when ADMIN_WAIT_APPROVE_MSG is set."""
        resp = client_with_custom_msg.get("/api/auth/pending-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["custom_message"] == "Contact @admin in Slack"

    def test_pending_status_omits_custom_message_when_unset(
        self, client_without_custom_msg
    ):
        """GET /api/auth/pending-status omits custom_message when ADMIN_WAIT_APPROVE_MSG is empty."""
        resp = client_without_custom_msg.get("/api/auth/pending-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert "custom_message" not in data

    def test_blocked_user_response_includes_custom_message_for_pending(
        self, client_with_custom_msg
    ):
        """A pending user gets 403 with custom_message from _blocked_user_status_response."""
        # Register a user (becomes pending because REQUIRE_APPROVAL=true)
        reg = client_with_custom_msg.post(
            "/api/auth/register", json={"username": "custmsguser"}
        )
        assert reg.status_code == 200
        api_key = reg.json()["api_key"]

        # Try to access a protected endpoint — should trigger _blocked_user_status_response
        resp = client_with_custom_msg.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["status"] == "pending"
        assert data["custom_message"] == "Contact @admin in Slack"


class TestConfigSetting:
    """Test the REQUIRE_APPROVAL configuration setting."""

    def test_default_is_true(self):
        """REQUIRE_APPROVAL defaults to True."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            },
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
                assert settings.require_approval is True
            finally:
                get_settings.cache_clear()

    def test_can_set_false(self):
        """REQUIRE_APPROVAL can be set to False via env var."""
        with patch.dict(
            os.environ,
            {
                "REQUIRE_APPROVAL": "false",
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            },
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
                assert settings.require_approval is False
            finally:
                get_settings.cache_clear()


class _StubRequest:
    """Minimal Request stand-in for driving the navbar SSE generators."""

    def __init__(self, username: str | None = None, is_admin: bool = False):
        self.state = type("State", (), {"username": username, "is_admin": is_admin})()

    async def is_disconnected(self) -> bool:
        return False


def _maybe_fail(fail: dict[str, BaseException], key: str, value):
    """AsyncMock side effect that raises while ``key`` is present in ``fail``."""

    def _side_effect(*_args):
        if key in fail:
            raise fail[key]
        return value() if callable(value) else value

    return AsyncMock(side_effect=_side_effect)


@contextlib.contextmanager
def _navbar_storage(
    pending: list[str],
    role: str = "admin",
    *,
    fail: dict[str, BaseException] | None = None,
):
    """Patch the storage reads the navbar streams make; yield the main module.

    ``pending`` is mutated by the test to simulate a count change.  ``role`` is
    the live role of the streaming admin, so tests can revoke admin rights
    while the stream is open.  ``fail`` maps a storage read (``"role"`` for the
    admin re-check, ``"count"`` for the pending lookup) to the exception it
    raises; tests mutate the dict to toggle the failure.
    """
    from rootcoz import main

    fail = fail if fail is not None else {}
    with (
        patch.object(main.storage, "count_active_analyses", AsyncMock(return_value=0)),
        patch.object(
            main.storage, "get_unread_mention_count", AsyncMock(return_value=0)
        ),
        patch.object(
            main.storage,
            "list_pending_users",
            _maybe_fail(fail, "count", lambda: [{"username": u} for u in pending]),
        ),
        patch.object(
            main.storage,
            "get_user_by_username",
            _maybe_fail(fail, "role", {"username": "boss", "role": role}),
        ),
        patch.object(main, "_check_allow_list"),
    ):
        yield main


async def _next_or_timeout(stream, timeout: float = 0.5):
    """Return the next SSE chunk, or raise if the stream stayed quiet."""
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


async def _open_navbar_stream(main, endpoint: str, *, username: str, is_admin: bool):
    """Open the navbar SSE stream on either endpoint and return its body."""
    request = _StubRequest(username=username, is_admin=is_admin)
    if endpoint == "multiplexed":
        resp = await main.stream_multiplexed(request, topics="navbar")
    else:
        resp = await main.stream_navbar_counts(request)
    return resp.body_iterator


# SSE event name carrying the pending count, per endpoint.
_PENDING_EVENT = {"multiplexed": "navbar:pending-count", "standalone": "pending-count"}


class TestNavbarPendingCountStream:
    """The navbar SSE streams carry pending-count for admins (issue #227)."""

    @pytest.mark.asyncio
    async def test_multiplexed_admin_gets_initial_and_updated_pending_count(self):
        """Layout consumes /api/stream?topics=navbar — this is its pending-count path."""
        pending = ["p1", "p2"]
        with _navbar_storage(pending) as main:
            resp = await main.stream_multiplexed(
                _StubRequest(username="boss", is_admin=True), topics="navbar"
            )
            stream = resp.body_iterator
            try:
                initial = [await stream.__anext__() for _ in range(3)]
                assert initial[0].startswith("event: navbar:active-count")
                assert initial[1].startswith("event: navbar:unread-count")
                assert initial[2] == "event: navbar:pending-count\ndata: 2\n\n"
                assert main._pending_count_listeners  # registered on connect

                # Same value re-broadcast is deduped, then a real change is sent
                pending.remove("p2")
                main.notify_pending_count_changed()
                assert await _next_or_timeout(stream) == (
                    "event: navbar:pending-count\ndata: 1\n\n"
                )
                pending.clear()
                main.notify_pending_count_changed()
                assert await _next_or_timeout(stream) == (
                    "event: navbar:pending-count\ndata: 0\n\n"
                )
            finally:
                await stream.aclose()
            assert not main._pending_count_listeners  # deregistered on disconnect

    @pytest.mark.asyncio
    async def test_standalone_admin_gets_initial_and_updated_pending_count(self):
        pending = ["p1", "p2"]
        with _navbar_storage(pending) as main:
            resp = await main.stream_navbar_counts(
                _StubRequest(username="boss", is_admin=True)
            )
            stream = resp.body_iterator
            try:
                initial = [await stream.__anext__() for _ in range(3)]
                assert initial[-1] == "event: pending-count\ndata: 2\n\n"
                assert main._pending_count_listeners  # registered on connect

                pending.clear()
                main.notify_pending_count_changed()
                assert await _next_or_timeout(stream) == (
                    "event: pending-count\ndata: 0\n\n"
                )
            finally:
                await stream.aclose()
            assert not main._pending_count_listeners  # deregistered on disconnect

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["multiplexed", "standalone"])
    async def test_non_admin_never_receives_pending_count(self, endpoint):
        with _navbar_storage(["p1"]) as main:
            stream = await _open_navbar_stream(
                main, endpoint, username="bob", is_admin=False
            )
            try:
                initial = [await stream.__anext__() for _ in range(2)]
                assert not any("pending-count" in chunk for chunk in initial)
                assert not main._pending_count_listeners
                main.notify_pending_count_changed()
                # The next chunk would only be the 30s keepalive.
                with pytest.raises((TimeoutError, StopAsyncIteration)):
                    await _next_or_timeout(stream)
            finally:
                await stream.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["multiplexed", "standalone"])
    async def test_revoked_admin_stops_receiving_counts(self, endpoint):
        """Admin rights are re-checked on every notification (#227 security)."""
        pending = ["p1", "p2"]
        with _navbar_storage(pending) as main:
            stream = await _open_navbar_stream(
                main, endpoint, username="boss", is_admin=True
            )
            event = _PENDING_EVENT[endpoint]
            try:
                await stream.__anext__()
                await stream.__anext__()
                await stream.__anext__()  # initial pending count
                listener = next(iter(main._pending_count_listeners))

                # Admin downgraded to reviewer while the stream stays open
                with _navbar_storage(pending, role="reviewer"):
                    main.notify_pending_count_changed()
                    # Badge is cleared once...
                    assert await _next_or_timeout(stream) == (
                        f"event: {event}\ndata: 0\n\n"
                    )
                assert listener not in main._pending_count_listeners
                pending.clear()
                main.notify_pending_count_changed()
                # ...and no further counts are delivered.
                with pytest.raises((TimeoutError, StopAsyncIteration)):
                    await _next_or_timeout(stream)
            finally:
                await stream.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["multiplexed", "standalone"])
    async def test_failed_role_lookup_sends_no_pending_count(self, endpoint):
        """An unverifiable role must not be treated as admin (#227 security)."""
        pending = ["p1", "p2"]
        fail: dict[str, BaseException] = {}
        with _navbar_storage(pending, fail=fail) as main:
            stream = await _open_navbar_stream(
                main, endpoint, username="boss", is_admin=True
            )
            try:
                initial = [await stream.__anext__() for _ in range(3)]
                assert initial[2] == f"event: {_PENDING_EVENT[endpoint]}\ndata: 2\n\n"
                listener = next(iter(main._pending_count_listeners))

                # The role lookup starts failing while the stream stays open
                pending.clear()
                fail["role"] = aiosqlite.OperationalError("database is locked")
                main.notify_pending_count_changed()
                # Nothing is emitted — an unverified role gets no count at all.
                with pytest.raises((TimeoutError, StopAsyncIteration)):
                    await _next_or_timeout(stream)
                assert listener not in main._pending_count_listeners
                fail.clear()
                main.notify_pending_count_changed()
                # ...and delivery never resumes on this connection.
                with pytest.raises((TimeoutError, StopAsyncIteration)):
                    await _next_or_timeout(stream)
            finally:
                await stream.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["multiplexed", "standalone"])
    async def test_failed_count_lookup_emits_no_zero(self, endpoint):
        """A failed pending lookup must not masquerade as a real count (#227)."""
        pending = ["p1", "p2"]
        fail: dict[str, BaseException] = {}
        with _navbar_storage(pending, fail=fail) as main:
            stream = await _open_navbar_stream(
                main, endpoint, username="boss", is_admin=True
            )
            try:
                for _ in range(3):  # active, unread, initial pending count
                    await stream.__anext__()
                assert main._pending_count_listeners  # still registered

                fail["count"] = aiosqlite.OperationalError("database is locked")
                pending.clear()
                main.notify_pending_count_changed()
                # The badge keeps its last good count instead of reading 0.
                with pytest.raises((TimeoutError, StopAsyncIteration)):
                    await _next_or_timeout(stream)
            finally:
                await stream.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["multiplexed", "standalone"])
    async def test_failed_count_lookup_retries_on_keepalive(self, endpoint):
        """A failed pending lookup is retried without a new mutation (#227)."""
        pending = ["p1", "p2"]
        fail: dict[str, BaseException] = {}
        with (
            _navbar_storage(pending, fail=fail) as main,
            patch.object(main, "_SSE_KEEPALIVE_SECONDS", 0.01),
        ):
            stream = await _open_navbar_stream(
                main, endpoint, username="boss", is_admin=True
            )
            try:
                for _ in range(3):  # active, unread, initial pending count
                    await stream.__anext__()

                fail["count"] = aiosqlite.OperationalError("database is locked")
                pending.clear()
                main.notify_pending_count_changed()
                # The keepalive is emitted once the failed lookup is observed.
                assert await _next_or_timeout(stream) == ": keepalive\n\n"
                fail.clear()
                pending.append("p3")  # count is 3 until the retry lands
                # No further notification happens: the retry alone delivers it.
                assert await _next_or_timeout(stream) == (
                    f"event: {_PENDING_EVENT[endpoint]}\ndata: 1\n\n"
                )
            finally:
                await stream.aclose()


class TestPendingCountBroadcast:
    """Endpoints that change the pending list broadcast a refresh (#227)."""

    def test_register_broadcasts(self, client_approval_on):
        from rootcoz import main

        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_on.post(
                "/api/auth/register", json={"username": "bcast0"}
            )
        assert resp.status_code == 200
        notify.assert_called_once()

    def test_register_without_approval_does_not_broadcast(self, client_approval_off):
        """No pending user was created, so listeners must not be woken."""
        from rootcoz import main

        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_off.post(
                "/api/auth/register", json={"username": "noapproval"}
            )
        assert resp.status_code == 200
        notify.assert_not_called()

    def test_approve_broadcasts(self, client_approval_on):
        from rootcoz import main

        client_approval_on.post("/api/auth/register", json={"username": "bcast1"})
        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_on.post(
                "/api/admin/users/bcast1/approve", headers=_admin_headers()
            )
        assert resp.status_code == 200
        notify.assert_called_once()

    def test_reject_broadcasts(self, client_approval_on):
        from rootcoz import main

        client_approval_on.post("/api/auth/register", json={"username": "bcast2"})
        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_on.post(
                "/api/admin/users/bcast2/reject", headers=_admin_headers()
            )
        assert resp.status_code == 200
        notify.assert_called_once()

    def test_delete_broadcasts(self, client_approval_on):
        from rootcoz import main

        client_approval_on.post("/api/auth/register", json={"username": "bcast3"})
        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_on.delete(
                "/api/admin/users/bcast3", headers=_admin_headers()
            )
        assert resp.status_code == 200
        notify.assert_called_once()

    def test_role_change_broadcasts(self, client_approval_on):
        """Promoting a pending user drops them from the pending list."""
        from rootcoz import main

        client_approval_on.post("/api/auth/register", json={"username": "bcast4"})
        with patch.object(main, "notify_pending_count_changed") as notify:
            resp = client_approval_on.put(
                "/api/admin/users/bcast4/role",
                json={"role": "admin"},
                headers=_admin_headers(),
            )
        assert resp.status_code == 200
        notify.assert_called_once()
