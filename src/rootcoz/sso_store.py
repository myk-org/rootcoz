"""SSO and external-identity persistence (``user_identities``, ``sso_device_codes``, ``sso_link_tickets``).

Extracted from :mod:`rootcoz.storage` so the SSO persistence boundary, its typed
domain exceptions, and its typed exchange result live in one cohesive module.

Dependency direction: this module imports database primitives from
:mod:`rootcoz.storage` (via module reference so test patches on
``storage.DB_PATH`` and ``storage.datetime`` remain authoritative) and
:data:`rootcoz.sso_providers.SSO_PROVIDERS`. It **never** imports
:mod:`rootcoz.auth` or :mod:`rootcoz.config` — policy checks (account
eligibility, domain allow-list) are injected as callbacks by the auth layer.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

import aiosqlite

from rootcoz import get_child_logger
from rootcoz import storage as _storage
from rootcoz.sso_providers import SSO_PROVIDERS

logger = get_child_logger(__name__)

#: How long a first-login choice ticket stays usable.
SSO_TICKET_TTL_SECONDS = 600

#: How many ``user_code`` allocations to try before giving up (collision odds
#: are tiny — 32^8 — but a constraint violation must never reach the caller).
_USER_CODE_ATTEMPTS = 5

#: Predicate every expiring row read uses — SSO device codes and first-login
#: tickets — so "live" means the same thing everywhere and each purge uses its
#: inverse.
_LIVE_ROW = "expires_at > datetime('now')"

#: Claim-name fragments that mark a credential rather than identity data. An
#: OIDC claim set routinely carries ``access_token`` / ``id_token`` / an
#: authorization ``code``, and the audit column must never become a secret store.
_SECRET_CLAIM_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "assertion",
)

#: Signature for an injected account-eligibility callback: ``(user_row, is_admin) -> refusal``
#: returning ``"disabled"`` / ``"locked"`` / ``"pending"`` / ``"rejected"`` or ``""``.
EligibilityCheck = Callable[[dict[str, Any], bool], str]


# ---------------------------------------------------------------------------
# Typed domain exceptions & result types (no string-parsed ValueErrors or
# sentinel dicts across the storage <-> auth seam).
# ---------------------------------------------------------------------------


class IdentityStorageError(ValueError):
    """Base typed error for SSO identity and ticket persistence failures.

    Subclasses ``ValueError`` so callers/tests catching ``ValueError`` remain
    compatible, while auth callers match on the concrete type or ``reason``
    instead of parsing error text.
    """

    reason: str = "identity_error"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class BootstrapAdminLinkError(IdentityStorageError):
    """Attempted to link an external identity to the reserved bootstrap admin."""

    reason = "bootstrap_admin_cannot_link"


class AccountNotFoundError(IdentityStorageError):
    """The target local user row does not exist."""

    reason = "account_missing"


class AccountIneligibleError(IdentityStorageError):
    """The target account is disabled, locked, pending, or rejected."""

    def __init__(self, block_reason: str) -> None:
        self.block_reason = block_reason
        reason = f"account_{block_reason}"
        super().__init__(reason, reason=reason)


class TicketExpiredError(IdentityStorageError):
    """The one-time first-login ticket is expired, unknown, or already spent."""

    reason = "ticket_expired"


class IdentityConflictError(IdentityStorageError):
    """The external ``(provider, subject)`` or ``(user, provider)`` slot is taken."""

    reason = "identity_conflict"


class UsernameTakenError(IdentityStorageError):
    """The chosen username already exists."""

    reason = "invalid_username"


class DeviceCodeCapacityError(ValueError):
    """The deployment already has as many live device codes as it allows.

    Raised instead of a bare ``ValueError`` so the route can answer ``429`` and
    name a retry, rather than surfacing a capacity refusal as a server fault.
    """


@dataclass(frozen=True)
class DeviceCodeExchangeResult:
    """Typed outcome of :func:`exchange_device_code_for_session`.

    Either a newly minted session (``token``, ``role``, ``is_admin``) or a
    policy refusal (``refused``, e.g. ``"account_disabled"`` or
    ``"domain_not_allowed"``). ``None`` from the function itself still means no
    live claim existed to exchange.
    """

    token: str = ""
    role: str = ""
    is_admin: bool = False
    refused: str | None = None

    @property
    def ok(self) -> bool:
        """True when a session token was minted and no refusal applied."""
        return self.refused is None and bool(self.token)


# ---------------------------------------------------------------------------
# Validation & scrubbing helpers
# ---------------------------------------------------------------------------


def _validate_identity_subject(subject: str) -> None:
    """Validate that an IdP subject is a non-empty stable identifier."""
    if not subject.strip():
        msg = "An SSO identity needs a non-empty subject"
        raise ValueError(msg)


def _validate_sso_provider(provider: str) -> None:
    """Validate that ``provider`` names a supported SSO provider."""
    if provider not in SSO_PROVIDERS:
        msg = f"Invalid SSO provider: '{provider}'. Must be one of {SSO_PROVIDERS}."
        raise ValueError(msg)


def _scrub_claims(raw_claims_json: str | None) -> str | None:
    """Remove credential-shaped keys from a claim set before it is stored.

    Scrubbing happens here, not at the call site: this is the last point before
    persistence, so no provider implementation can forget it.
    """
    if raw_claims_json is None:
        return None
    try:
        claims = json.loads(raw_claims_json)
    except json.JSONDecodeError as exc:
        msg = f"raw_claims_json is not valid JSON: {exc.msg}"
        raise ValueError(msg) from exc
    if not isinstance(claims, dict):
        msg = "raw_claims_json must be a JSON object"
        raise ValueError(msg)  # noqa: TRY004

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if not any(part in str(key).lower() for part in _SECRET_CLAIM_KEY_PARTS)
                and str(key).lower() != "code"
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(claims), sort_keys=True)


# ---------------------------------------------------------------------------
# External identities (``user_identities``)
# ---------------------------------------------------------------------------


def _identity_row(row: Any) -> dict[str, Any]:
    """Shape a ``user_identities`` row for callers."""
    return {
        "id": row["id"],
        "username": row["username"],
        "provider": row["provider"],
        "subject": row["subject"],
        "email": row["email"],
        "raw_claims_json": row["raw_claims_json"],
        "created_at": row["created_at"],
    }


_IDENTITY_SELECT = (
    "SELECT i.id, u.username, i.provider, i.subject, i.email, i.raw_claims_json,"
    " i.created_at FROM user_identities i JOIN users u ON u.id = i.user_id"
)


async def _select_identities(
    db: aiosqlite.Connection, where: str, params: tuple[Any, ...], *, order: str = ""
) -> list[dict[str, Any]]:
    """Run the one identity projection, so its columns are defined once."""
    cursor = await db.execute(
        f"{_IDENTITY_SELECT} WHERE {where} {order}".rstrip(), params
    )
    return [_identity_row(row) for row in await cursor.fetchall()]


async def get_user_identity(provider: str, subject: str) -> dict[str, Any] | None:
    """Return the identity linking ``(provider, subject)`` to a local account."""
    _validate_sso_provider(provider)
    async with _storage._connect_db() as db:
        rows = await _select_identities(
            db, "i.provider = ? AND i.subject = ?", (provider, subject)
        )
        return rows[0] if rows else None


async def list_user_identities(username: str) -> list[dict[str, Any]]:
    """List the identities linked to one account, ordered by provider."""
    username = _storage._normalize_username(username)
    async with _storage._connect_db() as db:
        return await _select_identities(
            db, "u.username = ?", (username,), order="ORDER BY i.provider"
        )


async def list_identities_for_users(
    usernames: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Batched identity lookup for multiple accounts (no N+1)."""
    if not usernames:
        return {}
    normalized = list(dict.fromkeys(_storage._normalize_username(u) for u in usernames))
    result: dict[str, list[dict[str, Any]]] = {}
    async with _storage._connect_db() as db:
        for start in range(0, len(normalized), 500):
            chunk = normalized[start : start + 500]
            placeholders = ", ".join("?" * len(chunk))
            rows = await _select_identities(
                db,
                f"u.username IN ({placeholders})",
                tuple(chunk),
                order="ORDER BY u.username, i.provider",
            )
            for row in rows:
                result.setdefault(row["username"], []).append(row)
    return result


