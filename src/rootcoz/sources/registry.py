"""CI source plugin registry.

Single source of truth for mapping ``analysis_type`` → ``CISource`` class.
"""

from __future__ import annotations

from typing import Any

from rootcoz.sources.base import CISource
from rootcoz.sources.file_source import FileSource
from rootcoz.sources.jenkins_source import JenkinsSource
from rootcoz.sources.prow_source import ProwSource
from rootcoz.sources.raw_source import RawSource

# All reconstructable / chat-capable sources (includes Jenkins).
SOURCE_REGISTRY: dict[str, type[CISource]] = {
    "file": FileSource,
    "raw": RawSource,
    "prow": ProwSource,
    "jenkins": JenkinsSource,
}

# Shared analyze/enqueue path — all registered sources use this path.
CI_SOURCE_REGISTRY: dict[str, type[CISource]] = dict(SOURCE_REGISTRY)


def get_source_class(analysis_type: str) -> type[CISource] | None:
    """Return the CISource class for ``analysis_type``, if registered."""
    return SOURCE_REGISTRY.get(analysis_type)


def create_source_from_request(analysis_type: str, body: Any, merged: Any) -> CISource:
    """Construct a CISource for analyze/re-analyze via the plugin registry."""
    source_cls = CI_SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        raise ValueError(f"Unsupported analysis type: {analysis_type}")
    return source_cls.from_analyze_request(body, merged)
