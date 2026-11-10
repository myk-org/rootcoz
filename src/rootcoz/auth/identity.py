"""Resolve SSO claims to a local account.

The rules, and why each one exists:

* The link is ``(provider, subject)``. Usernames and emails change at the IdP;
  the subject does not, so an existing link always wins.
* A name match is **never** used to find an account. A local ``ruthn`` and an IdP
  ``rnetser`` can be the same person, and two different people can both be
  ``ruth`` — so the username is only ever a *suggestion for a new account*. When
  there is no link, the callback asks the person to choose: claim an existing
  account with its own credential, or create one (``/api/auth/sso/{link,create}``).
* With ``ALLOWED_USERS`` set, a **new** account is refused unless its name is
  listed — the same bound registration applies. The check runs only at creation:
  a local account that is not on the list can still claim itself, because that
  list bounds who may write, not who may sign in (see ``AGENTS.md``).
* Email is never used to merge accounts: two providers can both claim the same
  address, and auto-merging on it is an account-takeover primitive. The address
  *domain* is used for exactly one thing: the deployment's optional
  ``SSO_ALLOWED_DOMAINS`` allow-list, checked before any account is looked up so
  it applies to linked accounts too.

This module decides *which account*, never *whether it may act* — that is
:mod:`rootcoz.auth.approval` — and it writes no SQL; persistence belongs to
:mod:`rootcoz.storage`.
"""

from __future__ import annotations

from dataclasses import dataclass

from rootcoz import get_child_logger, storage
from rootcoz.auth import approval as auth_approval
from rootcoz.auth.base import RESERVED_ADMIN_USERNAME
from rootcoz.auth.sso.base import AuthError, ClaimSet
from rootcoz.config import Settings

logger = get_child_logger(__name__)

#: Same shape the register path enforces (one definition lives in storage).

#: Prefix reserved for system identities (``rootcoz-ai`` and friends). Storage
#: enforces the same constant at the creation choke point.
_RESERVED_PREFIX = storage.RESERVED_USERNAME_PREFIX


def is_reserved_username(candidate: str) -> bool:
    """Whether a name belongs to a reserved identity rather than a person.

    Args:
        candidate: A lower-cased candidate name.

    Returns:
        True for the bootstrap ``admin``, the ``rootcoz*`` system prefix and
        storage's reserved system tags (``re-analyze``).
    """
    return (
        candidate == RESERVED_ADMIN_USERNAME
        or candidate.startswith(_RESERVED_PREFIX)
        or candidate in storage.RESERVED_USERNAMES
    )


@dataclass(frozen=True)
class IdentityResolution:
    """The local account an SSO login resolved to.

    Attributes:
        username: Local username.
        created: True when this login created the account.
        status: The account's approval status after resolution.
        api_key: The raw API key of an account created here, shown to the person
            once. Empty for a login that resolved an existing account — no key is
            ever re-readable.
    """

    username: str
    created: bool
    status: str
    api_key: str = ""


@dataclass(frozen=True)
class LoginChoice:
    """What the first-login choice screen needs: no account is linked yet.

    Attributes:
        provider: Provider that authenticated the person.
        subject: The provider's stable subject identifier.
        email: Email the provider reported, if any.
        suggested_username: Name proposed for a new account (editable).
    """

    provider: str
    subject: str
    email: str | None
    suggested_username: str


def domain_allowed(domain: str, allowed: frozenset[str]) -> bool:
    """Whether a domain is on the allow-list.

    A subdomain of an entry is allowed; a look-alike suffix is not — ``redhat.com``
    must never match ``notredhat.com`` or ``redhat.com.evil.io``.

    Args:
        domain: The email domain, lower-cased.
        allowed: The normalised allow-list.

    Returns:
        True when the domain equals an entry or ends with ``"." + entry``.
    """
    return any(domain == entry or domain.endswith("." + entry) for entry in allowed)