async def _consume_ticket_in_tx(db: Any, ticket: str | None) -> None:
    """Delete a one-time ticket inside the caller's transaction."""
    if ticket is None:
        return
    cursor = await db.execute(
        f"DELETE FROM sso_link_tickets WHERE ticket_hash = ? AND {_LIVE_ROW}",
        (_storage._hash_session_token(ticket),),
    )
    if cursor.rowcount == 0:
        raise TicketExpiredError("ticket is expired or was already used")


async def add_user_identity(
    username: str,
    provider: str,
    subject: str,
    *,
    eligibility_check: EligibilityCheck,
    email: str | None = None,
    raw_claims_json: str | None = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Link an external identity to a local account.

    ``created_via`` on the account is left untouched: linking is not creation.
    Re-linking the identity this account already holds is a no-op that returns
    the stored row, so a retried link does not fail on what it already did.

    Raises:
        BootstrapAdminLinkError: When ``username`` is the reserved ``admin``.
        AccountNotFoundError: When the account does not exist.
        AccountIneligibleError: When ``eligibility_check`` refuses the row read
            inside the transaction.
        TicketExpiredError: When ``ticket`` was expired or already spent.
        IdentityConflictError: When the subject is already linked to another
            account or this account already holds a different subject for this
            provider.
        ValueError: When ``provider``, ``subject``, or ``raw_claims_json`` is
            invalid.
    """
    _validate_sso_provider(provider)
    _validate_identity_subject(subject)
    username = _storage._normalize_username(username)
    if username == "admin":
        raise BootstrapAdminLinkError(
            "Cannot link an identity to the reserved 'admin' account"
        )
    scrubbed_claims = _scrub_claims(raw_claims_json)

    async def _attempt() -> dict[str, Any]:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"SELECT {_storage._USER_LOOKUP_COLUMNS}, status FROM users"
                    " WHERE username = ?",
                    (username,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise AccountNotFoundError(f"User '{username}' not found")
                user = _storage._user_row_to_dict(row)
                refusal = eligibility_check(
                    user, str(user.get("role") or "") == "admin"
                )
                if refusal:
                    raise AccountIneligibleError(refusal)
                linked = await _select_identities(
                    db, "i.provider = ? AND i.subject = ?", (provider, subject)
                )
                if linked and linked[0]["username"] == username:
                    await _consume_ticket_in_tx(db, ticket)
                    await db.commit()
                    return linked[0]
                if linked:
                    logger.info(
                        "Identity %s:%s already linked to '%s'",
                        provider,
                        subject,
                        linked[0]["username"],
                    )
                    raise IdentityConflictError(
                        f"Identity {provider}:{subject} is already linked"
                    )
                cursor = await db.execute(
                    "SELECT provider FROM user_identities"
                    " WHERE user_id = ? AND provider = ?",
                    (user["id"], provider),
                )
                if await cursor.fetchone() is not None:
                    raise IdentityConflictError(
                        f"'{username}' already has a {provider} identity"
                        " — unlink it first"
                    )
                await _consume_ticket_in_tx(db, ticket)
                await db.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, subject, email, raw_claims_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user["id"],
                        provider,
                        subject,
                        email,
                        scrubbed_claims,
                        _storage._now_str(),
                    ),
                )
                stored = await _select_identities(
                    db, "i.user_id = ? AND i.provider = ?", (user["id"], provider)
                )
                await db.commit()
            except sqlite3.IntegrityError as exc:
                await db.rollback()
                raise IdentityConflictError(
                    f"Identity {provider}:{subject} is already linked"
                ) from exc
            except Exception:
                await db.rollback()
                raise
            return stored[0]

    return await _storage._retry_contended(_attempt)


async def create_user_with_identity(
    username: str,
    *,
    provider: str,
    subject: str,
    email: str | None,
    status: str,
    role: str,
    created_via: str,
    ticket: str | None = None,
) -> str:
    """Create an account **and** its SSO link in one transaction, or neither.

    Returns:
        The raw API key, shown to the person once.

    Raises:
        UsernameTakenError: When the username is already taken.
        TicketExpiredError: When the ticket is expired or already spent.
        IdentityConflictError: When ``(provider, subject)`` is already linked.
        ValueError: When any input fails validation.
    """
    username = _storage._normalize_username(username)
    _storage._validate_username(username)
    _validate_sso_provider(provider)
    _validate_identity_subject(subject)
    _storage._validate_user_status(status)
    _storage._validate_created_via(created_via)
    raw_key = _storage.generate_api_key()
    key_hash = _storage.hash_api_key(raw_key)

    async def _attempt() -> None:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT 1 FROM users WHERE username = ?", (username,)
                )
                if await cursor.fetchone() is not None:
                    raise UsernameTakenError(f"Username '{username}' is already taken")
                await _consume_ticket_in_tx(db, ticket)
                cursor = await db.execute(
                    "SELECT 1 FROM user_identities WHERE provider = ? AND subject = ?",
                    (provider, subject),
                )
                if await cursor.fetchone() is not None:
                    raise IdentityConflictError(
                        f"Identity {provider}:{subject} is already linked"
                    )
                await db.execute(
                    "INSERT INTO users"
                    " (username, api_key_hash, role, status, created_via)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (username, key_hash, role, status, created_via),
                )
                cursor = await db.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                )
                user = await cursor.fetchone()
                await db.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, subject, email, raw_claims_json, created_at)"
                    " VALUES (?, ?, ?, ?, NULL, ?)",
                    (user["id"], provider, subject, email, _storage._now_str()),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    await _storage._retry_contended(_attempt)
    return raw_key


async def delete_orphan_identity(provider: str, subject: str) -> bool:
    """Delete an identity whose account no longer exists."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM user_identities WHERE provider = ? AND subject = ?"
            " AND user_id NOT IN (SELECT id FROM users)",
            (provider, subject),
        )
        await db.commit()
        return cursor.rowcount > 0


