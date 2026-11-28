"""Cross-cutting URL helpers.

Three jobs: sanitizing HTTP(S) URLs before they are stored or shown (userinfo,
clone URLs, hrefs), answering whether a URL may be handed to a browser as a
navigation target — the rule the SSO logout path and the issuer settings share —
and answering whether a URL names this deployment's own origin, the proof the
SSO routes require of a write.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import ParseResult, urlparse, urlunparse

# Fallback for values urlparse rejects (e.g. an unmatched "[" in the
# netloc): the userinfo-shaped prefix before the path is dropped/masked —
# failing closed beats raising from a settings export (#361 Qodo r2).
_USERINFO_PREFIX = re.compile(r"(?<=//)[^/?#\s]+@")


def _parse_valid_url(
    raw: str,
    *,
    allowed_schemes: tuple[str, ...],
    reject_userinfo: bool = False,
    reject_query_fragment: bool = False,
) -> ParseResult | None:
    """Parse an already-stripped, non-empty *raw* URL, or ``None`` if invalid.

    The one implementation of the five shared URL checks: no internal
    whitespace **and** no non-printable character (``isprintable()`` is True
    for a plain space, so ``isspace()`` is needed too — and ``isspace()``
    alone lets ``\\x00``/``\\x1b``/``\\x7f``/Unicode ``Cc``/``Cf`` through), an
    authority ``urlparse`` accepts, a port it accepts, an allowed scheme, and
    a non-empty hostname. *reject_userinfo* and *reject_query_fragment* add
    the stricter checks a gating write target needs (embedded credentials and
    token-carrying query/fragment).
    """
    if has_whitespace_or_control(raw):
        return None
    try:
        parsed = urlparse(raw)
        _ = parsed.port  # Reject a malformed port (:badport) before use.
    except ValueError:  # Malformed authority, e.g. an unmatched IPv6 bracket.
        return None
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        return None
    if reject_userinfo and (parsed.username is not None or parsed.password is not None):
        return None
    if reject_query_fragment and (parsed.query or parsed.fragment):
        return None
    return parsed


def strip_nonprintable(text: str) -> str:
    """Drop control and format characters — log- and message-forging bytes.

    Strictest of the text sanitizers: it keeps only what Python calls
    printable. Ordinary ASCII space (``U+0020``) IS printable and is kept;
    tabs, newlines and other non-printables are dropped. Use it for a
    one-line display value, not for free text —
    ``rootcoz.utils.strip_gating_text`` is the gating-system rule.
    """
    return "".join(ch for ch in text if ch.isprintable())


def has_whitespace_or_control(text: str) -> bool:
    """True when *text* holds whitespace or a non-printable character."""
    return any(ch.isspace() or not ch.isprintable() for ch in text)


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


_NON_PUBLIC_HOSTS = frozenset({"localhost", "local", "internal"})
_NON_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only when *ip* is globally routable."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id is not None:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _parse_ip_literal(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse *host* as an IPv4/IPv6 literal (including non-canonical IPv4 forms)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def validate_public_https_url(value: str, *, field_name: str = "URL") -> str:
    """Validate *value* as a public ``https://`` URL without embedded credentials.

    Rejects non-HTTPS schemes, whitespace/control chars, embedded userinfo,
    non-public IP literals (loopback, link-local/metadata, RFC1918, ULA,
    multicast, reserved, unspecified, IPv4-mapped non-public), and local/internal
    or single-label hostnames. Never echoes *value* in error messages.
    """
    error = f"{field_name} must be a valid public https:// URL"
    parsed = _parse_valid_url(value, allowed_schemes=("https",))
    if parsed is None:
        raise ValueError(error)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(error)
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        raise ValueError(error)
    ip = _parse_ip_literal(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise ValueError(error)
        return value
    if (
        host in _NON_PUBLIC_HOSTS
        or host.endswith(_NON_PUBLIC_HOST_SUFFIXES)
        or "." not in host
        or "" in host.split(".")
    ):
        raise ValueError(error)
    return value


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


#: Hosts for which plaintext http is acceptable — a loopback development server
#: has no network segment to intercept.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_safe_navigation_url(url: str, *, allow_loopback_http: bool = False) -> bool:
    """Whether a URL may be handed to a browser as a redirect target.

    One rule for every place that answers "may the browser go here": https
    always, plaintext http only for a loopback host when the caller opts in
    (a local IdP), and never a URL carrying credentials, a fragment or a port
    that does not parse — the first two exist to move data, the last one makes
    the request that follows fail instead.

    Args:
        url: The candidate URL.
        allow_loopback_http: Accept http when the host is loopback (dev IdP).

    Returns:
        True when the URL is safe to navigate to.
    """
    if not url:
        return False
    # A control character can split a header or a log line; nothing legitimate
    # carries one. Checked before stripping: a trailing newline is exactly the
    # header-injection shape.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return False
    candidate = url.strip()
    cleaned = strip_url_userinfo(candidate)
    if cleaned != candidate:
        # Credentials were present: drop the URL rather than silently rewrite it.
        return False
    try:
        parsed = urlparse(cleaned)
        # Touching .port is the check, not a no-op: urlparse accepts
        # "https://idp.example:bad" and only the property access raises. A
        # discovery document publishing such an endpoint would otherwise be
        # cached as valid and fail later, inside the HTTP client.
        _ = parsed.port
    except ValueError:  # invalid IPv6 authority or a non-numeric port: fail closed
        return False
    if parsed.fragment:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return allow_loopback_http and (parsed.hostname or "").lower() in LOOPBACK_HOSTS
    return False


#: Default port per scheme, so ``https://host`` and ``https://host:443`` are one
#: origin while ``https://host:444`` is not.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> tuple[str, str, int] | None:
    """Return the ``(scheme, hostname, port)`` origin *url* names, or ``None``.

    Only an HTTP(S) URL with a real authority has an origin here: a bare path,
    a scheme-relative ``//host``, ``null``, a control character or an
    unparseable port names none, so a caller comparing origins fails closed
    instead of matching a fragment of the value.

    Args:
        url: The candidate URL.

    Returns:
        The origin, with a lowercase scheme and host and the *effective* port —
        the scheme's default when the URL omits it — or ``None``.
    """
    raw = (url or "").strip()
    if not raw or has_whitespace_or_control(raw):
        return None
    try:
        parsed = urlparse(raw)
        port = parsed.port  # Rejects a malformed port (:badport) before use.
    except ValueError:  # Malformed authority, e.g. an unmatched IPv6 bracket.
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parsed.hostname:
        return None
    return scheme, parsed.hostname, _DEFAULT_PORTS[scheme] if port is None else port


def is_same_origin(source: str, base: str) -> bool:
    """Whether *source* names the same origin as *base* — scheme, host and port.

    The scheme is part of the comparison because an ``http`` page on the
    deployment's own host is a *different* origin, and it is the case a proxy
    deployment cannot otherwise see: when the trusted proxy authenticates the
    request itself (``X-Forwarded-User``), nothing about the request reveals
    which page started it, so the origin is the only proof left. The port counts
    for the same reason: ``https://host:8443`` is not ``https://host``.

    Args:
        source: The candidate, typically an ``Origin`` or ``Referer`` header.
        base: The deployment's own base URL.

    Returns:
        True only when both name the same origin. An unset or incomplete *base*
        is never the same origin as anything.
    """
    candidate = origin_of(source)
    return candidate is not None and candidate == origin_of(base)


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
# RFC 3986 §3.1 schemes are case-insensitive (HTTPS://, HTTP://, GIT://).
_URL_IN_TEXT = re.compile(r"(?:https?|git)://[^\s\"'<>`]+", re.IGNORECASE)


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
    r"@[^@/?#\s]+"
)  # any `@<run>`, not only one at the end of the pre-?/# head: a value with
# trailing prose ("repo@TOKEN and more") kept its token when the pattern was
# `$`-anchored, and masking over-reach in a malformed stored value is safer
# than a leaked credential in the log that reports it.


def _mask_one_url_value(segment: str) -> str:
    """Mask userinfo, ``@token`` suffix and query of one ``scheme://`` segment.

    A whole-value masker has no "stop at the first whitespace" failure mode:
    an arbitrary scheme, an internal space, and the RootCoz ``@<token>``
    repo-spec suffix are all covered. String-splitting, never ``urlparse``, so
    a malformed authority (unmatched IPv6 bracket) is masked too.
    """
    match = _SCHEME.match(segment)
    scheme_end = match.end() if match else 0
    head_end = _query_start(segment)
    head = segment[:head_end]
    # Mask userinfo inside the authority only (up to the first ``/``).
    path_start = head.find("/", scheme_end)
    authority_end = len(head) if path_start == -1 else path_start
    authority = head[scheme_end:authority_end]
    if "@" in authority:
        authority = "***@" + authority.rsplit("@", 1)[1]
        head = segment[:scheme_end] + authority + segment[authority_end:head_end]
    # The ``@token`` suffix is masked on the pre-cut head: after the query cut
    # the ``$`` anchor would see ``?foo=1`` and keep the token verbatim.
    slash = head.find("/", scheme_end)
    if slash != -1 and _AT_TOKEN_SUFFIX.search(head[slash:]):
        head = head[:slash] + _AT_TOKEN_SUFFIX.sub("@***", head[slash:])
    if head_end == len(segment):
        return head
    # Keep the whitespace that separated this URL from a following one.
    return f"{head}?***{segment[len(segment.rstrip()) :]}"


def mask_url_value(text: str) -> str:
    """Mask the credentials of every whole URL in **value** *text* (display only).

    Unlike :func:`mask_url_credentials_in_text` this has no "stop at the first
    whitespace" failure mode, and it masks **every** ``scheme://`` occurrence,
    so a value holding two URLs does not keep the second one's credentials.

    ``shortcut:`` text that is not URL-shaped is returned unchanged — a masker
    is not a sanitizer; :func:`mask_stored_url` is the URL-valued-input variant
    and is what a log of a stored value must use.
    """
    if not text or _SCHEME.search(text) is None:
        return text
    text = strip_nonprintable(text)
    starts = [match.start() for match in _SCHEME.finditer(text)]
    parts = [text[: starts[0]]]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        parts.append(_mask_one_url_value(text[start:end]))
    return "".join(parts)


def _mask_schemeless_userinfo(head: str) -> str:
    """Mask the authority userinfo of a schemeless value head (``user:pw@host/x``).

    Only a head with a path is touched, so a bare e-mail address is not
    rewritten. Fail closed on the ambiguity: anything before the first ``@``
    in the authority position is treated as credentials.
    """
    slash = head.find("/")
    if slash == -1:  # No path: not URL-shaped, so not userinfo either.
        return head
    authority = head[:slash]
    if "@" not in authority:
        return head
    return "***@" + authority.rsplit("@", 1)[1] + head[slash:]


def mask_stored_url(text: str) -> str:
    """``mask_url_value`` for a stored URL value whose scheme may be missing.

    ``mask_url_value`` leaves a schemeless value unchanged, so a stored
    ``github.com/org/repo?token=SECRET`` — or a schemeless ``user:pw@host/repo``
    — reached the debug log and the malformed-value WARNINGs verbatim. All
    three credential carriers are masked here — authority userinfo, the
    ``@<token>`` suffix and the query/fragment — and a schemed value is masked
    exactly as before (display only: what the app does with the stored value is
    unchanged).
    """
    text = strip_nonprintable(text)
    head_end = _query_start(text)
    head = text[:head_end]
    if _SCHEME.match(head) is None:
        head = _mask_schemeless_userinfo(head)
    # Only the text after the last ``/`` may carry a repo-spec token. Masking
    # the whole head would eat the host of an authority userinfo that this
    # function has just rewritten to ``***@host``.
    if "/" in head:
        tail_start = head.rfind("/") + 1
        head = head[:tail_start] + _AT_TOKEN_SUFFIX.sub("@***", head[tail_start:])
    elif any(char.isspace() for char in text):
        # No path, but prose around it: a value carrying words is not an
        # address, so an ``@<run>`` in it is a token, not a domain. This closes
        # the last leak Qodo found -- ``repo@ghp_… trailing`` was left whole
        # because the path branch never ran. ``user@example.com`` has no
        # whitespace, so it still survives untouched.
        head = _AT_TOKEN_SUFFIX.sub("@***", head)
    return mask_url_value(head if head_end == len(text) else f"{head}?***")
