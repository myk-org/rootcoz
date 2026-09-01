"""Shared Greenwave configuration and transport-policy helpers."""

from __future__ import annotations

import string
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class GreenwaveTransportPolicy:
    """Result of evaluating one ResultsDB or WaiverDB write transport."""

    base_url: str | None = None
    error: str | None = None
    insecure_http: bool = False


def referenced_placeholders(template: str) -> set[str]:
    """Return the set of field names referenced as placeholders in *template*.

    This is the single canonical placeholder-parsing primitive shared by
    ``config.py`` (auto-push validation) and ``greenwave_exporter.py``
    (render-time guard).  Both modules import from here to avoid duplicating
    the ``string.Formatter().parse()`` iteration.
    """
    return {ph for _, ph, _, _ in string.Formatter().parse(template) if ph is not None}


def normalize_greenwave_outcome_map(
    entries: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Casefold classification keys and reject ambiguous duplicates."""
    normalized: dict[str, str] = {}
    original_keys: dict[str, str] = {}
    for classification, outcome in entries:
        normalized_classification = classification.casefold()
        if normalized_classification in normalized:
            previous = original_keys[normalized_classification]
            raise ValueError(
                "Duplicate Greenwave outcome-map classification keys after "
                f"case-insensitive normalization: {previous!r} and "
                f"{classification!r}"
            )
        normalized[normalized_classification] = outcome
        original_keys[normalized_classification] = classification
    return normalized


def evaluate_greenwave_transport(
    url: str,
    *,
    service: str,
    auth_method: str,
    verify: bool | str,
) -> GreenwaveTransportPolicy:
    """Validate, normalize, and apply policy to one Greenwave write URL.

    Every write uses HTTPS by default. HTTP is permitted only for
    unauthenticated (``none``) writes when TLS verification is explicitly
    disabled (effective httpx ``verify`` is exactly ``False``). All
    authenticated methods — token, oidc, kerberos, and ssl — require HTTPS
    regardless of the ``verify`` setting, so credentials are never transmitted
    over plaintext HTTP. A CA-bundle path keeps writes HTTPS-only even when
    the separate verification boolean is false.
    """
    raw_url = url.strip()
    if not raw_url or any(
        character.isspace() or ord(character) < 32 for character in raw_url
    ):
        return GreenwaveTransportPolicy(error=f"{service} URL is malformed")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port  # Force malformed and out-of-range port validation.
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError:
        return GreenwaveTransportPolicy(error=f"{service} URL is malformed")

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return GreenwaveTransportPolicy(
            error=f"{service} URL must use HTTP(S) and include a hostname"
        )
    if username is not None or password is not None:
        return GreenwaveTransportPolicy(
            error=f"{service} URL must not contain embedded credentials"
        )
    if "?" in raw_url or "#" in raw_url:
        return GreenwaveTransportPolicy(
            error=f"{service} URL must not contain a query string or fragment"
        )
    if parsed.netloc.endswith(":") or "\\" in parsed.netloc:
        return GreenwaveTransportPolicy(error=f"{service} URL is malformed")

    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    normalized_netloc = (
        f"{normalized_host}:{port}" if port is not None else normalized_host
    )
    normalized_path = parsed.path.rstrip("/")
    base_url = urlunsplit(
        (parsed.scheme.lower(), normalized_netloc, normalized_path, "", "")
    )

    if parsed.scheme.lower() != "http":
        return GreenwaveTransportPolicy(base_url=base_url)
    if auth_method != "none" or verify is not False:
        return GreenwaveTransportPolicy(
            error=(
                f"{service} writes require HTTPS; HTTP is allowed only for "
                "unauthenticated (none) auth with effective GREENWAVE_VERIFY_SSL=false and "
                "no CA bundle"
            )
        )

    return GreenwaveTransportPolicy(base_url=base_url, insecure_http=True)