async def unlink_user_identity(username: str, provider: str) -> str:
    """Unlink a provider from an account, never leaving a keyless account without one.

    Returns:
        ``"deleted"``, ``"not_found"``, or ``"last"`` (keyless account's sole method).
    """
    _validate_sso_provider(provider)
    username = _storage._normalize_username(username)

    async def _attempt() -> str:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT id, (api_key_hash IS NOT NULL) AS has_api_key"
                    " FROM users WHERE username = ?",
                    (username,),
                )
                user_row = await cursor.fetchone()
                if user_row is None:
                    await db.rollback()
                    return "not_found"
                has_api_key = bool(user_row["has_api_key"])
                cursor = await db.execute(
                    "SELECT provider FROM user_identities WHERE user_id = ?",
                    (user_row["id"],),
                )
                providers = [row["provider"] for row in await cursor.fetchall()]
                if provider not in providers:
                    await db.rollback()
                    return "not_found"
                if len(providers) == 1 and not has_api_key:
                    await db.rollback()
                    return "last"
                await db.execute(
                    "DELETE FROM user_identities WHERE provider = ? AND user_id = ?",
                    (provider, user_row["id"]),
                )
                # Delegated OIDC tokens come from the identity just unlinked:
                # revoke them on every session of that account in the same
                # transaction. The sessions themselves survive — a user signed
                # in with a local password must not be logged out by an unlink.
                await db.execute(
                    "UPDATE sessions SET sso_tokens_enc = NULL"
                    " WHERE username = ? AND auth_provider = ?",
                    (username, provider),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return "deleted"

    return await _storage._retry_contended(_attempt)


# ---------------------------------------------------------------------------
# CLI device-authorization codes (``sso_device_codes``)
# ---------------------------------------------------------------------------


def _normalize_user_code(user_code: str) -> str:
    """Normalise a human-typed user code for lookup (``XXXX-XXXX``)."""
    compact = "".join(ch for ch in user_code.upper() if ch.isalnum())
    if len(compact) == 8:
        return f"{compact[:4]}-{compact[4:]}"
    return compact


def _new_user_code() -> str:
    """Generate a short human-readable ``XXXX-XXXX`` code (no 0/O or 1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # pragma: allowlist secret
    chars = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


async def create_device_code(
    *,
    ttl_seconds: int,
    interval_seconds: int,
    provider: str | None = None,
    client_name: str | None = None,
    max_live: int | None = None,
    max_live_per_caller: int | None = None,
    client_ip: str | None = None,
) -> dict[str, Any]:
    """Start a CLI device-authorization flow."""
    if provider is not None:
        _validate_sso_provider(provider)
    device_code = secrets.token_urlsafe(32)
    device_hash = _storage._hash_session_token(device_code)
    expires_at = _storage.datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    expires_str = expires_at.strftime(_storage.SQL_TIMESTAMP_FORMAT)
    created_at = _storage._now_str()

    async def _insert(user_code: str) -> None:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if max_live is not None:
                    cursor = await db.execute(
                        f"SELECT COUNT(*) AS n FROM sso_device_codes WHERE {_LIVE_ROW}"
                    )
                    if int((await cursor.fetchone())["n"]) >= max_live:
                        raise DeviceCodeCapacityError(
                            "Too many pending device codes deployment-wide"
                        )
                if max_live_per_caller is not None and client_ip:
                    cursor = await db.execute(
                        "SELECT COUNT(*) AS n FROM sso_device_codes"
                        f" WHERE client_ip = ? AND {_LIVE_ROW}",
                        (client_ip,),
                    )
                    if int((await cursor.fetchone())["n"]) >= max_live_per_caller:
                        raise DeviceCodeCapacityError(
                            "Too many pending device codes from this caller"
                        )
                await db.execute(
                    "INSERT INTO sso_device_codes"
                    " (device_code_hash, user_code, provider, user_id, client_name,"
                    " expires_at, interval_seconds, client_ip, created_at)"
                    " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        device_hash,
                        user_code,
                        provider,
                        client_name,
                        expires_str,
                        interval_seconds,
                        client_ip,
                        created_at,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    for _attempt in range(_USER_CODE_ATTEMPTS):
        user_code = _new_user_code()

        async def _insert_attempt(code: str = user_code) -> None:
            await _insert(code)

        try:
            await _storage._retry_contended(_insert_attempt)
        except DeviceCodeCapacityError:
            raise
        except sqlite3.IntegrityError as exc:
            if "user_code" not in str(exc):
                raise
            continue
        break
    else:
        msg = "Could not allocate a unique device user code"
        raise ValueError(msg)
    return {
        "device_code": device_code,
        "user_code": user_code,
        "client_name": client_name,
        "expires_at": expires_str,
        "interval_seconds": interval_seconds,
    }


def _device_code_row(row: Any) -> dict[str, Any]:
    """Shape a ``sso_device_codes`` row for both the poll and approval views."""
    return {
        "user_code": row["user_code"],
        "provider": row["provider"],
        "approved_provider": row["approved_provider"],
        "client_name": row["client_name"],
        "authorized": row["approved_username"] is not None,
        "expires_at": row["expires_at"],
        "interval_seconds": row["interval_seconds"],
        "last_polled_at": row["last_polled_at"],
    }


_DEVICE_CODE_SELECT = (
    "SELECT user_code, provider, approved_provider, approved_username,"
    " client_name, expires_at, interval_seconds, last_polled_at"
    " FROM sso_device_codes"
)


async def _select_device_code(
    where: str, params: tuple[Any, ...]
) -> dict[str, Any] | None:
    """Run the one device-code projection."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(f"{_DEVICE_CODE_SELECT} WHERE {where}", params)
        row = await cursor.fetchone()
    return _device_code_row(row) if row else None


async def get_device_code(device_code: str) -> dict[str, Any] | None:
    """Look up a device code by its raw value."""
    return await _select_device_code(
        f"device_code_hash = ? AND {_LIVE_ROW}",
        (_storage._hash_session_token(device_code),),
    )


async def get_device_code_by_user_code(user_code: str) -> dict[str, Any] | None:
    """Look up a pending device code by the code a human typed."""
    return await _select_device_code(
        f"user_code = ? AND {_LIVE_ROW}", (_normalize_user_code(user_code),)
    )


async def authorize_device_code(
    user_code: str, *, username: str, provider: str | None = None
) -> str:
    """Approve a pending device code for an account."""
    if provider is not None:
        _validate_sso_provider(provider)
    normalized = _normalize_user_code(user_code)
    approver = _storage._normalize_username(username)

    async def _attempt() -> str:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT id FROM users WHERE username = ?", (approver,)
                )
                user = await cursor.fetchone()
                updated = await db.execute(
                    "UPDATE sso_device_codes SET user_id = ?, approved_username = ?,"
                    " approved_provider = ?"
                    " WHERE user_code = ? AND approved_username IS NULL"
                    f" AND {_LIVE_ROW}"
                    " AND (provider IS NULL OR ? IS NULL OR provider = ?)",
                    (
                        user["id"] if user else None,
                        approver,
                        provider,
                        normalized,
                        provider,
                        provider,
                    ),
                )
                if updated.rowcount:
                    await db.commit()
                    return "approved"
                cursor = await db.execute(
                    "SELECT approved_username, provider,"
                    f" ({_LIVE_ROW}) AS live FROM sso_device_codes"
                    " WHERE user_code = ?",
                    (normalized,),
                )
                row = await cursor.fetchone()
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            if row is None:
                return "code_not_found"
            if row["approved_username"] is not None:
                return "already_approved"
            if not row["live"]:
                return "expired"
            return "provider_mismatch"

    return await _storage._retry_contended(_attempt)


