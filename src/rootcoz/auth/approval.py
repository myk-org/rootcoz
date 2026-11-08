"""The decisions behind the status gate every authentication path shares.

A strategy resolves *who* is calling; whether that identity may act is decided
outside the strategies, using the pieces here. They live in the package because
``main`` imports ``auth`` and not the reverse, and keeping them here — never
inside a strategy — is what stops the next authentication mechanism from
silently skipping them.

What each piece owns:

* :func:`blocked_user_status_response` — the approval states only
  (``pending`` / ``rejected``), answered by the middleware and the login route.
* :func:`account_block_reason` — whether a users row may act on a live
  credential: ``disabled`` stops every path, ``locked`` only the login page and
  the proxy redirect. The login lockout *transition* (counting a failure,
  deciding the lock) belongs to :func:`rootcoz.storage.settle_login_attempt`,
  which cannot live here: ``storage`` is a dependency of this package, so
  importing ``auth`` from it would be a cycle.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from rootcoz.config import Settings

#: Body of the generic refusal. A refused account and a bad credential must be
#: byte-identical, so the caller never learns which condition applied.
UNAUTHENTICATED_DETAIL = "Authentication required. Please log in with your API key."

#: Approval states that block a non-administrator, and what the caller is told.
APPROVAL_STATUS_DETAILS: dict[str, str] = {
    "pending": "Your account is awaiting admin approval",
    "rejected": "Your account has been rejected",
}


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
