"""Tests for the single shared URL predicate in ``rootcoz.url_utils`` (#562).

The predicate is the only implementation of the whitespace/control-character
+ ``urlparse`` + ``port`` + scheme + hostname checks, so every caller that
validates a URL is pinned through it here.
"""

import ipaddress

import pytest

from rootcoz.url_utils import (
    _parse_valid_url,
    is_public_ip,
    is_safe_navigation_url,
    mask_stored_url,
    mask_url_value,
    sanitize_clone_url,
    sanitize_http_href,
    strip_nonprintable,
    validate_http_url,
    validate_public_https_url,
)


class TestParseValidUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo",
            "https://gitlab.internal:8443/org/repo",
            "git://host/repo",
            "http://jenkins.internal:8080",
        ],
    )
    def test_accepts_valid_urls(self, url: str) -> None:
        assert (
            _parse_valid_url(url, allowed_schemes=("http", "https", "git")) is not None
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://[bad/repo",
            "https://github.com:badport/org/repo",
            "https://github.com/org/repo with space",
            "https://github.com/org/repo\tz",
            "https://github.com/org/repo\x00z",
            "https://github.com/org/repo\x1b[31m",
            "https://github.com/org\x07/repo",
            "https://github.com/org/repo\x7f",
            "https://github.com/org/\u200brepo",
            "not-a-valid-url",
            "https://",
        ],
    )
    def test_rejects_malformed_urls(self, url: str) -> None:
        assert _parse_valid_url(url, allowed_schemes=("http", "https", "git")) is None

    def test_rejects_disallowed_scheme(self) -> None:
        assert _parse_valid_url("http://h/p", allowed_schemes=("https",)) is None


