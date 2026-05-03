"""Tests for Web Push notification dispatch."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jenkins_job_insight.notifications import send_mention_notifications


class TestSendMentionNotifications:
    """Tests for send_mention_notifications."""

    @pytest.mark.asyncio
    async def test_self_mention_filtered(self) -> None:
        """Comment author mentioning themselves should not trigger any push."""
        with patch(
            "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
            new_callable=AsyncMock,
        ) as mock_get:
            await send_mention_notifications(
                mentioned_usernames=["alice"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_filtered_from_mixed_list(self) -> None:
        """Author is removed from a mixed mentioned list before subscription lookup."""
        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_get,
            patch("jenkins_job_insight.notifications.webpush"),
        ):
            await send_mention_notifications(
                mentioned_usernames=["alice", "bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            mock_get.assert_called_once_with(["bob"])

    @pytest.mark.asyncio
    async def test_empty_mentioned_list(self) -> None:
        """Empty mentioned list should return early without fetching subscriptions."""
        with patch(
            "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
            new_callable=AsyncMock,
        ) as mock_get:
            await send_mention_notifications(
                mentioned_usernames=[],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_subscriptions_found(self) -> None:
        """When no subscriptions exist for mentioned users, return early."""
        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "jenkins_job_insight.notifications.webpush",
            ) as mock_webpush,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            mock_webpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_push_to_subscribed_users(self) -> None:
        """Push notifications are sent to subscribed mentioned users."""
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-1",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "jenkins_job_insight.notifications.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
                public_base_url="https://jji.example.com",
            )
            mock_to_thread.assert_called_once()
            call_args = mock_to_thread.call_args
            # First positional arg is the webpush function
            # Check kwargs passed to webpush via to_thread
            assert (
                call_args.kwargs["subscription_info"]["endpoint"]
                == "https://push.example.com/sub/bob-1"
            )
            payload = json.loads(call_args.kwargs["data"])
            assert payload["title"] == "Mentioned by @alice"
            assert "test_foo" in payload["body"]
            assert payload["url"] == "https://jji.example.com/report/job-1"

    @pytest.mark.asyncio
    async def test_stale_subscriptions_cleaned_up(self) -> None:
        """Subscriptions returning 410 Gone are deleted."""
        from pywebpush import WebPushException

        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-stale",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        # Create a 410 response mock
        mock_response = MagicMock()
        mock_response.status_code = 410
        exc = WebPushException("Gone")
        exc.response = mock_response

        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "jenkins_job_insight.notifications.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=exc,
            ),
            patch(
                "jenkins_job_insight.notifications.delete_stale_push_subscriptions",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            mock_delete.assert_called_once_with(
                ["https://push.example.com/sub/bob-stale"]
            )

    @pytest.mark.asyncio
    async def test_errors_do_not_propagate(self) -> None:
        """Any unexpected error is caught and logged, never raised."""
        with patch(
            "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB connection failed"),
        ):
            # Should not raise
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )

    @pytest.mark.asyncio
    async def test_multiple_recipients_multiple_subscriptions(self) -> None:
        """Multiple mentioned users with multiple subscriptions each get notified."""
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-1",
                "p256dh_key": "p256dh-bob1",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob1",  # pragma: allowlist secret  # gitleaks:allow
            },
            {
                "username": "carol",
                "endpoint": "https://push.example.com/sub/carol-1",
                "p256dh_key": "p256dh-carol1",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-carol1",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "jenkins_job_insight.notifications.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob", "carol"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_bar",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            assert mock_to_thread.call_count == 2

    @pytest.mark.asyncio
    async def test_webpush_non_410_error_logged_not_raised(self) -> None:
        """WebPushException with non-410 status is logged but does not raise."""
        from pywebpush import WebPushException

        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-err",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        exc = WebPushException("Server error")
        exc.response = MagicMock()
        exc.response.status_code = 500

        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "jenkins_job_insight.notifications.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=exc,
            ),
            patch(
                "jenkins_job_insight.notifications.delete_stale_push_subscriptions",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            # Should not raise
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            # Should NOT delete — it wasn't a 410
            mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_without_public_base_url(self) -> None:
        """When public_base_url is None, URL is a relative path."""
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-1",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        with (
            patch(
                "jenkins_job_insight.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "jenkins_job_insight.notifications.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
                public_base_url=None,
            )
            payload = json.loads(mock_to_thread.call_args.kwargs["data"])
            assert payload["url"] == "/report/job-1"