def check_domain(claims: ClaimSet, settings: Settings) -> None:
    """Apply the deployment's SSO domain policy to one login.

    Runs before the account is looked up, so it applies to a linked account too:
    removing a domain revokes access on the next sign-in, not on the next
    account. The policy is admission only — approval and roles are decided
    elsewhere.

    While the list is set the address must be *positively* verified: a provider
    that omits ``email_verified`` (or sends ``false``) is refused, because an
    unverified address can be changed by whoever holds the account.

    Args:
        claims: The verified claims.
        settings: Application settings (``SSO_ALLOWED_DOMAINS``).

    Raises:
        AuthError: ``domain_not_allowed`` when the address is missing or its
            domain is not listed, ``email_not_verified`` when the provider does
            not assert that the address is verified.
    """
    try:
        allowed = settings.sso_allowed_domains_set
    except ValueError as exc:
        # A malformed list is a deployment bug, not a caller error: refuse the
        # login cleanly instead of raising a 500 out of the callback.
        logger.error("SSO domain allow-list is malformed: %s", exc)
        raise AuthError(
            "callback_misconfigured", "SSO domain list is malformed"
        ) from exc
    if not allowed:
        return
    email = (claims.email or "").strip().lower()
    if "@" not in email:
        # Fail closed: without an address there is nothing to check against.
        raise AuthError("domain_not_allowed", "provider returned no email address")
    if claims.asserts_email_verified:
        assertion = claims.claims.get("email_verified")
        # Only a positive assertion passes: a provider that publishes the claim
        # and stays silent about verification cannot satisfy an allow-list that
        # checks the address. A provider that publishes no such claim at all
        # (OpenShift) has nothing to assert, and the domain check is all there is.
        if not (assertion is True or str(assertion).lower() == "true"):
            raise AuthError(
                "email_not_verified",
                "provider did not assert that the email is verified",
            )
    domain = email.rsplit("@", 1)[-1]
    if not domain_allowed(domain, allowed):
        logger.info("SSO login refused: domain '%s' is not allowed", domain)
        raise AuthError("domain_not_allowed", f"domain '{domain}' is not allowed")


def check_email_domain(email: str | None, settings: Settings) -> None:
    """Re-apply the domain policy to an address read back from a ticket.

    A ticket lives up to ten minutes, so a domain revoked between the IdP
    callback and the person pressing "Create" or "Connect" must still be
    refused. Verification is not re-checked: it is an IdP fact recorded at
    callback time, and it cannot change inside the ticket's lifetime.

    Args:
        email: The address stored on the ticket.
        settings: Application settings (``SSO_ALLOWED_DOMAINS``).

    Raises:
        AuthError: ``domain_not_allowed`` when the list is set and the address
            is missing or its domain is not listed.
    """
    allowed = settings.sso_allowed_domains_set
    if not allowed:
        return
    address = (email or "").strip().lower()
    if "@" not in address:
        raise AuthError("domain_not_allowed", "ticket carried no email address")
    domain = address.rsplit("@", 1)[-1]
    if not domain_allowed(domain, allowed):
        raise AuthError("domain_not_allowed", f"domain '{domain}' is not allowed")


def suggested_username(claims: ClaimSet) -> str:
    """Best name to propose for a new account.

    Args:
        claims: The provider's claims.

    Returns:
        A lower-cased name that ``storage`` would accept, or ``""`` when neither
        the hint nor the email yields one — the person then types their own.
    """
    for raw in (claims.username_hint, (claims.email or "").split("@", 1)[0]):
        candidate = raw.strip().lower()
        if not candidate:
            continue
        if is_reserved_username(candidate):
            # Never *propose* a reserved or system name, even though the person
            # could type a valid one themselves.
            continue
        if storage.is_creatable_username(candidate):
            return candidate
    return ""


async def resolve(
    provider: str, claims: ClaimSet, settings: Settings
) -> IdentityResolution | LoginChoice:
    """Resolve claims to a linked account, or say that a choice is needed.

    Args:
        provider: Provider name the claims came from.
        claims: Verified claims.
        settings: Application settings for this request.

    Returns:
        The linked account, or a :class:`LoginChoice` when this identity is not
        linked yet — the caller mints a one-time ticket and lets the person
        decide (claim an existing account, or create one).

    Raises:
        AuthError: When the linked account must not sign in
            (``account_disabled``, ``account_locked``).
    """
    check_domain(claims, settings)
    linked = await storage.get_user_identity(provider, claims.subject)
    if linked is not None:
        user = await storage.get_user_by_username(linked["username"])
        if user is not None:
            block_reason = auth_approval.account_block_reason(user)
            if block_reason is not None:
                raise AuthError(
                    f"account_{block_reason}",
                    f"account is {block_reason}",
                    subject=str(user["username"]),
                )
            return IdentityResolution(
                username=str(user["username"]),
                created=False,
                status=await status_of(str(user["username"])),
            )
        # The link outlived its account. ``delete_user`` removes a user's
        # identities, so this is a row from before that cleanup or a concurrent
        # deletion — either way it cannot be trusted, and it cannot be deleted by
        # username (the account is what the delete looks up), so it is ignored.
        logger.debug(
            "Identity %s:%s points at a missing account; dropping the orphan",
            provider,
            claims.subject,
        )
        # Heal it: the row holds the (provider, subject) unique slot, so leaving
        # it would stop this person from ever linking again.
        await storage.delete_orphan_identity(provider, claims.subject)

    return LoginChoice(
        provider=provider,
        subject=claims.subject,
        email=claims.email,
        suggested_username=suggested_username(claims),
    )


