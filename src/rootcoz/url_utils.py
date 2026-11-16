"""Cross-cutting HTTP(S) URL sanitization helpers."""

from __future__ import annotations

import re
from urllib.parse import ParseResult, urlparse, urlunparse

# Fallback for values urlparse rejects (e.g. an unmatched "[" in the
# netloc): the userinfo-shaped prefix before the path is dropped/masked —
# failing closed beats raising from a settings export (#361 Qodo r2).
_USERINFO_PREFIX = re.compile(r"(?<=//)[^/?#\s]+@")


def _parse_valid_url(
    raw: str, *, allowed_schemes: tuple[str, ...]
) -> ParseResult | None:
    """Parse an already-stripped, non-empty *raw* URL, or ``None`` if invalid.

    The one implementation of the five shared URL checks: no internal
    whitespace **and** no non-printable character (``isprintable()`` is True
    for a plain space, so ``isspace()`` is needed too — and ``isspace()``
    alone lets ``\\x00``/``\\x1b``/``\\x7f``/Unicode ``Cc``/``Cf`` through), an
    authority ``urlparse`` accepts, a port it accepts, an allowed scheme, and
    a non-empty hostname.
    """
    if any(ch.isspace() or not ch.isprintable() for ch in raw):
        return None
    try:
        parsed = urlparse(raw)
        _ = parsed.port  # Reject a malformed port (:badport) before use.
    except ValueError:  # Malformed authority, e.g. an unmatched IPv6 bracket.
        return None
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        return None
    return parsed


def strip_nonprintable(text: str) -> str:
    """Drop control and format characters — log- and message-forging bytes."""
    return "".join(ch for ch in text if ch.isprintable())


def validate_http_url(
    value: str | None,
    *,
    field_name: str,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> str | None:
    """Return the stripped *value*, or raise ``ValueError`` naming the field.

    Empty (or ``None``) means "not configured" and passes through; the error
    never echoes the value or ``urlparse`` internals.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return ""
    if _parse_valid_url(raw, allowed_schemes=allowed_schemes) is None:
        schemes = " or ".join(f"{scheme}://" for scheme in allowed_schemes)
        raise ValueError(f"{field_name} must be a valid {schemes} URL")
    return raw


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
    if _parse_valid_url(cleaned, allowed_schemes=("http", "https")) is None:
        return ""
    return cleaned


def sanitize_clone_url(url: str) -> str:
    """Return a credential-free HTTP(S) or git clone URL without query or fragment."""
    if not url:
        return ""
    cleaned = strip_url_userinfo(url.strip())
    parsed = _parse_valid_url(cleaned, allowed_schemes=("http", "https", "git"))
    if parsed is None:
        return ""
    return urlunparse(parsed._replace(query="", fragment=""))


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


_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_AT_TOKEN_SUFFIX = re.compile(
    r"@[^@/?#\s]+$"
)  # anchored at the end of the pre-?/# head


def mask_url_value(text: str) -> str:
    """Mask the credentials of a whole URL *value* (display only).

    Unlike :func:`mask_url_credentials_in_text` this has no "stop at the first
    whitespace" failure mode: it masks the whole value, so an uppercase or
    arbitrary scheme, an internal space, and the RootCoz ``@<token>`` repo-spec
    suffix are all covered. String-splitting, never ``urlparse``, so a
    malformed authority (unmatched IPv6 bracket) is masked too.

    ``shortcut:`` text that is not URL-shaped is returned unchanged — a masker
    is not a sanitizer; upgrade if a schemeless credential carrier ever reaches
    a log or a ``422`` body.
    """
    if not text or _SCHEME.search(text) is None:
        return text
    text = strip_nonprintable(text)
    scheme_end = _SCHEME.search(text).end()  # type: ignore[union-attr]
    head_end = _query_start(text)
    head = text[:head_end]
    # Mask userinfo inside the authority only (up to the first ``/``).
    path_start = head.find("/", scheme_end)
    authority_end = len(head) if path_start == -1 else path_start
    authority = head[scheme_end:authority_end]
    if "@" in authority:
        authority = "***@" + authority.rsplit("@", 1)[1]
        head = text[:scheme_end] + authority + text[authority_end:head_end]
    # The ``@token`` suffix is masked on the pre-cut head: after the query cut
    # the ``$`` anchor would see ``?foo=1`` and keep the token verbatim.
    slash = head.find("/", scheme_end)
    if slash != -1 and _AT_TOKEN_SUFFIX.search(head[slash:]):
        head = head[:slash] + _AT_TOKEN_SUFFIX.sub("@***", head[slash:])
    return head if head_end == len(text) else f"{head}?***"
