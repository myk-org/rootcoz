"""Tests for the single shared URL predicate in ``rootcoz.url_utils`` (#562).

The predicate is the only implementation of the whitespace/control-character
+ ``urlparse`` + ``port`` + scheme + hostname checks, so every caller that
validates a URL is pinned through it here.
"""

import pytest

from rootcoz.url_utils import (
    _parse_valid_url,
    sanitize_clone_url,
    sanitize_http_href,
    strip_nonprintable,
    validate_http_url,
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