async def status_of(username: str) -> str:
    """Current approval status of an account.

    Args:
        username: The account.

    Returns:
        ``active``, ``pending`` or ``rejected``; ``active`` when the row is gone.
    """
    return await storage.get_user_status(username) or "active"


async def link_existing(
    *,
    username: str,
    provider: str,
    subject: str,
    email: str | None,
) -> IdentityResolution:
    """Attach a verified IdP identity to an account whose owner just proved it.

    The caller has already verified the account's credential — this only writes
    the link, so role, status, API key and history are untouched and
    ``created_via`` is not rewritten.

    Args:
        username: Account the owner authenticated as.
        provider: Provider name.
        subject: The provider's stable subject identifier.
        email: Email the provider reported.

    Returns:
        The resolution for that account.

    Raises:
        AuthError: ``identity_conflict`` when the subject is already linked
            elsewhere or the account already holds an identity for this provider;
            ``account_missing`` when the account no longer exists.
    """
    try:
        await storage.add_user_identity(
            username,
            provider,
            subject,
            email=email,
        )
    except ValueError as exc:
        # The account disappeared between the credential check and this write: it
        # is not a conflict, and the caller must not be told "already linked".
        reason = "account_missing" if "not found" in str(exc) else "identity_conflict"
        raise AuthError(reason, str(exc), subject=username) from exc
    return IdentityResolution(
        username=username, created=False, status=await status_of(username)
    )


def validate_new_username(username: str, settings: Settings) -> None:
    """Check a name chosen on the choice screen before anything is written.

    Split out so an endpoint can reject a name *without* spending the ticket, and
    so the rule list (register shape, reserved names, allow-list) is written once.

    Args:
        username: The name the person typed.
        settings: Application settings (``ALLOWED_USERS``).

    Raises:
        AuthError: ``invalid_username`` when storage would reject it,
            ``not_allowed`` when ``ALLOWED_USERS`` excludes it.
    """
    candidate = username.strip().lower()
    # The register path's rule plus the reserved identities: storage refuses the
    # same names at insert time, but the endpoint must answer 400 before writing.
    if is_reserved_username(candidate) or not storage.is_creatable_username(candidate):
        raise AuthError("invalid_username", "that name cannot be used")
    allowed = settings.allowed_users_set
    if allowed and candidate not in allowed:
        raise AuthError("not_allowed", "username is not on the allow-list")


async def create_new(
    *, provider: str, subject: str, email: str | None, username: str, settings: Settings
) -> IdentityResolution:
    """Create a new account with the name the person chose, and link it.

    Both rows are written in one transaction (``storage.create_user_with_identity``),
    so a failure cannot leave an account without its identity — and the register
    path's "existing account without a key gets one" behaviour is deliberately not
    reused: a taken name is always refused here.

    Args:
        provider: Provider name.
        subject: The provider's stable subject identifier.
        email: Email the provider reported.
        username: Name chosen on the choice screen (register rules apply).
        settings: Application settings (``REQUIRE_APPROVAL``, ``ALLOWED_USERS``).

    Returns:
        The resolution for the new account.

    Raises:
        AuthError: ``invalid_username`` when the name is taken or rejected,
            ``not_allowed`` when ``ALLOWED_USERS`` excludes it, or
            ``identity_conflict`` when the subject was linked in the meantime.
    """
    validate_new_username(username, settings)
    candidate = username.strip().lower()
    status = "pending" if settings.require_approval else "active"
    try:
        api_key = await storage.create_user_with_identity(
            candidate,
            provider=provider,
            subject=subject,
            email=email,
            status=status,
            role=settings.default_user_role,
            created_via=provider,
        )
    except ValueError as exc:
        reason = (
            "identity_conflict" if "already linked" in str(exc) else "invalid_username"
        )
        raise AuthError(reason, str(exc), subject=candidate) from exc
    return IdentityResolution(
        username=candidate, created=True, status=status, api_key=api_key
    )
