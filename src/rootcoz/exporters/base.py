"""Base classes for the exporter plugin architecture.

Exporters push rootcoz analysis results to external systems
(e.g. Report Portal).  Each exporter implements the :class:`Exporter` ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Self


@dataclass
class ExportContext:
    """Shared metadata that any exporter needs to push results.

    Populated by the push endpoint from the stored analysis result and
    application settings.

    Attributes:
        job_id: The rootcoz analysis job identifier.
        job_name: CI job name (e.g. Jenkins job name).
        build_number: Display build number / build ID.
        jenkins_url: Full Jenkins build URL (used as launch identifier).
        failures: List of failure dicts from the stored result.
        report_url: Public URL to the rootcoz report page.
        child_job_name: Optional child job name for scoped push.
        child_build_number: Optional child build number.
        pushed_by: Username of the user who triggered the push.
        history_classifications: Mapping of test name to history classification.
        tracked_in_links: Mapping of test name to tracked-in link dicts.
        reviewed_by: Mapping of test name to reviewer username.
    """

    job_id: str
    job_name: str
    build_number: str
    jenkins_url: str
    failures: list[dict[str, Any]]
    report_url: str
    child_job_name: str | None = None
    child_build_number: int | None = None
    pushed_by: str = ""
    history_classifications: dict[str, str] = field(default_factory=dict)
    tracked_in_links: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reviewed_by: dict[str, str] = field(default_factory=dict)


@dataclass
class ExporterResult:
    """Generic result from an exporter push operation.

    Attributes:
        success: Whether the push completed without errors.
        message: Human-readable summary of the push result.
        details: Exporter-specific result data (e.g. pushed count, errors).
    """

    success: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class Exporter(ABC):
    """Abstract base class for exporter plugins.

    Subclasses must implement all abstract properties and the :meth:`push`
    method.  The ``push`` method receives an :class:`ExportContext` with all
    the metadata needed to push results to the external system.
    """

    #: Whether this exporter consumes per-test history classifications.
    #: When ``False`` (the default), the push pipeline skips the per-test,
    #: DB-backed history classification lookups when building the
    #: :class:`ExportContext`.  Subclasses that read
    #: :attr:`ExportContext.history_classifications` must set this to ``True``.
    needs_history_classifications: bool = False

    #: Whether this exporter consumes tracked-in links from storage.
    #: When ``False`` (the default), the push pipeline skips
    #: ``storage.get_tracked_in_for_scope`` when building the
    #: :class:`ExportContext`.  Subclasses that read
    #: :attr:`ExportContext.tracked_in_links` must set this to ``True``.
    needs_tracked_in_links: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Machine-readable exporter identifier (e.g. ``'reportportal'``)."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable exporter name (e.g. ``'Report Portal'``)."""

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this exporter is configured and ready to use."""

    @abstractmethod
    async def push(self, context: ExportContext) -> ExporterResult:
        """Push analysis results to the external system.

        Args:
            context: Export context with job metadata and failures.

        Returns:
            Result of the push operation.
        """

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(self, *args: object) -> None:
        """Exit the context manager — delegates to :meth:`close`."""
        self.close()

    def close(self) -> None:
        """Release resources.  Override in subclasses that hold connections."""
