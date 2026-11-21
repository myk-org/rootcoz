"""Tests for Web Push notification dispatch."""

import asyncio
import base64
import datetime
import http.server
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

import rootcoz.notifications as notifications_mod
from rootcoz.notifications import send_mention_notifications
from rootcoz.vapid import _generate_vapid_keys


def _make_real_push_keys() -> tuple[str, str]:
    """Generate valid Web Push p256dh + auth keys for end-to-end pywebpush tests."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    p256dh = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii")
    return p256dh, auth


def _write_self_signed_cert(tmp_path: Path, hostname: str) -> tuple[Path, Path]:
    """Create a self-signed TLS cert/key pair valid for *hostname*."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TestSendMentionNotifications:
    """Tests for send_mention_notifications."""

    @pytest.mark.asyncio
    async def test_self_mention_filtered(self) -> None:
        """Comment author mentioning themselves should not trigger any push."""
        with patch(
            "rootcoz.notifications.get_push_subscriptions_for_users",
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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_get,
            patch("rootcoz.notifications.webpush"),
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
            "rootcoz.notifications.get_push_subscriptions_for_users",
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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "rootcoz.notifications.webpush",
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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch("rootcoz.notifications.webpush") as mock_webpush,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
                public_base_url="https://rootcoz.example.com",
            )
            mock_webpush.assert_called_once()
            call_args = mock_webpush.call_args
            assert (
                call_args.kwargs["subscription_info"]["endpoint"]
                == "https://push.example.com/sub/bob-1"
            )
            payload = json.loads(call_args.kwargs["data"])
            assert payload["title"] == "Mentioned by @alice"
            assert "test_foo" in payload["body"]
            # The report page is served at /results/:jobId (frontend/src/App.tsx);
            # /report/:id is not a registered route, so pointing notifications
            # at it makes every tap land on an unmatched URL.
            assert payload["url"] == "https://rootcoz.example.com/results/job-1"

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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.webpush",
                side_effect=exc,
            ),
            patch(
                "rootcoz.notifications.delete_stale_push_subscriptions",
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
            "rootcoz.notifications.get_push_subscriptions_for_users",
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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch("rootcoz.notifications.webpush") as mock_webpush,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob", "carol"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_bar",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
            assert mock_webpush.call_count == 2

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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.webpush",
                side_effect=exc,
            ),
            patch(
                "rootcoz.notifications.delete_stale_push_subscriptions",
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
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch("rootcoz.notifications.webpush") as mock_webpush,
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
            payload = json.loads(mock_webpush.call_args.kwargs["data"])
            assert payload["url"] == "/results/job-1"


class TestWebPushDeliveryHardening:
    """Regression tests for Web Push timeout, redirect, and DNS SSRF hardening (#599)."""

    @pytest.mark.asyncio
    async def test_webpush_passes_finite_timeout_and_closes_session(self) -> None:
        """_send_one passes a finite timeout and closes its requests.Session."""
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://push.example.com/sub/bob-1",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        captured_sessions: list[requests.Session] = []
        closed_sessions: list[requests.Session] = []

        orig_close = requests.Session.close

        def _spy_close(self: requests.Session) -> None:
            closed_sessions.append(self)
            orig_close(self)

        def _fake_webpush(**kwargs: object) -> None:
            sess = kwargs["requests_session"]
            assert isinstance(sess, requests.Session)
            captured_sessions.append(sess)

        with (
            patch(
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.webpush", side_effect=_fake_webpush
            ) as mock_webpush,
            patch.object(requests.Session, "close", _spy_close),
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_foo",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )

        timeout = mock_webpush.call_args.kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        assert 0 < timeout <= 30
        assert len(captured_sessions) == 1
        assert captured_sessions[0] in closed_sessions

    @pytest.mark.asyncio
    async def test_stalling_endpoints_timeout_promptly_without_starving_default_executor(
        self,
    ) -> None:
        """Stalling endpoints time out promptly and never starve asyncio.to_thread."""
        release_tarpit = threading.Event()
        accepted_sockets: list[socket.socket] = []

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        port = server.getsockname()[1]

        def _tarpit_loop() -> None:
            server.settimeout(0.1)
            while not release_tarpit.is_set():
                try:
                    conn, _ = server.accept()
                    accepted_sockets.append(conn)
                except TimeoutError:
                    continue
                except OSError:
                    break
            release_tarpit.wait(5.0)

        tarpit_thread = threading.Thread(target=_tarpit_loop, daemon=True)
        tarpit_thread.start()

        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": f"https://tarpit.example.com:{port}/sub/{i}",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
            for i in range(5)
        ]

        def _fake_getaddrinfo(
            host: str,
            svc_port: int,
            family: int = 0,
            socktype: int = 0,
            *args: object,
            **kwargs: object,
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            if host == "tarpit.example.com":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", int(svc_port)),
                    )
                ]
            raise OSError(f"unexpected host {host}")

        try:
            with (
                patch(
                    "rootcoz.notifications.get_push_subscriptions_for_users",
                    new_callable=AsyncMock,
                    return_value=subscriptions,
                ),
                patch.object(notifications_mod, "WEBPUSH_TIMEOUT_SECONDS", 0.25),
                patch.object(notifications_mod, "is_public_ip", return_value=True),
                patch(
                    "rootcoz.notifications.socket.getaddrinfo",
                    side_effect=_fake_getaddrinfo,
                ),
            ):
                start = time.monotonic()
                push_task = asyncio.create_task(
                    send_mention_notifications(
                        mentioned_usernames=["bob"],
                        comment_author="alice",
                        job_id="job-1",
                        test_name="test_tarpit",
                        vapid_private_key=vapid["private_key"],
                        vapid_claim_email="admin@example.com",
                    )
                )
                # Shared default executor remains responsive during push stalls.
                default_pool_result = await asyncio.wait_for(
                    asyncio.to_thread(lambda: "responsive"), timeout=0.5
                )
                assert default_pool_result == "responsive"

                await asyncio.wait_for(push_task, timeout=3.0)
                elapsed = time.monotonic() - start
                assert elapsed < 2.5
                assert len(accepted_sockets) >= 1
        finally:
            release_tarpit.set()
            server.close()
            for conn in accepted_sockets:
                conn.close()
            tarpit_thread.join(timeout=2.0)

    @pytest.mark.asyncio
    async def test_307_redirect_to_loopback_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        """A 307 redirect from an HTTPS push endpoint to http://127.0.0.1 is NOT followed."""
        http_hits: list[str] = []
        https_hits: list[str] = []

        class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                http_hits.append(f"POST {self.path}")
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        http_server = http.server.HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        http_port = http_server.server_address[1]
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()

        class _RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                https_hits.append(f"POST {self.path}")
                content_len = int(self.headers.get("Content-Length", "0"))
                if content_len:
                    self.rfile.read(content_len)
                self.send_response(307)
                self.send_header(
                    "Location", f"http://127.0.0.1:{http_port}/loopback-hit"
                )
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        cert_path, key_path = _write_self_signed_cert(tmp_path, "push.example.com")
        https_server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        https_server.socket = ssl_ctx.wrap_socket(https_server.socket, server_side=True)
        https_port = https_server.server_address[1]
        https_thread = threading.Thread(target=https_server.serve_forever, daemon=True)
        https_thread.start()

        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": f"https://push.example.com:{https_port}/redir",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
        ]

        orig_session_cls = notifications_mod._WebPushSession

        class _TestCertWebPushSession(orig_session_cls):
            def __init__(self) -> None:
                super().__init__()
                self.verify = str(cert_path)

        def _fake_getaddrinfo(
            host: str,
            svc_port: int,
            family: int = 0,
            socktype: int = 0,
            *args: object,
            **kwargs: object,
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            if host == "push.example.com":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", int(svc_port)),
                    )
                ]
            raise OSError(f"unexpected host {host}")

        try:
            with (
                patch(
                    "rootcoz.notifications.get_push_subscriptions_for_users",
                    new_callable=AsyncMock,
                    return_value=subscriptions,
                ),
                patch.object(
                    notifications_mod, "_WebPushSession", _TestCertWebPushSession
                ),
                patch.object(notifications_mod, "is_public_ip", return_value=True),
                patch(
                    "rootcoz.notifications.socket.getaddrinfo",
                    side_effect=_fake_getaddrinfo,
                ),
            ):
                await send_mention_notifications(
                    mentioned_usernames=["bob"],
                    comment_author="alice",
                    job_id="job-1",
                    test_name="test_redir",
                    vapid_private_key=vapid["private_key"],
                    vapid_claim_email="admin@example.com",
                )

            assert https_hits == ["POST /redir"]
            assert http_hits == []
        finally:
            https_server.shutdown()
            https_server.server_close()
            http_server.shutdown()
            http_server.server_close()
            https_thread.join(timeout=2.0)
            http_thread.join(timeout=2.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resolved_addrs",
        [
            [(socket.AF_INET, ("127.0.0.1", 443))],
            [(socket.AF_INET, ("169.254.169.254", 443))],
            [(socket.AF_INET, ("10.0.0.1", 443))],
            [(socket.AF_INET6, ("::1", 443, 0, 0))],
            [
                (socket.AF_INET, ("93.184.216.34", 443)),
                (socket.AF_INET, ("127.0.0.1", 443)),
            ],
        ],
    )
    async def test_dns_resolving_to_non_public_ip_is_blocked_before_connect(
        self,
        resolved_addrs: list[tuple[int, tuple[object, ...]]],
    ) -> None:
        """DNS resolving to any non-public IP is rejected before socket.connect."""
        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://rebind.example.com/push",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
        ]
        gai_result = [
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
            for family, sockaddr in resolved_addrs
        ]
        connect_calls: list[object] = []

        def _forbid_connect(self: socket.socket, address: object) -> None:
            connect_calls.append(address)
            raise AssertionError(f"socket.connect should not be called: {address}")

        with (
            patch(
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.socket.getaddrinfo",
                return_value=gai_result,
            ),
            patch.object(socket.socket, "connect", _forbid_connect),
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_dns_ssrf",
                vapid_private_key=vapid["private_key"],
                vapid_claim_email="admin@example.com",
            )

        assert connect_calls == []

    @pytest.mark.asyncio
    async def test_dns_rebinding_uses_pinned_validated_ip_without_second_lookup(
        self,
    ) -> None:
        """Connects directly to the validated IP from the single getaddrinfo call."""
        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://rebind.example.com/push",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
        ]
        gai_calls = 0

        def _rebinding_getaddrinfo(
            host: str,
            svc_port: int,
            family: int = 0,
            socktype: int = 0,
            *args: object,
            **kwargs: object,
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            nonlocal gai_calls
            gai_calls += 1
            ip = "93.184.216.34" if gai_calls == 1 else "127.0.0.1"
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (ip, int(svc_port)),
                )
            ]

        connect_targets: list[object] = []

        def _record_connect(self: socket.socket, address: object) -> None:
            connect_targets.append(address)
            raise ConnectionRefusedError("stubbed connect")

        with (
            patch(
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.socket.getaddrinfo",
                side_effect=_rebinding_getaddrinfo,
            ),
            patch.object(socket.socket, "connect", _record_connect),
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_dns_pin",
                vapid_private_key=vapid["private_key"],
                vapid_claim_email="admin@example.com",
            )

        assert gai_calls == 1
        assert connect_targets == [("93.184.216.34", 443)]

    @pytest.mark.asyncio
    async def test_legacy_stored_non_public_endpoint_rejected_before_webpush(
        self,
    ) -> None:
        """Non-public endpoints already stored in DB are skipped before calling webpush."""
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://169.254.169.254/latest/meta-data",
                "p256dh_key": "p256dh-bob",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-bob",  # pragma: allowlist secret  # gitleaks:allow
            },
        ]
        with (
            patch(
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch("rootcoz.notifications.webpush") as mock_webpush,
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_legacy",
                vapid_private_key="fake-key",  # pragma: allowlist secret
                vapid_claim_email="admin@example.com",
            )
        mock_webpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_slow_drip_response_aborted_at_total_deadline(
        self, tmp_path: Path
    ) -> None:
        """A server trickling bytes inside the per-read timeout still hits the total deadline."""
        stop_drip = threading.Event()

        class _SlowDripHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_len = int(self.headers.get("Content-Length", "0"))
                if content_len:
                    self.rfile.read(content_len)
                self.send_response(200)
                self.send_header("Content-Length", "10000")
                self.end_headers()
                for _ in range(200):
                    if stop_drip.is_set():
                        break
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except OSError:
                        break
                    stop_drip.wait(0.05)

            def log_message(self, format: str, *args: object) -> None:
                return

        cert_path, key_path = _write_self_signed_cert(tmp_path, "drip.example.com")

        class _ThreadedHTTPServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

        https_server = _ThreadedHTTPServer(("127.0.0.1", 0), _SlowDripHandler)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        https_server.socket = ssl_ctx.wrap_socket(https_server.socket, server_side=True)
        https_port = https_server.server_address[1]
        https_thread = threading.Thread(target=https_server.serve_forever, daemon=True)
        https_thread.start()

        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": f"https://drip.example.com:{https_port}/sub/{i}",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
            for i in range(4)
        ]

        orig_session_cls = notifications_mod._WebPushSession

        class _TestCertWebPushSession(orig_session_cls):
            def __init__(self) -> None:
                super().__init__()
                self.verify = str(cert_path)

        def _fake_getaddrinfo(
            host: str,
            svc_port: int,
            *args: object,
            **kwargs: object,
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            if host == "drip.example.com":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", int(svc_port)),
                    )
                ]
            raise OSError(f"unexpected host {host}")

        try:
            with (
                patch(
                    "rootcoz.notifications.get_push_subscriptions_for_users",
                    new_callable=AsyncMock,
                    return_value=subscriptions,
                ),
                patch.object(
                    notifications_mod, "_WebPushSession", _TestCertWebPushSession
                ),
                patch.object(notifications_mod, "WEBPUSH_TIMEOUT_SECONDS", 0.3),
                patch.object(notifications_mod, "is_public_ip", return_value=True),
                patch(
                    "rootcoz.notifications.socket.getaddrinfo",
                    side_effect=_fake_getaddrinfo,
                ),
            ):
                start = time.monotonic()
                await asyncio.wait_for(
                    send_mention_notifications(
                        mentioned_usernames=["bob"],
                        comment_author="alice",
                        job_id="job-1",
                        test_name="test_slow_drip",
                        vapid_private_key=vapid["private_key"],
                        vapid_claim_email="admin@example.com",
                    ),
                    timeout=3.0,
                )
                elapsed = time.monotonic() - start
                assert elapsed < 2.0
        finally:
            stop_drip.set()
            https_server.shutdown()
            https_server.server_close()
            https_thread.join(timeout=2.0)

    @pytest.mark.asyncio
    async def test_dns_stall_times_out_without_blocking_executor(self) -> None:
        """Stalled DNS lookups time out promptly and free _PUSH_EXECUTOR workers."""
        release_dns = threading.Event()
        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": f"https://stalled-dns-{i}.example.com/push",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
            for i in range(4)
        ]

        def _stalling_getaddrinfo(*args: object, **kwargs: object) -> list[object]:
            release_dns.wait(5.0)
            raise socket.gaierror("stalled")

        try:
            with (
                patch(
                    "rootcoz.notifications.get_push_subscriptions_for_users",
                    new_callable=AsyncMock,
                    return_value=subscriptions,
                ),
                patch.object(notifications_mod, "WEBPUSH_TIMEOUT_SECONDS", 0.2),
                patch(
                    "rootcoz.notifications.socket.getaddrinfo",
                    side_effect=_stalling_getaddrinfo,
                ),
            ):
                start = time.monotonic()
                await asyncio.wait_for(
                    send_mention_notifications(
                        mentioned_usernames=["bob"],
                        comment_author="alice",
                        job_id="job-1",
                        test_name="test_dns_stall",
                        vapid_private_key=vapid["private_key"],
                        vapid_claim_email="admin@example.com",
                    ),
                    timeout=2.5,
                )
                elapsed = time.monotonic() - start
                assert elapsed < 1.5

                # _PUSH_EXECUTOR workers are free immediately after DNS timeouts.
                loop = asyncio.get_running_loop()
                probe = await asyncio.wait_for(
                    loop.run_in_executor(
                        notifications_mod._PUSH_EXECUTOR, lambda: "free"
                    ),
                    timeout=0.5,
                )
                assert probe == "free"
        finally:
            release_dns.set()

    @pytest.mark.asyncio
    async def test_proxy_env_vars_ignored_and_proxy_manager_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment proxies cannot bypass connect-time public-IP validation."""
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8888")

        with notifications_mod._WebPushSession() as session:
            assert session.trust_env is False

        adapter = notifications_mod._PublicHTTPSAdapter()
        with pytest.raises(ValueError, match="Proxies are disabled"):
            adapter.proxy_manager_for("http://127.0.0.1:8888")

        vapid = _generate_vapid_keys()
        p256dh, auth = _make_real_push_keys()
        subscriptions = [
            {
                "username": "bob",
                "endpoint": "https://rebind.example.com/push",
                "p256dh_key": p256dh,
                "auth_key": auth,
            }
        ]

        connect_calls: list[object] = []

        def _forbid_connect(self: socket.socket, address: object) -> None:
            connect_calls.append(address)
            raise AssertionError(f"must not connect to {address}")

        with (
            patch(
                "rootcoz.notifications.get_push_subscriptions_for_users",
                new_callable=AsyncMock,
                return_value=subscriptions,
            ),
            patch(
                "rootcoz.notifications.socket.getaddrinfo",
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", 443),
                    )
                ],
            ),
            patch.object(socket.socket, "connect", _forbid_connect),
        ):
            await send_mention_notifications(
                mentioned_usernames=["bob"],
                comment_author="alice",
                job_id="job-1",
                test_name="test_proxy_bypass",
                vapid_private_key=vapid["private_key"],
                vapid_claim_email="admin@example.com",
            )

        assert connect_calls == []


async def test_send_mention_notifications_skips_deleted_and_disabled_users(
    temp_db_path, monkeypatch
) -> None:
    """Web Push delivery excludes deleted, disabled, and pending accounts (#603)."""
    from rootcoz import storage

    monkeypatch.setattr(storage, "DB_PATH", temp_db_path)
    await storage.init_db()
    await storage.create_user("active_user", role="reviewer", status="active")
    await storage.create_user("disabled_user", role="reviewer", status="active")
    await storage.create_user("deleted_user", role="reviewer", status="active")

    for name in ("active_user", "disabled_user", "deleted_user", "admin"):
        await storage.save_push_subscription(
            name,
            f"https://push.example.com/sub/{name}",
            "p256dh-key",  # pragma: allowlist secret  # gitleaks:allow
            "auth-key",  # pragma: allowlist secret  # gitleaks:allow
        )

    await storage.set_user_disabled("disabled_user", True)
    await storage.delete_user("deleted_user")

    delivered: list[str] = []
    vapid = _generate_vapid_keys()

    def _fake_webpush(subscription_info, **_kwargs):
        delivered.append(subscription_info["endpoint"])

    with patch("rootcoz.notifications.webpush", side_effect=_fake_webpush):
        await send_mention_notifications(
            mentioned_usernames=[
                "active_user",
                "disabled_user",
                "deleted_user",
                "admin",
            ],
            comment_author="commenter",
            job_id="job-603",
            test_name="test_lifecycle",
            vapid_private_key=vapid["private_key"],
            vapid_claim_email="admin@example.com",
        )

    assert set(delivered) == {
        "https://push.example.com/sub/active_user",
        "https://push.example.com/sub/admin",
    }
