"""Web Push notification dispatch for @mentions in comments."""

import asyncio
import json
import os

from pywebpush import WebPushException, webpush
from simple_logger.logger import get_logger

from jenkins_job_insight.storage import (
    delete_stale_push_subscriptions,
    get_push_subscriptions_for_users,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


async def _send_one(
    sub: dict,
    payload_str: str,
    vapid_private_key: str,
    vapid_claim_email: str,
) -> str | None:
    """Send a single push notification. Returns endpoint if stale (410), else None."""
    try:
        await asyncio.to_thread(
            webpush,
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {
                    "p256dh": sub["p256dh_key"],
                    "auth": sub["auth_key"],
                },
            },
            data=payload_str,
            vapid_private_key=vapid_private_key,
            vapid_claims={
                "sub": vapid_claim_email
                if vapid_claim_email.startswith("mailto:")
                else f"mailto:{vapid_claim_email}"
            },
        )
        logger.debug(
            "send_mention_notifications: sent to %s at %s",
            sub["username"],
            sub["endpoint"],
        )
    except WebPushException as exc:
        if (
            hasattr(exc, "response")
            and exc.response is not None
            and exc.response.status_code == 410
        ):
            logger.info(
                "send_mention_notifications: stale subscription (410) for %s, scheduling removal",
                sub["username"],
            )
            return sub["endpoint"]
        logger.warning(
            "send_mention_notifications: failed to send to %s: %s",
            sub["username"],
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort delivery; log and continue
        logger.warning(
            "send_mention_notifications: unexpected error sending to %s: %s",
            sub["username"],
            exc,
        )
    return None


async def send_mention_notifications(
    mentioned_usernames: list[str],
    comment_author: str,
    job_id: str,
    test_name: str,
    vapid_private_key: str,
    vapid_claim_email: str,
    public_base_url: str | None = None,
) -> None:
    """Send Web Push notifications to mentioned users.

    Best-effort — failures are logged but never raised.
    Removes stale subscriptions (410 Gone) automatically.
    Does not notify the comment author if they mention themselves.
    """
    try:
        recipients = [u for u in mentioned_usernames if u != comment_author]
        if not recipients:
            return

        subscriptions = await get_push_subscriptions_for_users(recipients)
        if not subscriptions:
            logger.debug(
                f"send_mention_notifications: no subscriptions for {recipients}"
            )
            return

        report_path = f"/report/{job_id}"
        url = (
            f"{public_base_url.rstrip('/')}{report_path}"
            if public_base_url
            else report_path
        )
        payload = json.dumps(
            {
                "title": f"Mentioned by @{comment_author}",
                "body": f"@{comment_author} mentioned you in a comment on {test_name}",
                "url": url,
            }
        )
        results = await asyncio.gather(
            *[
                _send_one(sub, payload, vapid_private_key, vapid_claim_email)
                for sub in subscriptions
            ],
            return_exceptions=True,
        )
        stale_endpoints = [r for r in results if isinstance(r, str)]

        if stale_endpoints:
            await delete_stale_push_subscriptions(stale_endpoints)

    except Exception:  # noqa: BLE001 — best-effort dispatch; never propagate
        logger.warning(
            "send_mention_notifications: unexpected error in notification dispatch",
            exc_info=True,
        )
