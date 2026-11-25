"""The decisions behind the status gate every authentication path shares.

A strategy resolves *who* is calling; whether that identity may act is decided
outside the strategies, using the pieces here. They live in the package because
``main`` imports ``auth`` and not the reverse, and keeping them here — never
inside a strategy — is what stops the next authentication mechanism from
silently skipping them.

What each piece owns:

* :func:`blocked_user_status_response` — the approval states only
  (``pending`` / ``rejected``), answered by the middleware and the login route.
* :func:`approval_gate_applies` — whether ``REQUIRE_APPROVAL`` holds a
  non-administrator back at all: the condition half of every approval refusal.
* :func:`initial_user_status` — the status a brand-new account starts in, so
  registration, SSO account creation, proxy provisioning and the projected
  status behind a proxy redirect cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from rootcoz.config import Settings, get_settings

#: Body of the generic refusal. A refused account and a bad credential must be
#: byte-identical, so the caller never learns which condition applied.
UNAUTHENTICATED_DETAIL = "Authentication required. Please log in with your API key."

#: Approval states that block a non-administrator, and what the caller is told.
APPROVAL_STATUS_DETAILS: dict[str, str] = {
    "pending": "Your account is awaiting admin approval",
    "rejected": "Your account has been rejected",
}


def initial_user_status(settings: Settings) -> str:
    """The status a newly created account starts in.

    ``REQUIRE_APPROVAL`` is the deployment's admission gate: on, every new account
    waits for an admin (``pending``); off, it may act at once (``active``). Every
    creation path asks this — registration, an SSO account, a proxy-provisioned
    account, and the *projected* status of a row a proxy redirect is about to
    create — so the rule cannot drift between them.

    Args:
        settings: Application settings (the approval rule).

    Returns:
        ``pending`` while the deployment requires approval, else ``active``.
    """
    return "pending" if settings.require_approval else "active"


def is_approval_blocked(user_status: str | None) -> bool:
    """Whether a users-row status blocks access until an admin acts.

    Args:
        user_status: The ``users.status`` value.

    Returns:
        True for ``pending`` and ``rejected`` — the states a session may exist
        for but every gate refuses.
    """
    return (user_status or "") in APPROVAL_STATUS_DETAILS


def approval_gate_applies(*, is_admin: bool, settings: Settings) -> bool:
    """Whether ``REQUIRE_APPROVAL`` holds a non-administrator back at all.

    The condition half of every approval refusal: the middleware, the proxy
    redirect, ``/api/auth/login``, the device poll and the SSO session issue ask
    this first and :func:`is_approval_blocked` second, so none of them can drift
    into refusing an administrator, or an ordinary account below
    ``REQUIRE_APPROVAL``, that the others let in.

    Args:
        is_admin: Whether the resolved identity is an administrator.
        settings: Application settings carrying ``REQUIRE_APPROVAL``.

    Returns:
        True when this caller's ``status`` decides access.
    """
    return settings.require_approval and not is_admin


def account_block_reason(user: dict[str, Any] | None) -> str | None:
    """``"disabled"`` / ``"locked"`` for a row that must not log in (#379).

    Only the login path asks this. ``disabled`` is an admin taking the account
    out of service, so the middleware refuses it on every path too; ``locked``
    is a credential-guessing guard, and refusing it anywhere but here would let
    a stranger who knows a username cut off that account's sessions and keys.

    Args:
        user: The users row, or ``None``.

    Returns:
        The blocking reason, or ``None`` for a usable account and for the
        bootstrap admin, which has no ``users`` row and is unaffected.
    """
    if not user:
        return None
    if user.get("disabled"):
        return "disabled"
    if user.get("locked"):
        return "locked"
    return None


def account_eligibility_refusal(
    user: dict[str, Any], *, is_admin: bool, settings: Settings
) -> str:
    """Name why *user* may not act at all, or ``""`` when it may.

    One predicate for every write that hands out access — a session minted from a
    device code, an identity link written for a first login — each applied to the
    row read *inside* its own transaction. Approval happens minutes before either
    write lands, so a block an admin commits in that window must win.

    Args:
        user: The users row, read inside the writing transaction.
        is_admin: Whether the account is an administrator, who bypasses approval.
        settings: Application settings carrying ``REQUIRE_APPROVAL``.

    Returns:
        ``"disabled"``, ``"locked"``, ``"pending"`` or ``"rejected"`` — the
        reason a caller prefixes with ``account_`` for the trail — or ``""`` for
        an account that may act.
    """
    reason = account_block_reason(user)
    if reason is not None:
        return reason
    status = str(user.get("status") or "")
    if approval_gate_applies(is_admin=is_admin, settings=settings) and (
        is_approval_blocked(status)
    ):
        return status
    return ""


def default_eligibility_check(user: dict[str, Any], is_admin: bool) -> str:
    """Default ``EligibilityCheck`` callback reading live settings at commit time."""
    return account_eligibility_refusal(user, is_admin=is_admin, settings=get_settings())


def maybe_add_custom_approval_msg(content: dict[str, Any], settings: Settings) -> None:
    """Add the deployment's custom approval note to a response body.

    Args:
        content: Response body to extend in place.
        settings: Application settings carrying ``ADMIN_WAIT_APPROVE_MSG``.
    """
    if settings.admin_wait_approve_msg:
        content["custom_message"] = settings.admin_wait_approve_msg


def blocked_user_status_response(
    user_status: str | None, settings: Settings
) -> JSONResponse | None:
    """Refuse a pending or rejected account.

    Args:
        user_status: The ``users.status`` value of the account.
        settings: Application settings carrying ``ADMIN_WAIT_APPROVE_MSG``.

    Returns:
        A ``403`` body naming the approval state, or ``None`` when the account
        is not blocked by approval.
    """
    detail = APPROVAL_STATUS_DETAILS.get(user_status or "")
    if detail is None:
        return None
    content: dict[str, Any] = {"detail": detail, "status": user_status}
    if user_status == "pending":
        maybe_add_custom_approval_msg(content, settings)
    return JSONResponse(status_code=403, content=content)
