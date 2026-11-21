"""Web Push notification dispatch for @mentions in comments."""

import asyncio
import ipaddress
import json
import socket
import threading
import time
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

# Whole-delivery wall-clock deadline so stalled or slow-drip push endpoints fail promptly.
WEBPUSH_TIMEOUT_SECONDS: float = 10.0

# Cap push response bodies (Web Push services return empty or tiny error bodies).
_MAX_WEBPUSH_RESPONSE_BYTES: int = 64 * 1024

# Dedicated bounded executor isolates outbound push I/O from Python's shared
# default asyncio.to_thread pool so stalling endpoints cannot starve the server.
_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rootcoz-webpush")

# Bound background DNS resolver threads so stalled lookups cannot grow unboundedly.
_DNS_SEMAPHORE = threading.BoundedSemaphore(16)

# Thread-local active deadline binding _WebPushSession.send() to _PublicHTTPSConnection.
_CURRENT_DEADLINE = threading.local()


class _DeliveryDeadline:
    """Wall-clock deadline controller that can interrupt in-flight sockets on expiry."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.deadline = time.monotonic() + timeout_seconds
        self._lock = threading.Lock()
        self._aborted = False
        self._finished = False
        self._dups: list[socket.socket] = []

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def register_socket(self, sock: socket.socket) -> None:
        """Track a duplicate handle so abort() can interrupt even SSL-wrapped sockets."""
        dup = sock.dup()
        with self._lock:
            if self._aborted or self._finished:
                dup.close()
                if self._aborted:
                    raise TimeoutError("Web Push delivery exceeded total timeout")
                return
            self._dups.append(dup)

    def abort(self) -> None:
        with self._lock:
            if self._finished or self._aborted:
                return
            self._aborted = True
            dups = list(self._dups)
            self._dups.clear()
        for dup in dups:
            try:
                dup.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                dup.close()
            except OSError:
                pass

    def finish(self) -> bool:
        """Release tracked duplicate handles; returns True if deadline already aborted."""
        with self._lock:
            self._finished = True
            aborted = self._aborted
            dups = list(self._dups)
            self._dups.clear()
        for dup in dups:
            try:
                dup.close()
            except OSError:
                pass
        return aborted


def _resolve_host_with_timeout(
    host: str, port: int, timeout: float
) -> list[tuple[Any, ...]]:
    """Resolve *host* via socket.getaddrinfo within *timeout* seconds."""
    if timeout <= 0:
        raise TimeoutError(f"DNS resolution for {host} timed out")
    if not _DNS_SEMAPHORE.acquire(blocking=False):
        raise TimeoutError(f"DNS resolution capacity exhausted for {host}")

    done = threading.Event()
    result: list[tuple[Any, ...]] = []
    error: list[BaseException] = []

    def _worker() -> None:
        try:
            result.extend(
                socket.getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM)
            )
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            done.set()
            _DNS_SEMAPHORE.release()

    threading.Thread(target=_worker, name="rootcoz-webpush-dns", daemon=True).start()

    if not done.wait(timeout=timeout):
        raise TimeoutError(f"DNS resolution for {host} timed out after {timeout:.2f}s")
    if error:
        raise error[0]
    return result


class _PublicHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that validates every resolved IP is public and pins it."""

    def _effective_timeout(self, deadline: _DeliveryDeadline | None) -> float:
        base = (
            float(self.timeout)
            if isinstance(self.timeout, (int, float))
            else WEBPUSH_TIMEOUT_SECONDS
        )
        if deadline is None:
            return base
        return min(base, deadline.remaining())

    def _new_conn(self) -> socket.socket:
        deadline: _DeliveryDeadline | None = getattr(_CURRENT_DEADLINE, "value", None)
        remaining = self._effective_timeout(deadline)
        if remaining <= 0 or (deadline is not None and deadline.aborted):
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out before DNS resolution.",
            )

        dns_host = self._dns_host.strip("[]")
        try:
            infos = _resolve_host_with_timeout(dns_host, self.port, remaining)
        except TimeoutError as exc:
            raise ConnectTimeoutError(
                self,
                f"DNS resolution for {self.host} timed out. (timeout={remaining:.2f}s)",
            ) from exc
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
            remaining = self._effective_timeout(deadline)
            if remaining <= 0 or (deadline is not None and deadline.aborted):
                last_err = TimeoutError("Web Push delivery exceeded total timeout")
                break
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.socket_options:
                    for opt in self.socket_options:
                        sock.setsockopt(*opt)
                sock.settimeout(remaining)
                if self.source_address:
                    sock.bind(self.source_address)
                if deadline is not None:
                    deadline.register_socket(sock)
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
    """HTTPAdapter restricted to direct HTTPS connections targeting public IPs."""

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

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        raise ValueError("Proxies are disabled for Web Push delivery")


def _extract_total_timeout(timeout: Any) -> float:
    """Resolve a Requests timeout value into a positive finite total-deadline float."""
    if isinstance(timeout, tuple) and timeout:
        candidates = [
            float(v) for v in timeout if isinstance(v, (int, float)) and v > 0
        ]
        if candidates:
            return max(candidates)
    elif isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)
    return WEBPUSH_TIMEOUT_SECONDS


class _WebPushSession(requests.Session):
    """Hardened Session: direct public HTTPS only, no redirects, total deadline."""

    def __init__(self) -> None:
        super().__init__()
        self.trust_env = False
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
        kwargs["stream"] = True

        total_timeout = _extract_total_timeout(kwargs.get("timeout"))
        kwargs["timeout"] = total_timeout

        deadline = _DeliveryDeadline(total_timeout)
        timer = threading.Timer(total_timeout, deadline.abort)
        timer.daemon = True
        prev_deadline = getattr(_CURRENT_DEADLINE, "value", None)
        _CURRENT_DEADLINE.value = deadline
        timer.start()
        resp: requests.Response | None = None
        try:
            resp = super().send(request, **kwargs)
            if resp.raw is not None:
                chunks: list[bytes] = []
                total_read = 0
                while total_read < _MAX_WEBPUSH_RESPONSE_BYTES:
                    if deadline.aborted or deadline.remaining() <= 0:
                        raise requests.ReadTimeout(
                            "Web Push delivery exceeded total timeout", request=request
                        )
                    chunk = resp.raw.read(
                        min(8192, _MAX_WEBPUSH_RESPONSE_BYTES - total_read),
                        decode_content=True,
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total_read += len(chunk)
                if deadline.aborted:
                    raise requests.ReadTimeout(
                        "Web Push delivery exceeded total timeout", request=request
                    )
                resp._content = b"".join(chunks)
                resp._content_consumed = True  # type: ignore[attr-defined]
            return resp
        except Exception as exc:
            if resp is not None:
                resp.close()
            if deadline.aborted and not isinstance(exc, requests.Timeout):
                raise requests.ReadTimeout(
                    "Web Push delivery exceeded total timeout", request=request
                ) from exc
            raise
        finally:
            timer.cancel()
            deadline.finish()
            _CURRENT_DEADLINE.value = prev_deadline
            if resp is not None:
                resp.close()

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
        timeout = WEBPUSH_TIMEOUT_SECONDS
        await asyncio.wait_for(
            loop.run_in_executor(
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
                    timeout=timeout,
                ),
            ),
            timeout=timeout + 2.0,
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
