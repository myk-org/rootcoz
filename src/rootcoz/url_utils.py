"""Cross-cutting HTTP(S) URL sanitization helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# Fallback for values urlparse rejects (e.g. an unmatched "[" in the
# netloc): the userinfo-shaped prefix before the path is dropped/masked —
# failing closed beats raising from a settings export (#361 Qodo r2).
_USERINFO_PREFIX = re.compile(r"(?<=//)[^/?#\s]+@")


def strip_url_userinfo(url: str) -> str:
    """Remove userinfo (username/password) from a URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        # ``is not None`` catches empty userinfo (e.g. ``https://@host`` / ``https://:@host``)
        if parsed.username is not None or parsed.password is not None:
            clean_netloc = parsed.netloc.rsplit("@", 1)[-1]
            return urlunparse(parsed._replace(netloc=clean_netloc))
        return url
    except ValueError:  # Invalid IPv6 URL and friends — never raise from export
        return _USERINFO_PREFIX.sub("", url)


def sanitize_http_href(url: str) -> str:
    """Return a safe http(s) URL without credentials, or empty string if invalid."""
    if not url:
        return ""
    cleaned = strip_url_userinfo(url.strip())
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return cleaned


def sanitize_clone_url(url: str) -> str:
    """Return a credential-free HTTP(S) or git clone URL without query or fragment."""
    try:
        cleaned = strip_url_userinfo(url.strip())
        parsed = urlparse(cleaned)
        if (
            parsed.scheme not in ("http", "https", "git")
            or not parsed.hostname
            or any(char.isspace() for char in parsed.netloc)
        ):
            return ""
        _ = parsed.port  # Reject malformed ports before persisting the URL.
        return urlunparse(parsed._replace(query="", fragment=""))
    except ValueError:  # Malformed authority, e.g. an unmatched IPv6 bracket or port.
        return ""


def _query_start(url: str) -> int:
    """Index of the first ``?`` or ``#`` in *url*, or ``len(url)`` if neither."""
    return min(
        (pos for pos in (url.find("?"), url.find("#")) if pos != -1),
        default=len(url),
    )


def strip_url_query(url: str) -> str:
    """Remove the query string and fragment from a URL."""
    try:
        parsed = urlparse(url)
    except ValueError:  # Malformed authority — cut at the first ?/# instead
        return url[: _query_start(url)]
    return urlunparse(parsed._replace(query="", fragment=""))


def strip_url_credentials(url: str) -> str:
    """Remove userinfo, query and fragment — all credential carriers — from a URL."""
    return strip_url_query(strip_url_userinfo(url))


# git:// is a supported clone scheme (RepositoryManager.clone_into
# allowlist: https/git only) — its URLs carry userinfo too (#361 Qodo r2).
_URL_IN_TEXT = re.compile(r"(?:https?|git)://[^\s\"'<>`]+")


def strip_url_userinfo_in_text(text: str) -> str:
    """Strip userinfo from every http(s)/git URL in free text."""
    if not text:
        return text
    return _URL_IN_TEXT.sub(lambda match: strip_url_userinfo(match.group(0)), text)


def mask_url_userinfo_in_text(text: str) -> str:
    """Replace userinfo in http(s)/git URLs in free text with ``***`` (display only)."""
    if not text:
        return text

    def _mask(match: re.Match[str]) -> str:
        url = match.group(0)
        try:
            parsed = urlparse(url)
            if parsed.username is None and parsed.password is None:
                return url
            host = parsed.netloc.rsplit("@", 1)[-1]
            return urlunparse(parsed._replace(netloc=f"***@{host}"))
        except ValueError:  # Invalid IPv6 URL — mask the userinfo-shaped prefix
            return _USERINFO_PREFIX.sub("***@", url)

    return _URL_IN_TEXT.sub(_mask, text)


def mask_url_query_in_text(text: str) -> str:
    """Replace the query/fragment of http(s)/git URLs in free text with ``?***``.

    A query or fragment can carry a token (``?token=SECRET``) — masked like
    userinfo, display only. String-split, not urlparse, so malformed URLs
    (unmatched IPv6 bracket) are masked too.
    """
    if not text:
        return text

    def _mask(match: re.Match[str]) -> str:
        url = match.group(0)
        cut = _query_start(url)
        return f"{url[:cut]}?***" if cut != len(url) else url

    return _URL_IN_TEXT.sub(_mask, text)


def mask_url_credentials_in_text(text: str) -> str:
    """Mask userinfo and query/fragment in http(s)/git URLs (display only)."""
    return mask_url_query_in_text(mask_url_userinfo_in_text(text))


def redact_urls_in_text(text: str) -> str:
    """Replace http(s)/git URLs in free text with credential- and query-free forms."""
    if not text:
        return text
    return _URL_IN_TEXT.sub(lambda match: strip_url_credentials(match.group(0)), text)
