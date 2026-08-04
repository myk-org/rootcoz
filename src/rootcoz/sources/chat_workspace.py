"""Chat workspace population for CI source plugins.

Keeps CI-specific chat setup out of ``engine/`` so the chat engine stays
CI-agnostic. All CI types go through ``SOURCE_REGISTRY`` →
``CISource.populate_chat_workspace()``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rootcoz.sources.base import CISource
from rootcoz.sources.registry import SOURCE_REGISTRY, get_source_class

logger = logging.getLogger(__name__)


def reconstruct_source(
    analysis_type: str,
    source_params: dict[str, Any],
    settings: Any = None,
    *,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> CISource | None:
    """Reconstruct a CISource plugin from stored request params."""
    source_cls = get_source_class(analysis_type)
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
    request_params: dict[str, Any],
    *,
    github_token: str = "",
    settings: Any = None,
) -> bool:
    """Populate the chat workspace with CI build data for the analyzed job."""
    analysis_type = request_params.get("analysis_type", "jenkins")
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
    finally:
        # Drop any leftover extract dirs not transferred to the workspace
        # symlink (successful chat links nullify _extract_path first).
        source.cleanup()


# Re-export for callers that imported SOURCE_REGISTRY from this module.
__all__ = [
    "SOURCE_REGISTRY",
    "reconstruct_source",
    "setup_ci_build_workspace",
]
