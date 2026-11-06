"""User-friendly error message mapping.

Converts internal/technical errors into messages suitable for end users.
"""

from __future__ import annotations

from rootcoz import get_child_logger

logger = get_child_logger(__name__)


def make_user_friendly_error(error: str | Exception) -> str:
    """Convert internal errors to user-friendly messages.

    Maps known error patterns to human-readable messages.
    Unknown errors get a generic message.
    """
    err_str = str(error)
    err_lower = err_str.lower()

    if (
        "not found in jenkins" in err_lower
        or "job does not exist" in err_lower
        or "404" in err_lower
    ):
        logger.debug("Error mapped to Jenkins-not-found: %s", err_str[:200])
        return (
            "The requested Jenkins job or build is no longer available. "
            "The build data may have been deleted or expired."
        )

    if (
        "authentication" in err_lower
        or "unauthorized" in err_lower
        or "401" in err_lower
    ):
        logger.debug("Error mapped to Jenkins-auth-failed: %s", err_str[:200])
        return "Jenkins authentication failed. Please verify your Jenkins credentials."

    if (
        "connection" in err_lower
        or "timeout" in err_lower
        or "unreachable" in err_lower
    ):
        logger.debug("Error mapped to Jenkins-connection: %s", err_str[:200])
        return (
            "Could not connect to Jenkins. The server may be temporarily unavailable."
        )

    if (
        "sidecar" in err_lower
        or ("ai service" in err_lower and "failed" in err_lower)
        or "ai call failed" in err_lower
    ):
        logger.debug("Error mapped to AI-service-unavailable: %s", err_str[:200])
        return "The AI service is temporarily unavailable. Please try again."

    if "ssl" in err_lower or "certificate" in err_lower:
        logger.debug("Error mapped to SSL-certificate: %s", err_str[:200])
        return "SSL certificate verification failed when connecting to an external service."

    if "clone" in err_lower or "repository" in err_lower:
        logger.debug("Error mapped to repo-clone: %s", err_str[:200])
        return (
            "Failed to clone a source code repository. "
            "The repository may be unavailable or require authentication."
        )

    if "decrypt" in err_lower:
        logger.debug("Error mapped to decrypt-failure: %s", err_str[:200])
        return "Could not decrypt stored credentials. The server encryption key may have changed."

    # Generic fallback — no internal details
    logger.debug("Error mapped to generic fallback: %s", err_str[:200])
    return "Analysis encountered an unexpected error. Please try again or contact an admin if the issue persists."


def tracker_credential_message(kind: str, *, has_profile: bool = True) -> str:
    """The missing-tracker-credential sentence, in a wording the reader can follow.

    The bootstrap ``admin`` has no ``users`` row, so ``PUT /api/user/tokens``
    answers 404 for it: pointing it at Profile Settings is a dead end. Both the
    HTTP detail and the chat prompt use this wording, so a credential error
    never sends an account somewhere it cannot go.

    Args:
        kind: The tracker name (``"GitHub"``/``"Jira"``).
        has_profile: False for a caller with no ``users`` row to save a token in.
    """
    if has_profile:
        return f"{kind} token is required. Set up your token in Profile Settings."
    return (
        f"{kind} token is required, and this account has no profile to store one. "
        "Sign in with your own account and add the token in Profile Settings."
    )


def ai_not_configured_message(what: str, *, is_admin: bool = False) -> str:
    """Build a role-aware error message when AI provider/model is not configured."""
    if is_admin:
        return (
            f"{what} is not configured. "
            f"Go to Server Settings \u2192 AI to configure the default provider and model."
        )
    return (
        f"{what} is not configured on this server. "
        f"Please contact a server administrator to configure AI settings."
    )
