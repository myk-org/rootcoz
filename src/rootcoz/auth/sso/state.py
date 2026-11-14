"""Sealed state for an in-flight SSO login.

The browser is anonymous between ``/login`` and ``/callback``, so the CSRF
nonce, the PKCE verifier and the intended destination travel in one encrypted
cookie. Encryption (not just signing) keeps the verifier out of the browser's
reach while still letting the callback prove it issued the value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from rootcoz.encryption import decrypt_sealed_or_none, encrypt_value

#: Cookie carrying the sealed attempt.
STATE_COOKIE = "rootcoz_sso_state"

#: Cookie binding a first-login ticket to the browser it was issued to. The
#: ticket in the URL is the identity half; this is the "same browser" half. A
#: stolen link without it is dead — otherwise a thief could attach the victim's
#: IdP identity to the thief's *own* account and receive every later login.
TICKET_COOKIE = "rootcoz_sso_ticket"

#: What a sealed cookie must say it is. Binding the purpose means a sealed value
#: minted for another feature can never be replayed here.
_STATE_PURPOSE = "rootcoz.sso.login_attempt"

#: Maximum length of the ``next`` destination we accept, so a hostile link cannot
#: turn the callback into a redirector with a kilobyte payload.
MAX_NEXT_LENGTH = 512

#: What an attempt is for. ``login`` resolves or offers to create an account;
#: ``link`` attaches the provider identity to the account already signed in and
#: never touches account creation.
MODE_LOGIN = "login"
MODE_LINK = "link"
MODES = frozenset({MODE_LOGIN, MODE_LINK})


class StateError(Exception):
    """The state cookie is missing, unreadable, expired or not ours."""


@dataclass(frozen=True)
class LoginAttempt:
    """What the callback needs to finish a login it did not start.

    Attributes:
        provider: Provider the attempt was started for.
        state: CSRF nonce echoed by the provider.
        code_verifier: PKCE verifier for the token exchange.
        nonce: OIDC nonce echoed into the ID token, so the token is bound to this
            attempt and not just to this client.
        next_path: Where to send the user afterwards (already validated).
        mode: ``login`` or ``link`` — sealed, so a callback cannot be talked into
            linking when it was started as a login (or the reverse) by editing a
            query parameter.
        link_to: For a ``link`` attempt, the account that started it. Sealed for
            the same reason: the cookie binds the attempt to a *browser*, and
            without this a flow begun by one person could be finished into
            somebody else's account.
    """

    provider: str
    state: str
    code_verifier: str
    nonce: str
    next_path: str
    mode: str = MODE_LOGIN
    link_to: str = ""


def ticket_binding(ticket: str) -> str:
    """The value the browser must present alongside a ticket.

    Args:
        ticket: The raw ticket from the URL fragment.

    Returns:
        A digest of the ticket. The cookie carries this rather than the ticket
        itself, so the two halves only work together.
    """
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def safe_next(next_path: str) -> str:
    """Accept only a same-site absolute path as a post-login destination.

    Args:
        next_path: The caller-supplied destination.

    Returns:
        The destination, or ``/`` when it is empty, over-long, or could send the
        browser off-site (``//host``, ``http://...``, a backslash trick).
    """
    candidate = next_path.strip()
    if not candidate or len(candidate) > MAX_NEXT_LENGTH:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return "/"
    return candidate


def _pack(token: str) -> str:
    """Encode a sealed token for a cookie value.

    A Fernet token contains ``:`` and ``=`` and ``+``/``/``, which Python's
    cookie morsel would quote — and a quoted value is a fragile thing to send
    through proxies and back. Base64url keeps the cookie plain and re-encodable.

    Args:
        token: The ciphertext.

    Returns:
        A URL-safe, unpadded encoding of the ciphertext.
    """
    return base64.urlsafe_b64encode(token.encode("ascii")).decode("ascii").rstrip("=")


def _unpack(value: str) -> str:
    """Decode a packed cookie value back to its ciphertext.

    Args:
        value: The cookie value as received.

    Returns:
        The ciphertext.

    Raises:
        StateError: When the value is not a packed token.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise StateError("state cookie unreadable") from exc


def code_challenge(verifier: str) -> str:
    """Derive the S256 PKCE challenge for a verifier.

    Args:
        verifier: The PKCE code verifier.

    Returns:
        The base64url-encoded SHA-256 digest, unpadded as RFC 7636 requires.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_attempt(
    *,
    provider: str,
    next_path: str,
    ttl_seconds: int,
    mode: str = MODE_LOGIN,
    link_to: str = "",
) -> tuple[LoginAttempt, str, str]:
    """Start a login or link attempt.

    Args:
        provider: Provider name the attempt is for.
        next_path: Requested post-login destination (sanitised here).
        ttl_seconds: How long the attempt stays valid.
        mode: ``login`` (resolve an account) or ``link`` (attach this identity to
            the account already signed in). Sealed with the rest of the attempt,
            so the callback acts on what was started, not on what the query
            string says now.
        link_to: The account a ``link`` attempt belongs to (ignored for a login).

    Raises:
        ValueError: When ``mode`` is neither of :data:`MODES`.

    One sealed cookie per attempt. It is deleted when the login completes and
    left in place after a refusal until its TTL, so a transient provider error
    can be retried; replay is bounded by the single-use authorization code and
    PKCE, not by the cookie.

    Returns:
        The attempt, its sealed cookie value, and the PKCE challenge to send to
        the provider.
    """
    verifier = secrets.token_urlsafe(48)
    if mode not in MODES:
        msg = f"Unknown SSO attempt mode: {mode}"
        raise ValueError(msg)
    attempt = LoginAttempt(
        provider=provider,
        state=secrets.token_urlsafe(32),
        code_verifier=verifier,
        nonce=secrets.token_urlsafe(32),
        next_path=safe_next(next_path),
        mode=mode,
        link_to=link_to if mode == MODE_LINK else "",
    )
    payload = {
        "purpose": _STATE_PURPOSE,
        "provider": attempt.provider,
        "state": attempt.state,
        "verifier": attempt.code_verifier,
        "nonce": attempt.nonce,
        "next": attempt.next_path,
        "mode": attempt.mode,
        "link_to": attempt.link_to,
        "issued_at": int(time.time()),
        "ttl": ttl_seconds,
    }
    sealed = _pack(encrypt_value(json.dumps(payload, separators=(",", ":"))))
    return attempt, sealed, code_challenge(verifier)


def open_attempt(sealed: str, *, provider: str) -> LoginAttempt:
    """Verify and unpack a sealed attempt.

    Args:
        sealed: The cookie value set by :func:`new_attempt`.
        provider: Provider the callback was reached for.

    Returns:
        The attempt.

    Raises:
        StateError: When the cookie is missing or unreadable, expired, issued
            for another provider, or shaped wrongly.
    """
    if not sealed:
        raise StateError("state cookie missing")
    # ``decrypt_sealed_or_none`` refuses anything the process did not encrypt:
    # the legacy-plaintext passthrough of the general helper would otherwise let
    # a caller hand back their own JSON and be believed.
    raw = decrypt_sealed_or_none(_unpack(sealed))
    if not raw:
        raise StateError("state cookie was not sealed by this server")
    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError("state cookie is not JSON") from exc
    if payload.get("purpose") != _STATE_PURPOSE:
        raise StateError("sealed value was minted for something else")
    ttl = int(payload.get("ttl", 0))
    issued_at = int(payload.get("issued_at", 0))
    if ttl <= 0 or time.time() - issued_at > ttl:
        raise StateError("state expired")
    if payload.get("provider") != provider:
        raise StateError("state was issued for another provider")
    state = payload.get("state")
    verifier = payload.get("verifier")
    nonce = payload.get("nonce")
    if not isinstance(state, str) or not state or not isinstance(verifier, str):
        raise StateError("state is missing its nonce or verifier")
    if not isinstance(nonce, str) or not nonce:
        raise StateError("state is missing its ID-token nonce")
    mode = payload.get("mode", MODE_LOGIN)
    if mode not in MODES:
        raise StateError("state carries an unknown mode")
    return LoginAttempt(
        provider=provider,
        state=state,
        code_verifier=verifier,
        nonce=nonce,
        next_path=safe_next(str(payload.get("next", "/"))),
        mode=mode,
        link_to=str(payload.get("link_to", "")),
    )
