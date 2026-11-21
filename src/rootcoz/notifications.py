"""Web Push notification dispatch for @mentions in comments."""

import asyncio
import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import requests
from pywebpush import WebPushException, webpush
from requests.adapters import DEFAULT_POOLBLOCK, HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util.connection import allowed_gai_family

from rootcoz import get_child_logger
from rootcoz.storage import (
    delete_stale_push_subscriptions,
    get_push_subscriptions_for_users,
)
from rootcoz.url_utils import (
    is_public_ip,
    mask_stored_url,
    validate_public_https_url,
)

logger = get_child_logger(__name__)

# Finite connect/read timeout so unresponsive push endpoints fail promptly.
WEBPUSH_TIMEOUT_SECONDS: float = 10.0

# Dedicated bounded executor isolates outbound push I/O from Python's shared
# default asyncio.to_thread pool so stalling endpoints cannot starve the server.
_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rootcoz-webpush")


class _PublicHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that validates every resolved IP is public and pins it."""

    def _new_conn(self) -> socket.socket:
        dns_host = self._dns_host.strip("[]")
        try:
            infos = socket.getaddrinfo(
                dns_host, self.port, allowed_gai_family(), socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc

        if not infos:
            raise NewConnectionError(
                self, f"DNS resolution returned no addresses for {self.host}"
            )

        for _family, _socktype, _proto, _canonname, sockaddr in infos:
            ip = ipaddress.ip_address(str(sockaddr[0]))
            if not is_public_ip(ip):
                logger.warning(
                    "webpush: blocked connection to non-public resolved IP for host %s",
                    self.host,
                )
                raise NewConnectionError(
                    self,
                    f"Refusing push connection to non-public address for {self.host}",
                )

        last_err: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in infos:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.socket_options:
                    for opt in self.socket_options:
                        sock.setsockopt(*opt)
                if isinstance(self.timeout, (int, float)):
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                return sock
            except TimeoutError as exc:
                if sock is not None:
                    sock.close()
                last_err = exc
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_err = exc

        if isinstance(last_err, TimeoutError):
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from last_err
        raise NewConnectionError(
            self, f"Failed to establish a new connection: {last_err}"
        ) from last_err


class _PublicHTTPSConnectionPool(HTTPSConnectionPool):
    """HTTPSConnectionPool using connect-time public-IP validation and pinning."""

    ConnectionCls = _PublicHTTPSConnection


class _PublicHTTPSAdapter(HTTPAdapter):
    """HTTPAdapter restricted to HTTPS connections targeting public IPs."""

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = DEFAULT_POOLBLOCK,
        **pool_kwargs: Any,
    ) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "https": _PublicHTTPSConnectionPool,
        }


class _WebPushSession(requests.Session):
    """Hardened Session: public HTTPS only, no redirects, connect-time IP pinning."""

    def __init__(self) -> None:
        super().__init__()
        self.max_redirects = 0
        self.adapters.clear()
        self.mount("https://", _PublicHTTPSAdapter())

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        if not request.url:
            raise ValueError("Push endpoint must be a valid public https:// URL")
        validate_public_https_url(request.url, field_name="Push endpoint")
        kwargs["allow_redirects"] = False
        return super().send(request, **kwargs)

    def resolve_redirects(
        self,
        resp: requests.Response,
        req: requests.PreparedRequest,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        raise requests.TooManyRedirects(
            "Redirects are disabled for Web Push delivery", response=resp
        )


def _deliver_webpush(
    subscription_info: dict[str, Any],
    data: str,
    vapid_private_key: str,
    vapid_claims: dict[str, str],
    timeout: float,
) -> None:
    """Synchronously deliver one Web Push message inside a closed hardened session."""
    with _WebPushSession() as session:
        webpush(
            subscription_info=subscription_info,
            data=data,
            vapid_private_key=vapid_private_key,
            vapid_claims=vapid_claims,
            timeout=timeout,
            requests_session=session,
        )


async def _send_one(
    sub: dict[str, Any],
    payload_str: str,
    vapid_private_key: str,
    vapid_claim_email: str,
) -> str | None:
    """Send a single push notification. Returns endpoint if stale (410), else None."""
    try:
        endpoint = validate_public_https_url(
            sub["endpoint"], field_name="Push endpoint"
        )
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning(
            "send_mention_notifications: skipping invalid push endpoint for %s (%s): %s",
            sub.get("username", "<unknown>"),
            mask_stored_url(str(sub.get("endpoint", ""))),
            exc,
        )
        return None

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _PUSH_EXECUTOR,
            partial(
                _deliver_webpush,
                subscription_info={
                    "endpoint": endpoint,
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
                timeout=WEBPUSH_TIMEOUT_SECONDS,
            ),
        )
        logger.debug(
            "send_mention_notifications: sent to %s at %s",
            sub["username"],
            endpoint,
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
    except (TypeError, ValueError, KeyError, OSError) as exc:
        # best-effort delivery; log and continue
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

        # The SPA serves the report at /results/:jobId (frontend/src/App.tsx).
        # /report/:id is not a registered route, so a tap on the notification
        # used to land on an unmatched URL.
        report_path = f"/results/{job_id}"
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

    except Exception:  # best-effort dispatch; never propagate
        logger.warning(
            "send_mention_notifications: unexpected error in notification dispatch",
            exc_info=True,
        )
