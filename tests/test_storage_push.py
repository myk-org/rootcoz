"""Tests for push subscription storage functions."""

from pathlib import Path
from unittest.mock import patch

import pytest

from jenkins_job_insight import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


class TestSavePushSubscription:
    """Tests for save_push_subscription."""

    async def test_insert_new_subscription(self, setup_test_db: Path) -> None:
        """Saving a new subscription creates a row."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_push_subscription(
                username="alice",
                endpoint="https://push.example.com/sub/alice-1",
                p256dh_key="p256dh-alice",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="auth-alice",  # pragma: allowlist secret  # gitleaks:allow
            )
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == 1
            assert subs[0]["username"] == "alice"
            assert subs[0]["endpoint"] == "https://push.example.com/sub/alice-1"
            assert subs[0]["p256dh_key"] == "p256dh-alice"  # pragma: allowlist secret  # gitleaks:allow  # fmt: skip
            assert subs[0]["auth_key"] == "auth-alice"  # pragma: allowlist secret  # gitleaks:allow  # fmt: skip

    async def test_upsert_existing_endpoint(self, setup_test_db: Path) -> None:
        """Saving with an existing endpoint upserts the record."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            endpoint = "https://push.example.com/sub/shared"
            await storage.save_push_subscription(
                username="alice",
                endpoint=endpoint,
                p256dh_key="old-key",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="old-auth",  # pragma: allowlist secret  # gitleaks:allow
            )
            # Update with same endpoint but different keys/user
            await storage.save_push_subscription(
                username="bob",
                endpoint=endpoint,
                p256dh_key="new-key",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="new-auth",  # pragma: allowlist secret  # gitleaks:allow
            )
            # Should only have one record for this endpoint
            subs_alice = await storage.get_push_subscriptions_for_users(["alice"])
            subs_bob = await storage.get_push_subscriptions_for_users(["bob"])
            assert len(subs_alice) == 0  # alice no longer owns it
            assert len(subs_bob) == 1
            assert subs_bob[0]["p256dh_key"] == "new-key"  # pragma: allowlist secret  # gitleaks:allow  # fmt: skip

    async def test_multiple_subscriptions_per_user(self, setup_test_db: Path) -> None:
        """A user can have multiple subscriptions (different browsers/devices)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_push_subscription(
                username="alice",
                endpoint="https://push.example.com/sub/alice-browser1",
                p256dh_key="key1",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="auth1",  # pragma: allowlist secret  # gitleaks:allow
            )
            await storage.save_push_subscription(
                username="alice",
                endpoint="https://push.example.com/sub/alice-browser2",
                p256dh_key="key2",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="auth2",  # pragma: allowlist secret  # gitleaks:allow
            )
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == 2


class TestDeletePushSubscription:
    """Tests for delete_push_subscription."""

    async def test_delete_existing(self, setup_test_db: Path) -> None:
        """Deleting an existing subscription returns True."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            endpoint = "https://push.example.com/sub/to-delete"
            await storage.save_push_subscription(
                username="alice",
                endpoint=endpoint,
                p256dh_key="key",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="auth",  # pragma: allowlist secret  # gitleaks:allow
            )
            deleted = await storage.delete_push_subscription(endpoint, "alice")
            assert deleted is True
            # Verify it's gone
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == 0

    async def test_delete_nonexistent_returns_false(self, setup_test_db: Path) -> None:
        """Deleting a non-existent endpoint returns False."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            deleted = await storage.delete_push_subscription(
                "https://push.example.com/sub/nonexistent", "alice"
            )
            assert deleted is False

    async def test_delete_wrong_user_returns_false(self, setup_test_db: Path) -> None:
        """Deleting another user's subscription returns False."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            endpoint = "https://push.example.com/sub/alice-owned"
            await storage.save_push_subscription(
                username="alice",
                endpoint=endpoint,
                p256dh_key="key",  # pragma: allowlist secret  # gitleaks:allow
                auth_key="auth",  # pragma: allowlist secret  # gitleaks:allow
            )
            deleted = await storage.delete_push_subscription(endpoint, "bob")
            assert deleted is False
            # Verify it's still there
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == 1


class TestSubscriptionLimit:
    """Tests for per-user subscription limit."""

    async def test_oldest_deleted_when_limit_exceeded(
        self, setup_test_db: Path
    ) -> None:
        """Adding beyond MAX_PUSH_SUBSCRIPTIONS_PER_USER deletes the oldest."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Create MAX + 1 subscriptions
            limit = storage.MAX_PUSH_SUBSCRIPTIONS_PER_USER
            for i in range(limit + 1):
                await storage.save_push_subscription(
                    username="alice",
                    endpoint=f"https://push.example.com/sub/alice-{i}",
                    p256dh_key=f"key-{i}",  # pragma: allowlist secret  # gitleaks:allow
                    auth_key=f"auth-{i}",  # pragma: allowlist secret  # gitleaks:allow
                )
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == limit
            # The oldest (alice-0) should have been evicted
            endpoints = {s["endpoint"] for s in subs}
            assert "https://push.example.com/sub/alice-0" not in endpoints
            assert f"https://push.example.com/sub/alice-{limit}" in endpoints


class TestGetPushSubscriptionsForUsers:
    """Tests for get_push_subscriptions_for_users."""

    async def test_get_subscriptions_for_multiple_users(
        self, setup_test_db: Path
    ) -> None:
        """Returns subscriptions for multiple users."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_push_subscription(
                "alice", "https://push.example.com/alice", "k1", "a1"
            )
            await storage.save_push_subscription(
                "bob", "https://push.example.com/bob", "k2", "a2"
            )
            await storage.save_push_subscription(
                "carol", "https://push.example.com/carol", "k3", "a3"
            )
            subs = await storage.get_push_subscriptions_for_users(["alice", "bob"])
            assert len(subs) == 2
            usernames = {s["username"] for s in subs}
            assert usernames == {"alice", "bob"}

    async def test_empty_usernames_returns_empty(self, setup_test_db: Path) -> None:
        """Empty usernames list returns empty list without DB query."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            subs = await storage.get_push_subscriptions_for_users([])
            assert subs == []

    async def test_no_subscriptions_returns_empty(self, setup_test_db: Path) -> None:
        """Returns empty list when users have no subscriptions."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            subs = await storage.get_push_subscriptions_for_users(["nobody"])
            assert subs == []


class TestDeleteStalePushSubscriptions:
    """Tests for delete_stale_push_subscriptions."""

    async def test_remove_stale_endpoints(self, setup_test_db: Path) -> None:
        """Stale endpoints are removed from the database."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            ep1 = "https://push.example.com/stale1"
            ep2 = "https://push.example.com/stale2"
            ep3 = "https://push.example.com/valid"
            await storage.save_push_subscription("alice", ep1, "k1", "a1")
            await storage.save_push_subscription("bob", ep2, "k2", "a2")
            await storage.save_push_subscription("carol", ep3, "k3", "a3")

            await storage.delete_stale_push_subscriptions([ep1, ep2])

            # Only carol's subscription should remain
            subs = await storage.get_push_subscriptions_for_users(
                ["alice", "bob", "carol"]
            )
            assert len(subs) == 1
            assert subs[0]["username"] == "carol"

    async def test_empty_endpoints_noop(self, setup_test_db: Path) -> None:
        """Empty endpoints list is a no-op."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_push_subscription(
                "alice", "https://push.example.com/keep", "k1", "a1"
            )
            await storage.delete_stale_push_subscriptions([])
            subs = await storage.get_push_subscriptions_for_users(["alice"])
            assert len(subs) == 1
