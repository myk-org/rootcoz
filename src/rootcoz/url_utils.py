"""Cross-cutting HTTP(S) URL sanitization helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# URL userinfo ("scheme://user:pass@host") embedded anywhere in free text —  # pragma: allowlist secret
# e.g. exception messages that quote a request URL.
_EMBEDDED_USERINFO_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/@\s:]+(?::[^/@\s]*)?@)"
)


def redact_url_userinfo(text: str) -> str:
    """Remove userinfo from URLs embedded anywhere in free text.

    Unlike :func:`strip_url_userinfo`, which requires the whole string to be
    a URL, this scans for embedded userinfo in scheme URLs (``user:pass@host``)
    inside arbitrary text (exception messages, log lines) and strips the
    ``user:pass@`` portion of each.
    """
    if not text:
        return text
    return _EMBEDDED_USERINFO_RE.sub(r"\1", text)


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
