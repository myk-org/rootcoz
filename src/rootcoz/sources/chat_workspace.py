"""Chat workspace population for CI source plugins.

Keeps CI-specific chat setup out of ``engine/`` so the chat engine stays
CI-agnostic.  Jenkins setup is injected from ``main.py`` at import time
because its implementation still lives in ``engine/chat.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rootcoz.sources.base import CISource
from rootcoz.sources.jenkins_source import JenkinsSource
from rootcoz.sources.prow_source import ProwSource

logger = logging.getLogger(__name__)

SOURCE_REGISTRY: dict[str, type[CISource]] = {
    "prow": ProwSource,
    "jenkins": JenkinsSource,
}

_JenkinsChatSetup = Callable[[Path, dict], Awaitable[bool]]
_jenkins_chat_setup: _JenkinsChatSetup | None = None


def register_jenkins_chat_setup(handler: _JenkinsChatSetup) -> None:
    """Register Jenkins chat workspace setup (called from ``main``)."""
    global _jenkins_chat_setup
    _jenkins_chat_setup = handler


def reconstruct_source(
    analysis_type: str,
    source_params: dict,
    settings: Any = None,
    *,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> CISource | None:
    """Reconstruct a CISource plugin from stored request params."""
    source_cls = SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        return None

    return source_cls.from_stored_params(
        source_params,
        settings,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
    )


async def setup_ci_build_workspace(
    workspace: Path,
    request_params: dict,
    *,
    github_token: str = "",
    settings: Any = None,
) -> bool:
    """Populate the chat workspace with CI build data for the analyzed job."""
    analysis_type = request_params.get("analysis_type", "jenkins")
    if analysis_type == "jenkins":
        if _jenkins_chat_setup is None:
            logger.warning("Jenkins chat workspace setup is not registered")
            return False
        return await _jenkins_chat_setup(workspace, request_params)

    source = reconstruct_source(analysis_type, request_params, settings)
    if source is None:
        return False

    try:
        return await source.populate_chat_workspace(
            workspace, github_token=github_token
        )
    except Exception:
        logger.warning(
            "Chat: failed to populate %s workspace", analysis_type, exc_info=True
        )
        return False