class TestValidateHttpUrl:
    def test_none_and_empty_pass_through(self) -> None:
        assert validate_http_url(None, field_name="jenkins_url") is None
        assert validate_http_url("", field_name="jenkins_url") == ""
        assert validate_http_url("   ", field_name="jenkins_url") == ""

    def test_strips_and_returns_value(self) -> None:
        assert (
            validate_http_url(" https://j/p ", field_name="jenkins_url")
            == "https://j/p"
        )

    def test_message_names_the_field_and_never_the_value(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            bad_url = "https://user:secret@[bad/j"  # pragma: allowlist secret
            validate_http_url(bad_url, field_name="jenkins_url")
        message = str(excinfo.value)
        assert message.startswith("jenkins_url must be a valid")
        assert "secret" not in message
        assert "[bad" not in message

    def test_custom_schemes_are_named(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_http_url(
                "http://h/p",
                field_name="tests_repo_url",
                allowed_schemes=("https", "git"),
            )
        assert "https:// or git://" in str(excinfo.value)


class TestSanitizersDegrade:
    @pytest.mark.parametrize(
        "url",
        [
            "https://[bad/repo",
            "https://user:pass@[bad/repo",  # pragma: allowlist secret
            "https://github.com/org/repo with space",
            "https://github.com/org/repo\x00z",
            "javascript:alert(1)",
            " ",
        ],
    )
    def test_sanitize_http_href_returns_empty(self, url: str) -> None:
        assert sanitize_http_href(url) == ""

    @pytest.mark.parametrize(
        "url",
        [
            "https://[bad/repo",
            "https://github.com:badport/org/repo",
            "https://github.com/org/repo with space",
            "ssh://github.com/org/repo",
        ],
    )
    def test_sanitize_clone_url_returns_empty(self, url: str) -> None:
        assert sanitize_clone_url(url) == ""

    def test_sanitize_http_href_keeps_query(self) -> None:
        assert (
            sanitize_http_href("https://prow.example.com/view?x=1")
            == "https://prow.example.com/view?x=1"
        )

    def test_sanitize_clone_url_drops_query_and_fragment(self) -> None:
        clone_url = (
            "https://user:pass@host/repo?token=t#frag"  # pragma: allowlist secret
        )
        assert sanitize_clone_url(clone_url) == "https://host/repo"


class TestStripNonprintable:
    def test_drops_control_and_format_characters_keeps_spaces(self) -> None:
        assert strip_nonprintable("a\x00b\x1b[31mc\x7f d\u200be") == "ab[31mc de"


class TestNavigationUrlMalformedPort:
    """The navigation rule is the one place a malformed port is refused (#561).

    OIDC discovery, the logout endpoint and the issuer settings all ask this
    predicate, so a discovery document publishing ``https://idp.example:bad``
    is rejected at validation instead of being cached and failing later inside
    the HTTP client.
    """

    @pytest.mark.parametrize(
        ("url", "allow_loopback_http"),
        [
            ("https://idp.example:bad", False),
            ("http://localhost:notaport/", True),
            ("http://127.0.0.1:bad/", True),
        ],
    )
    def test_refuses_a_malformed_port(
        self, url: str, allow_loopback_http: bool
    ) -> None:
        assert is_safe_navigation_url(url, allow_loopback_http=allow_loopback_http) is (
            False
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://idp.example:8443/path",
            "https://idp.example",
        ],
    )
    def test_accepts_a_valid_or_absent_port(self, url: str) -> None:
        assert is_safe_navigation_url(url) is True

    @pytest.mark.parametrize(
        ("url", "allow_loopback_http", "expected"),
        [
            ("http://localhost:8080/logout", True, True),
            ("http://localhost:8080/logout", False, False),
            ("http://idp.example:8080/logout", True, False),
        ],
    )
    def test_loopback_http_opt_in_is_unchanged(
        self, url: str, allow_loopback_http: bool, expected: bool
    ) -> None:
        assert (
            is_safe_navigation_url(url, allow_loopback_http=allow_loopback_http)
            is expected
        )


class TestMaskUrlValueEveryOccurrence:
    """A value with several URLs masks every one, not only the first (#581)."""

    def test_masks_credentials_in_the_second_url(self) -> None:
        masked = mask_url_value(
            "see https://a/x and https://u2:p2@h2/r2"  # pragma: allowlist secret
        )
        assert masked == "see https://a/x and https://***@h2/r2"
        assert "p2" not in masked

    def test_masks_the_query_of_the_second_url(self) -> None:
        masked = mask_url_value(
            "https://a/x?token=tok1 https://u2:p2@h2/r2?token=tok2"  # pragma: allowlist secret
        )
        assert masked == "https://a/x?*** https://***@h2/r2?***"
        assert "tok1" not in masked
        assert "tok2" not in masked

    def test_single_url_behaviour_is_unchanged(self) -> None:
        assert (
            mask_url_value("https://github.com/org/repo")
            == "https://github.com/org/repo"
        )
        assert (
            mask_url_value("https://user:pw@h/r?token=tok")  # pragma: allowlist secret
            == "https://***@h/r?***"
        )


class TestMaskStoredUrl:
    """``mask_stored_url`` masks a stored URL value whose scheme may be missing."""

    def test_schemeless_query_is_masked(self) -> None:
        masked = mask_stored_url("github.com/org/repo?token=tok")
        assert masked == "github.com/org/repo?***"
        assert "tok" not in masked

    def test_schemeless_at_token_is_masked(self) -> None:
        masked = mask_stored_url("github.com/org/repo@ghp_secret")
        assert masked == "github.com/org/repo@***"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Schemeless userinfo: the authority, and the same with a port.
            ("user:sec@git.internal/org/repo", "***@git.internal/org/repo"),
            ("user:pw@host:8443/org/repo", "***@host:8443/org/repo"),
            ("user:pw@host/org/repo?token=SECRET", "***@host/org/repo?***"),
        ],
    )
    def test_schemeless_userinfo_is_masked(self, value: str, expected: str) -> None:
        masked = mask_stored_url(value)
        assert masked == expected
        assert "sec" not in masked.removeprefix("***@")
        assert "pw" not in masked
        assert "SECRET" not in masked

    @pytest.mark.parametrize(
        "value",
        [
            # Not URL-shaped at all: a plain path, and a bare e-mail address.
            # Neither carries userinfo, so neither is rewritten.
            "https://github.com/org/repo",
            "github.com/org/repo",
            "user@example.com",
        ],
    )
    def test_values_without_credentials_are_unchanged(self, value: str) -> None:
        assert mask_stored_url(value) == value

    def test_schemed_value_masked_like_mask_url_value(self) -> None:
        credential_url = "https://user:pw@h/r?token=tok"  # pragma: allowlist secret
        assert mask_stored_url(credential_url) == mask_url_value(credential_url)
        assert mask_stored_url("https://github.com/org/repo") == (
            "https://github.com/org/repo"
        )

    def test_control_bytes_never_survive(self) -> None:
        assert mask_stored_url("ab\x00cd\n[AUDIT] forged") == ("abcd[AUDIT] forged")