async def record_device_code_poll(device_code: str) -> bool:
    """Record a poll and say whether it arrived inside the allowed interval."""
    device_hash = _storage._hash_session_token(device_code)

    async def _attempt() -> bool:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            now_str = _storage.datetime.now(UTC).strftime(_storage.SQL_TIMESTAMP_FORMAT)
            try:
                cursor = await db.execute(
                    "SELECT interval_seconds, last_polled_at FROM sso_device_codes"
                    f" WHERE device_code_hash = ? AND {_LIVE_ROW}",
                    (device_hash,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return False
                last = row["last_polled_at"]
                stamped = _storage.datetime.strptime(
                    now_str, _storage.SQL_TIMESTAMP_FORMAT
                )
                too_soon = last is not None and (
                    stamped
                    - _storage.datetime.strptime(last, _storage.SQL_TIMESTAMP_FORMAT)
                ) < timedelta(seconds=row["interval_seconds"])
                await db.execute(
                    "UPDATE sso_device_codes SET last_polled_at = ?"
                    " WHERE device_code_hash = ?",
                    (now_str, device_hash),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return not too_soon

    return await _storage._retry_contended(_attempt)


async def count_live_device_codes() -> int:
    """How many device codes are currently approvable."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) AS n FROM sso_device_codes WHERE {_LIVE_ROW}"
        )
        row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def redeem_device_code(device_code: str) -> dict[str, Any] | None:
    """Claim an authorized device code and name the account it approved."""
    device_hash = _storage._hash_session_token(device_code)

    async def _attempt() -> dict[str, Any] | None:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT d.approved_username, d.approved_provider"
                    " FROM sso_device_codes d"
                    " WHERE d.device_code_hash = ?"
                    " AND d.approved_username IS NOT NULL"
                    " AND d.redeemed_at IS NULL"
                    f" AND d.{_LIVE_ROW}",
                    (device_hash,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                claimed = await db.execute(
                    "UPDATE sso_device_codes SET redeemed_at = ?"
                    " WHERE device_code_hash = ? AND redeemed_at IS NULL",
                    (_storage._now_str(), device_hash),
                )
                if claimed.rowcount == 0:
                    await db.rollback()
                    return None
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return {
                "username": row["approved_username"],
                "approved_provider": row["approved_provider"],
            }

    return await _storage._retry_contended(_attempt)


async def release_device_code(device_code: str) -> bool:
    """Undo a redemption claim after the session could not be minted."""
    device_hash = _storage._hash_session_token(device_code)

    async def _attempt() -> bool:
        async with _storage._connect_db() as db:
            cursor = await db.execute(
                "UPDATE sso_device_codes SET redeemed_at = NULL"
                " WHERE device_code_hash = ?",
                (device_hash,),
            )
            await db.commit()
            return cursor.rowcount > 0

    return await _storage._retry_contended(_attempt)


async def exchange_device_code_for_session(
    device_code: str,
    *,
    username: str,
    eligibility_check: EligibilityCheck,
    approved_provider: str | None = None,
    ttl_hours: int | None = None,
    domain_check: Callable[[str | None], str | None] | None = None,
) -> DeviceCodeExchangeResult | None:
    """Spend a claimed device code and mint its session in one transaction.

    Rights, account eligibility, and domain policy are re-read from SQLite
    inside the transaction so a block, demotion, or domain revocation committed
    between poll and exchange always wins. Policy predicates are supplied by the
    caller to keep the dependency direction intact (storage never imports auth).

    Returns:
        :class:`DeviceCodeExchangeResult` (either a minted session or a
        ``refused`` reason), or ``None`` when no live claim exists to spend.
    """
    if ttl_hours is None:
        ttl_hours = _storage.current_session_ttl_hours()
    device_hash = _storage._hash_session_token(device_code)
    normalized = _storage._normalize_username(username)

    async def _attempt() -> DeviceCodeExchangeResult | None:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT 1 FROM sso_device_codes d"
                    " WHERE d.device_code_hash = ? AND d.redeemed_at IS NOT NULL"
                    " AND d.approved_username = ?"
                    f" AND d.{_LIVE_ROW}",
                    (device_hash, normalized),
                )
                if await cursor.fetchone() is None:
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    f"SELECT {_storage._USER_LOOKUP_COLUMNS}, status FROM users"
                    " WHERE username = ?",
                    (normalized,),
                )
                row = await cursor.fetchone()
                user = _storage._user_row_to_dict(row) if row is not None else None
                if user is None:
                    if normalized != "admin":
                        await db.rollback()
                        return None
                    role, is_admin = "admin", True
                else:
                    role = str(row["role"] or "reviewer")
                    is_admin = role == "admin"
                    refusal = eligibility_check(user, is_admin)
                    if refusal:
                        await db.rollback()
                        return DeviceCodeExchangeResult(refused=f"account_{refusal}")
                if approved_provider is not None and domain_check is not None:
                    cursor = await db.execute(
                        "SELECT email FROM user_identities"
                        " WHERE user_id = ("
                        "   SELECT id FROM users WHERE username = ?"
                        " ) AND provider = ?",
                        (normalized, approved_provider),
                    )
                    id_row = await cursor.fetchone()
                    email = id_row["email"] if id_row is not None else None
                    domain_refusal = domain_check(email)
                    if domain_refusal is not None:
                        await db.rollback()
                        return DeviceCodeExchangeResult(refused=domain_refusal)
                token = await _storage._insert_session_in_tx(
                    db,
                    username=normalized,
                    is_admin=is_admin,
                    role=role,
                    auth_provider=approved_provider,
                    ttl_hours=ttl_hours,
                )
                await db.execute(
                    "DELETE FROM sso_device_codes WHERE device_code_hash = ?",
                    (device_hash,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return DeviceCodeExchangeResult(token=token, role=role, is_admin=is_admin)

    return await _storage._retry_contended(_attempt)


# ---------------------------------------------------------------------------
# First-login choice tickets (``sso_link_tickets``)
# ---------------------------------------------------------------------------


async def create_sso_ticket(
    *,
    provider: str,
    subject: str,
    email: str | None,
    suggested_username: str,
    binding: str,
    next_path: str = "/",
    ttl_seconds: int = SSO_TICKET_TTL_SECONDS,
    sso_tokens_enc: str | None = None,
) -> str:
    """Mint a one-time ticket for the first-login choice screen.

    ``sso_tokens_enc`` is an already-encrypted browser-session token blob; it is
    carried separately from the identity fields so no token ever enters
    ``raw_claims_json`` or an audit row.
    """
    _validate_sso_provider(provider)
    _validate_identity_subject(subject)
    ticket = secrets.token_urlsafe(32)
    expires_at = _storage.datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    created_at = _storage._now_str()
    expires_str = expires_at.strftime(_storage.SQL_TIMESTAMP_FORMAT)

    async def _insert() -> None:
        async with _storage._connect_db() as db:
            await db.execute(
                "INSERT INTO sso_link_tickets"
                " (ticket_hash, provider, subject, email, suggested_username,"
                " next_path, expires_at, created_at, binding_hash,"
                " sso_tokens_enc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _storage._hash_session_token(ticket),
                    provider,
                    subject,
                    email,
                    suggested_username,
                    next_path,
                    expires_str,
                    created_at,
                    _storage._hash_session_token(binding),
                    sso_tokens_enc,
                ),
            )
            await db.commit()

    await _storage._retry_contended(_insert)
    return ticket


async def sso_ticket_binding_hash(ticket: str) -> str | None:
    """The digest of the browser binding a live ticket was minted with."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(
            "SELECT binding_hash FROM sso_link_tickets"
            f" WHERE ticket_hash = ? AND {_LIVE_ROW}",
            (_storage._hash_session_token(ticket),),
        )
        row = await cursor.fetchone()
    return row["binding_hash"] if row else None


def _ticket_view(row: Any) -> dict[str, Any]:
    """Shape an ``sso_link_tickets`` row for both readers."""
    return {
        "provider": row["provider"],
        "subject": row["subject"],
        "email": row["email"],
        "suggested_username": row["suggested_username"],
        "next_path": row["next_path"],
        "expires_at": row["expires_at"],
        "sso_tokens_enc": row["sso_tokens_enc"],
    }


_TICKET_SELECT = (
    "SELECT provider, subject, email, suggested_username, next_path, expires_at,"
    " sso_tokens_enc"
    " FROM sso_link_tickets"
)


async def peek_sso_ticket(ticket: str) -> dict[str, Any] | None:
    """Read a live ticket without consuming it."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(
            f"{_TICKET_SELECT} WHERE ticket_hash = ? AND {_LIVE_ROW}",
            (_storage._hash_session_token(ticket),),
        )
        row = await cursor.fetchone()
    return _ticket_view(row) if row else None


async def consume_sso_ticket(ticket: str) -> dict[str, Any] | None:
    """Consume a live ticket, exactly once."""
    ticket_hash = _storage._hash_session_token(ticket)

    async def _attempt() -> dict[str, Any] | None:
        async with _storage._connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"{_TICKET_SELECT} WHERE ticket_hash = ? AND {_LIVE_ROW}",
                    (ticket_hash,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                await db.execute(
                    "DELETE FROM sso_link_tickets WHERE ticket_hash = ?",
                    (ticket_hash,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return _ticket_view(row)

    return await _storage._retry_contended(_attempt)


async def _purge_expired(table: str) -> int:
    """Delete the expired rows of one table."""
    async with _storage._connect_db() as db:
        cursor = await db.execute(f"DELETE FROM {table} WHERE NOT ({_LIVE_ROW})")
        await db.commit()
        return cursor.rowcount


async def purge_expired_sso_tickets() -> int:
    """Delete expired first-login tickets."""
    return await _purge_expired("sso_link_tickets")


async def purge_expired_device_codes() -> int:
    """Delete device codes that can no longer be used."""
    return await _purge_expired("sso_device_codes")
