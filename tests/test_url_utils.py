"""Tests for the single shared URL predicate in ``rootcoz.url_utils`` (#562).

The predicate is the only implementation of the whitespace/control-character
+ ``urlparse`` + ``port`` + scheme + hostname checks, so every caller that
validates a URL is pinned through it here.
"""

import pytest

from rootcoz.url_utils import (
    _parse_valid_url,
    mask_stored_url,
    mask_url_value,
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