class TestAtTokenNotAtTheEnd:
    """A token followed by prose must still be masked (#562, Qodo 2a8e9efa)."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("github.com/org/repo@SECRET extra", "github.com/org/repo@*** extra"),
            (
                "https://github.com/org/repo@SECRET extra",
                "https://github.com/org/repo@*** extra",
            ),
            ("github.com/org/repo@SECRET", "github.com/org/repo@***"),
            (
                "see github.com/org/repo@SECRET and more",
                "see github.com/org/repo@*** and more",
            ),
        ],
    )
    def test_a_trailing_token_is_masked_before_the_prose(
        self, value: str, expected: str
    ) -> None:
        assert mask_stored_url(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "user:sec@git.internal/org/repo",
            "user:pw@host:8443/org/repo",
            "https://user:pw@host/repo",  # pragma: allowlist secret
        ],
    )
    def test_the_host_is_never_mistaken_for_a_token(self, value: str) -> None:
        masked = mask_stored_url(value)
        assert "***@***" not in masked, masked
        assert "git.internal" in masked or "host" in masked, masked

    def test_an_email_shaped_value_is_left_alone(self) -> None:
        assert mask_stored_url("user@example.com") == "user@example.com"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("repo@ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaa trailing", "repo@*** trailing"),
            ("not-a-url@SECRETTOKEN and more", "not-a-url@*** and more"),
            ("git.internal@SECRET token here", "git.internal@*** token here"),
        ],
    )
    def test_a_schemeless_value_with_prose_masks_its_token(
        self, value: str, expected: str
    ) -> None:
        """No path, but words around it: a token, not a host (#562)."""
        assert mask_stored_url(value) == expected


class TestIsPublicIp:
    @pytest.mark.parametrize(
        "addr",
        [
            "8.8.8.8",
            "1.1.1.1",
            "142.250.80.46",
            "2607:f8b0:4005:80b::200e",
        ],
    )
    def test_accepts_public_ips(self, addr: str) -> None:
        assert is_public_ip(ipaddress.ip_address(addr)) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "::",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "fe80::1",
            "fe80::1%eth0",
            "fc00::1",
            "fd12::1",
            "100.64.0.1",
            "224.0.0.1",
            "ff02::1",
            "240.0.0.1",
            "255.255.255.255",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "::ffff:10.0.0.1",
            "64:ff9b::127.0.0.1",
        ],
    )
    def test_rejects_non_public_ips(self, addr: str) -> None:
        assert is_public_ip(ipaddress.ip_address(addr)) is False


class TestValidatePublicHttpsUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://fcm.googleapis.com/fcm/send/abc123",
            "https://updates.push.services.mozilla.com/wpush/v2/abc123",
            "https://web.push.apple.com/QAbc123",
            "https://wns2-bl2p.notify.windows.com/w/?token=abc",
            "https://push.example.com:8443/sub/1",
            "https://8.8.8.8/push",
            "https://[2607:f8b0:4005:80b::200e]/push",
        ],
    )
    def test_accepts_public_https_urls(self, url: str) -> None:
        assert validate_public_https_url(url, field_name="Push endpoint") == url

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "http://fcm.googleapis.com/fcm/send/abc123",
            "https://",
            "https://[bad/push",
            "https://push.example.com:badport/push",
            "https://user:pass@push.example.com/push",  # pragma: allowlist secret
            "https://@push.example.com/push",
            "https://push.example.com/push with space",
            " https://push.example.com/push",
            "https://push.example.com/push\n",
            "https://push.example.com/push\x00",
            "https://127.0.0.1/push",
            "https://127.0.0.1:8443/push",
            "https://127.1/push",
            "https://0177.0.0.1/push",
            "https://2130706433/push",
            "https://[::1]/push",
            "https://[::ffff:127.0.0.1]/push",
            "https://169.254.169.254/latest/meta-data",
            "https://[fe80::1]/push",
            "https://[::ffff:169.254.169.254]/push",
            "https://10.0.0.1/internal",
            "https://172.16.0.1/internal",
            "https://192.168.1.1/internal",
            "https://[fd00::1]/internal",
            "https://0.0.0.0/push",
            "https://[::]/push",
            "https://224.0.0.1/push",
            "https://[ff02::1]/push",
            "https://240.0.0.1/push",
            "https://localhost/push",
            "https://LOCALHOST./push",
            "https://sub.localhost/push",
            "https://printer.local/push",
            "https://metadata.google.internal/computeMetadata",
            "https://host.docker.internal/push",
            "https://svc.internal./push",
            "https://sidecar/push",
            "https://.example.com/push",
            "https://a..example.com/push",
        ],
    )
    def test_rejects_invalid_or_non_public_urls(self, url: str) -> None:
        with pytest.raises(ValueError, match="Push endpoint"):
            validate_public_https_url(url, field_name="Push endpoint")

    def test_error_never_echoes_secret_value(self) -> None:
        secret_url = "https://user:s3cr3t@127.0.0.1/push"  # pragma: allowlist secret
        with pytest.raises(ValueError) as excinfo:
            validate_public_https_url(secret_url, field_name="Push endpoint")
        message = str(excinfo.value)
        assert "Push endpoint" in message
        assert "s3cr3t" not in message
        assert "127.0.0.1" not in message
