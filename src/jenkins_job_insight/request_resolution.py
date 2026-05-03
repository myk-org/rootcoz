"""Shared request-parameter resolution helpers.

Extracted to avoid circular imports between main.py and analyzer.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jenkins_job_insight.config import Settings
    from jenkins_job_insight.models import BaseAnalysisRequest


def resolve_tests_repo_token(body: BaseAnalysisRequest, merged: Settings) -> str:
    """Resolve the effective tests repo token from request body or settings.

    Request body value takes precedence over the server-level setting.

    Args:
        body: The analysis request (may contain per-request token override).
        merged: Application settings (may contain server-level token).

    Returns:
        The resolved token string, or empty string if none configured.
    """
    if body.tests_repo_token is not None:
        return body.tests_repo_token
    if merged.tests_repo_token:
        return merged.tests_repo_token.get_secret_value()
    return ""
