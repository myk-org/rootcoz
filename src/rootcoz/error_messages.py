"""User-friendly error message mapping.

Converts internal/technical errors into messages suitable for end users.
"""

from __future__ import annotations


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
        return (
            "The requested Jenkins job or build is no longer available. "
            "The build data may have been deleted or expired."
        )

    if (
        "authentication" in err_lower
        or "unauthorized" in err_lower
        or "401" in err_lower
    ):
        return "Jenkins authentication failed. Please verify your Jenkins credentials."

    if (
        "connection" in err_lower
        or "timeout" in err_lower
        or "unreachable" in err_lower
    ):
        return (
            "Could not connect to Jenkins. The server may be temporarily unavailable."
        )

    if "sidecar" in err_lower or "ai" in err_lower and "failed" in err_lower:
        return "The AI service is temporarily unavailable. Please try again."

    if "ssl" in err_lower or "certificate" in err_lower:
        return "SSL certificate verification failed when connecting to an external service."

    if "clone" in err_lower or "repository" in err_lower:
        return (
            "Failed to clone a source code repository. "
            "The repository may be unavailable or require authentication."
        )

    if "decrypt" in err_lower:
        return "Could not decrypt stored credentials. The server encryption key may have changed."

    # Generic fallback — no internal details
    return "Analysis encountered an unexpected error. Please try again or contact an admin if the issue persists."
