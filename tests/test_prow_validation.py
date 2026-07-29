"""Tests for shared Prow validation helpers."""

import pytest

from rootcoz.prow_validation import (
    normalize_gcs_bucket,
    normalize_prow_url,
    sanitize_http_href,
    strip_url_userinfo,
    validate_gcs_prefix_suffix,
    validate_prow_build_id,
    validate_prow_job_name,
)


class TestNormalizeProwUrl:
    def test_accepts_https(self):
        assert (
            normalize_prow_url("https://prow.example.com") == "https://prow.example.com"
        )

    def test_rejects_http(self):
        with pytest.raises(ValueError, match="https://"):
            normalize_prow_url("http://prow.example.com")

    def test_rejects_credentials(self):
        url = "https://user:pass@prow.example.com"  # pragma: allowlist secret
        with pytest.raises(ValueError, match="credentials"):
            normalize_prow_url(url)

    def test_rejects_empty_userinfo(self):
        with pytest.raises(ValueError, match="credentials"):
            normalize_prow_url("https://@prow.example.com")
        with pytest.raises(ValueError, match="credentials"):
            normalize_prow_url("https://:@prow.example.com")

    def test_empty_allowed(self):
        assert normalize_prow_url("") == ""
        assert normalize_prow_url(None) == ""


class TestNormalizeGcsBucket:
    def test_valid_bucket(self):
        assert normalize_gcs_bucket("test-platform-results") == "test-platform-results"

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError, match="lowercase"):
            normalize_gcs_bucket("MyBucket")


class TestValidateGcsPrefixSuffix:
    def test_valid_suffix(self):
        validate_gcs_prefix_suffix("logs/my-job/42", "my-job", "42")

    def test_invalid_suffix(self):
        with pytest.raises(ValueError, match="must end with"):
            validate_gcs_prefix_suffix("logs/other/99", "my-job", "42")


class TestSanitizeHttpHref:
    def test_strips_credentials(self):
        url = (
            "https://user:pass@prow.example.com/view/gs/b/p"  # pragma: allowlist secret
        )
        safe = sanitize_http_href(url)
        assert "user" not in safe
        assert safe.startswith("https://prow.example.com")

    def test_rejects_javascript(self):
        assert sanitize_http_href("javascript:alert(1)") == ""

    def test_accepts_https_build_url(self):
        assert (
            sanitize_http_href("https://prow.example.com/view/gs/bucket/logs/job/1")
            == "https://prow.example.com/view/gs/bucket/logs/job/1"
        )


class TestValidateProwJobName:
    def test_valid_name(self):
        assert validate_prow_job_name("periodic-ci-e2e-aws") == "periodic-ci-e2e-aws"

    def test_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            validate_prow_job_name("job/name")


class TestValidateProwBuildId:
    def test_valid_id(self):
        assert validate_prow_build_id("2072319655766134784") == "2072319655766134784"

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="numeric"):
            validate_prow_build_id("abc")


class TestStripUrlUserinfo:
    def test_no_change_without_userinfo(self):
        url = "https://prow.example.com/path"
        assert strip_url_userinfo(url) == url

    def test_strips_empty_userinfo(self):
        assert strip_url_userinfo("https://@prow.example.com/path") == (
            "https://prow.example.com/path"
        )
        assert strip_url_userinfo("https://:@prow.example.com/path") == (
            "https://prow.example.com/path"
        )
