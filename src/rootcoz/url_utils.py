"""Cross-cutting HTTP(S) URL sanitization helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


def strip_url_userinfo(url: str) -> str:
    """Remove userinfo (username/password) from a URL."""
    if not url:
        return url
    parsed = urlparse(url)
    # ``is not None`` catches empty userinfo (e.g. ``https://@host`` / ``https://:@host``)
    if parsed.username is not None or parsed.password is not None:
        clean_netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunparse(parsed._replace(netloc=clean_netloc))
    return url


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


def strip_url_query(url: str) -> str:
    """Remove the query string and fragment from a URL."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>`]+")


def strip_url_userinfo_in_text(text: str) -> str:
    """Strip userinfo from every http(s) URL in free text."""
    if not text:
        return text
    return _URL_IN_TEXT.sub(lambda match: strip_url_userinfo(match.group(0)), text)


def mask_url_userinfo_in_text(text: str) -> str:
    """Replace userinfo in http(s) URLs in free text with ``***`` (display only)."""
    if not text:
        return text

    def _mask(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlparse(url)
        if parsed.username is None and parsed.password is None:
            return url
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunparse(parsed._replace(netloc=f"***@{host}"))

    return _URL_IN_TEXT.sub(_mask, text)


def redact_urls_in_text(text: str) -> str:
    """Replace http(s) URLs in free text with credential- and query-free forms."""
    if not text:
        return text
    return _URL_IN_TEXT.sub(
        lambda match: strip_url_query(strip_url_userinfo(match.group(0))), text
    )
