"""Report Portal integration for pushing rootcoz classifications into RP test items.

Maps rootcoz AI classifications to Report Portal defect types and pushes
classification results, analysis text, and Jira matches into RP launches.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.parse
import warnings
from typing import TYPE_CHECKING, Any, Literal

import requests as _requests
import urllib3
from pydantic import ValidationError
from reportportal_client import RPClient
from simple_logger.logger import get_logger

from rootcoz.exporters.base import ExportContext, Exporter, ExporterResult

if TYPE_CHECKING:
    from rootcoz.models import FailureAnalysis

# Exception types caught around each RP API call in ReportPortalClient.push().
# OSError covers raw socket errors (builtin ConnectionError) and all
# requests-level transport faults (RequestException is a subclass of OSError).
_RP_PUSH_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    TypeError,
    RuntimeError,
    KeyError,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))
_RPCLIENT_INIT_LOCK = threading.Lock()

# Disable RPClient's background "RP-API-Info-Prefetch" thread.  RPClient
# spawns a daemon thread during __init__ that calls get_api_info(); when
# RP is unreachable the library dumps full exception tracebacks to logs.
# We never use api_info (all API calls go through our own Session), so
# prevent the thread from starting and set the prefetch event immediately
# so any internal wait on it returns instantly.
#
# This patches a name-mangled private method; if a future library upgrade
# removes it, the ImportError here will surface immediately at import
# time rather than silently reverting to noisy behaviour.
_PREFETCH_ATTR = "_RPClient__init_api_info_prefetch"
if not hasattr(RPClient, _PREFETCH_ATTR):
    raise ImportError(
        f"reportportal_client.RPClient no longer has '{_PREFETCH_ATTR}'; "
        "update the prefetch suppression patch in reportportal.py"
    )


def _noop_prefetch(self: Any) -> None:
    """No-op replacement: skip the background thread, mark event done."""
    self._api_info_prefetched.set()


setattr(RPClient, _PREFETCH_ATTR, _noop_prefetch)


def format_rp_error(exc: Exception, operation: str) -> tuple[str, str]:
    """Build a short user-facing message and a detailed log message.

    Returns:
        Tuple of ``(user_message, log_detail)``.
        *user_message* is short and suitable for API responses.
        *log_detail* contains the full exception context for server logs.
    """
    detail = ""
    rp_message = ""
    status = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = str(resp.status_code)
        try:
            rp_body = resp.json()
            raw = rp_body.get("message") if isinstance(rp_body, dict) else None
            # RP JSON "message" field — short, user-friendly
            rp_message = raw if isinstance(raw, str) else ""
            # Full response text — log only
            detail = resp.text or ""
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            detail = resp.text or ""
    else:
        detail = str(exc) if str(exc) else ""

    # User message: short — operation + status + RP message (if any)
    if status:
        user_msg = f"Error {operation} (HTTP {status})"
        if rp_message:
            user_msg += f": {rp_message}"
    else:
        user_msg = f"Error {operation}"

    # Log message: full technical detail
    log_msg = f"{type(exc).__name__} {operation}"
    if status:
        log_msg = f"{status} ({type(exc).__name__}) {operation}"
    if detail:
        log_msg += f": {detail}"

    return user_msg, log_msg


class AmbiguousLaunchError(Exception):
    """Multiple RP launches matched but none could be disambiguated."""

    def __init__(self, count: int, job_name: str, jenkins_url: str) -> None:
        self.count = count
        self.job_name = job_name
        self.jenkins_url = jenkins_url
        super().__init__(
            f"Found {count} launches matching jenkins_url='{jenkins_url}'"
            f" for job '{job_name}'. Cannot disambiguate."
        )


# rootcoz classification -> RP defect type category
_CLASSIFICATION_MAP: dict[str, str] = {
    "PRODUCT BUG": "PRODUCT_BUG",
    "CODE ISSUE": "AUTOMATION_BUG",
    "INFRASTRUCTURE": "SYSTEM_ISSUE",
}

# Default RP locators (used as fallback when project settings unavailable)
_DEFAULT_LOCATORS: dict[str, str] = {
    "PRODUCT_BUG": "pb001",
    "AUTOMATION_BUG": "ab001",
    "SYSTEM_ISSUE": "si001",
    "TO_INVESTIGATE": "ti001",
}


def _extract_bts_fields(url: str) -> tuple[str, str]:
    """Extract btsProject and ticketId from a tracked-in URL.

    - GitHub: btsProject = "org/repo", ticketId = issue/PR number
    - Jira: btsProject = project key, ticketId = issue key
    - Other: btsProject = hostname, ticketId = last path segment
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    hostname = parsed.hostname or ""
    ticket_id = segments[-1] if segments else hostname

    # github.com/org/repo/issues/123 or github.com/org/repo/pull/123/files
    # Use segments[3] (the number), not segments[-1] (may be 'files', 'commits')
    if (
        "github" in hostname
        and len(segments) >= 4
        and segments[2] in {"issues", "pull"}
    ):
        return f"{segments[0]}/{segments[1]}", segments[3]

    # Jira: ticketId like PROJ-123, btsProject = PROJ
    if ("jira" in hostname or "atlassian" in hostname) and "-" in ticket_id:
        return ticket_id.split("-")[0], ticket_id

    # Fallback: hostname as project, last segment as ticket
    return hostname, ticket_id


class ReportPortalClient(Exporter):
    """Client for pushing rootcoz classifications into Report Portal.

    Uses the ``reportportal-client`` package to communicate with the RP API.
    Supports the context manager protocol for automatic cleanup.

    Args:
        url: Report Portal server URL.
        token: API token for authentication.
        project: RP project name.
        verify_ssl: Verify TLS certificates. Set to ``False`` for
            self-signed certificates.
    """

    def __init__(
        self,
        url: str,
        token: str,
        project: str,
        *,
        verify_ssl: bool = True,
        push_classifications: bool = True,
        push_rootcoz_url: bool = True,
        push_tracker_links: bool = True,
    ) -> None:
        # Build our own requests.Session for custom API calls instead of
        # relying on RPClient.session, which may not honour verify_ssl
        # for all requests (observed with self-signed certificates).
        # Initialised before RPClient so close() can always clean up.
        self._session = _requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.verify = verify_ssl
        self._suppress_ssl_warnings = not verify_ssl
        if not _RPCLIENT_INIT_LOCK.acquire(timeout=30):
            self._session.close()
            raise TimeoutError("Timed out waiting for RPClient initialisation lock")
        try:
            self._rp_client = RPClient(
                endpoint=url.rstrip("/"),
                project=project,
                api_key=token,
                verify_ssl=verify_ssl,
            )
        except Exception:
            self._session.close()
            raise
        finally:
            _RPCLIENT_INIT_LOCK.release()
        self._push_classifications = push_classifications
        self._push_rootcoz_url = push_rootcoz_url
        self._push_tracker_links = push_tracker_links

    # -- Exporter ABC ---------------------------------------------------------

    NAME = "reportportal"
    DISPLAY_NAME = "Report Portal"
    needs_history_classifications = True

    @property
    def name(self) -> str:
        """Machine-readable exporter identifier."""
        return self.NAME

    @property
    def display_name(self) -> str:
        """Human-readable exporter name."""
        return self.DISPLAY_NAME

    @property
    def is_enabled(self) -> bool:
        """Whether this client instance is ready to use.

        Always ``True`` for an instantiated client — construction requires
        valid URL, token, and project.  Enablement gating is handled by
        ``_create_exporter()`` which checks ``Settings.reportportal_enabled``
        and ``PUBLIC_BASE_URL`` before constructing the client.
        """
        return True

    @staticmethod
    def _failure_result(
        message: str, *, launch_id: int | None = None
    ) -> ExporterResult:
        """Build a standard failure ExporterResult."""
        return ExporterResult(
            success=False,
            message=message,
            details={
                "pushed": 0,
                "unmatched": [],
                "errors": [message],
                "launch_id": launch_id,
            },
        )

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push analysis results to Report Portal.

        Finds the matching RP launch, matches failed items to rootcoz
        failures, and pushes classifications with optional comments
        and tracker links.
        """
        from rootcoz.models import FailureAnalysis

        failures_data = context.failures
        if not failures_data:
            return self._failure_result("No failures to push to Report Portal.")

        # Validate at least one push content toggle is enabled
        if not any(
            (
                self._push_classifications,
                self._push_rootcoz_url,
                self._push_tracker_links,
            )
        ):
            msg = (
                "All Report Portal push content toggles are disabled. "
                "Enable at least one of: rp_push_classifications, rp_push_rootcoz_url, rp_push_tracker_links."
            )
            return self._failure_result(msg)

        jenkins_url = context.jenkins_url
        job_name = context.job_name

        logger.debug(
            "RP push: searching for launch job='%s' #%s, jenkins_url='%s'",
            job_name,
            context.build_number,
            jenkins_url,
        )

        try:
            launch_id = await asyncio.to_thread(self.find_launch, job_name, jenkins_url)
        except AmbiguousLaunchError as exc:
            logger.warning("RP push: %s", exc)
            msg = f"Ambiguous RP launch: found {exc.count} launches. Remove duplicate launches to disambiguate."
            return self._failure_result(msg)
        except _RP_PUSH_ERRORS as exc:
            msg, log_msg = format_rp_error(exc, "searching RP launches")
            logger.error(
                "RP push failed: %s, job='%s' #%s, jenkins_url='%s'",
                log_msg,
                job_name,
                context.build_number,
                jenkins_url,
            )
            return self._failure_result(msg)

        if launch_id is None:
            msg = "No Report Portal launch found. Ensure the Jenkins build URL is in the RP launch description."
            logger.error(
                "RP push failed: %s, job='%s' #%s, jenkins_url='%s'",
                msg,
                job_name,
                context.build_number,
                jenkins_url,
            )
            return self._failure_result(msg)

        try:
            failed_items = await asyncio.to_thread(self.get_failed_items, launch_id)
        except _RP_PUSH_ERRORS as exc:
            msg, log_msg = format_rp_error(exc, "fetching failed items from RP")
            logger.error(
                "RP push failed: %s, job='%s' #%s, launch_id=%s",
                log_msg,
                job_name,
                context.build_number,
                launch_id,
            )
            return self._failure_result(msg, launch_id=launch_id)

        if not failed_items:
            logger.debug(
                "RP push: no failed items in launch_id=%d for job='%s'",
                launch_id,
                job_name,
            )
            return self._failure_result(
                "No failed test items found in RP launch.", launch_id=launch_id
            )

        # Build FailureAnalysis objects from stored result
        try:
            rcz_failures = [FailureAnalysis.model_validate(f) for f in failures_data]
        except ValidationError as exc:
            msg = f"Stored result contains invalid failure data: {exc.error_count()} validation error(s)"
            logger.warning(
                "RP push: %s, job='%s' #%s",
                msg,
                job_name,
                context.build_number,
            )
            return self._failure_result(msg, launch_id=launch_id)

        try:
            matched = await asyncio.to_thread(
                self.match_failures, failed_items, rcz_failures
            )
        except _RP_PUSH_ERRORS as exc:
            msg, log_msg = format_rp_error(exc, "matching RP items to failures")
            logger.error(
                "RP push failed: %s, job='%s' #%s, launch_id=%s",
                log_msg,
                job_name,
                context.build_number,
                launch_id,
            )
            return self._failure_result(msg, launch_id=launch_id)

        if not matched and failed_items and rcz_failures:
            rp_names = [item.get("name", "") for item in failed_items]
            rcz_names = [f.test_name for f in rcz_failures]
            msg = f"No overlap between {len(failed_items)} RP item(s) and {len(rcz_failures)} rootcoz failure(s)."
            log_detail = f"{msg} RP items: {', '.join(rp_names)}. rootcoz tests: {', '.join(rcz_names)}."
            logger.error(
                "RP push failed: %s, job='%s' #%s, launch_id=%s",
                log_detail,
                job_name,
                context.build_number,
                launch_id,
            )
            return self._failure_result(msg, launch_id=launch_id)

        try:
            push_result = await asyncio.to_thread(
                self.push_classifications,
                matched,
                context.report_url,
                context.history_classifications,
                push_classifications=self._push_classifications,
                push_rootcoz_url=self._push_rootcoz_url,
                push_tracker_links=self._push_tracker_links,
                tracked_in_links=context.tracked_in_links,
                pushed_by=context.pushed_by,
                reviewed_by=context.reviewed_by,
            )
        except _RP_PUSH_ERRORS as exc:
            msg, log_msg = format_rp_error(exc, "pushing classifications to RP")
            logger.error(
                "RP push failed: %s, job='%s' #%s, launch_id=%s",
                log_msg,
                job_name,
                context.build_number,
                launch_id,
            )
            return self._failure_result(msg, launch_id=launch_id)

        push_result["launch_id"] = launch_id
        pushed = push_result.get("pushed", 0)
        errors = push_result.get("errors", [])
        success = pushed > 0 and not errors
        message = (
            f"Pushed {pushed} classification(s) to Report Portal"
            if success
            else (errors[0] if errors else "Push completed with no items")
        )

        return ExporterResult(
            success=success,
            message=message,
            details=push_result,
        )

    def _map_classification(
        self,
        classification: str,
        history_classification: str | None = None,
        locators: dict[str, str] | None = None,
    ) -> str | None:
        """Map a rootcoz classification to an RP defect type locator.

        If *history_classification* is ``INFRASTRUCTURE``, maps to System Issue
        regardless of the AI classification.

        Args:
            classification: rootcoz AI classification (e.g. ``PRODUCT BUG``).
            history_classification: Optional history classification from
                test_classifications table.
            locators: Project-specific defect type locators. Falls back
                to ``_DEFAULT_LOCATORS`` when ``None`` or missing key.

        Returns:
            RP locator string (e.g. ``pb001``), or ``None`` if no mapping.
        """
        effective = classification
        if history_classification == "INFRASTRUCTURE":
            effective = "INFRASTRUCTURE"

        rp_category = _CLASSIFICATION_MAP.get(effective)
        if not rp_category:
            return None

        if locators and rp_category in locators:
            return locators[rp_category]
        return _DEFAULT_LOCATORS.get(rp_category)

    def _paginate_get(
        self, url: str, params: dict[str, str | int]
    ) -> list[dict[str, Any]]:
        """Paginate a GET endpoint that returns ``{content, page}``.

        Args:
            url: RP API endpoint URL.
            params: Base query parameters (``page.page`` is managed internally).

        Returns:
            Aggregated list of items from all pages.
        """
        all_items: list[dict[str, Any]] = []
        params = {**params}  # avoid mutating caller's dict
        page = 1

        while True:
            params["page.page"] = page
            response = self._request("get", url, params=params)
            response.raise_for_status()
            data = response.json()
            all_items.extend(data.get("content", []))

            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages")
            if not isinstance(total_pages, int) or total_pages < 0:
                logger.warning(f"Invalid totalPages from RP: {total_pages}")
                break
            if page >= total_pages:
                break
            page += 1

        return all_items

    def get_defect_type_locators(self) -> dict[str, str]:
        """Fetch defect type locators from RP project settings.

        Returns:
            Mapping of RP defect category to locator string,
            e.g. ``{"PRODUCT_BUG": "pb_xxxxx", ...}``.
        """
        url = f"{self._rp_client.base_url_v1}/settings"
        response = self._request("get", url)
        response.raise_for_status()
        settings = response.json()
        sub_types = settings.get("subTypes", {})
        result: dict[str, str] = {}
        for category, items in sub_types.items():
            if items and isinstance(items, list):
                result[category] = items[0]["locator"]
        return result

    def find_launch(self, job_name: str, jenkins_url: str) -> int | None:
        """Find an RP launch matching the given Jenkins build.

        Searches recent launches in the project and matches by checking
        whether the launch **description** contains *jenkins_url*.  The
        Jenkins URL is unique per build and is a reliable identifier
        regardless of launch naming conventions.

        Args:
            job_name: Jenkins job name (used for error context).
            jenkins_url: Full Jenkins build URL used as identifier.

        Returns:
            Numeric launch ID, or ``None`` if no match found.

        Raises:
            AmbiguousLaunchError: Multiple launches matched by
                description (URL query) and cannot be disambiguated.
        """
        base = self._rp_client.base_url_v1
        url = f"{base}/launch"

        url_matches = self._paginate_get(
            url,
            {
                "filter.cnt.description": jenkins_url,
                "page.size": 50,
                "page.sort": "startTime,desc",
            },
        )

        if len(url_matches) == 1:
            return url_matches[0]["id"]

        if len(url_matches) > 1:
            raise AmbiguousLaunchError(len(url_matches), job_name, jenkins_url)

        return None

    def get_failed_items(self, launch_id: int) -> list[dict[str, Any]]:
        """Get all failed test items from a launch.

        Handles pagination to collect all results.

        Args:
            launch_id: Numeric RP launch ID.

        Returns:
            List of item dicts from the RP API.
        """
        base = self._rp_client.base_url_v1
        url = f"{base}/item"
        return self._paginate_get(
            url,
            {
                "filter.eq.launchId": launch_id,
                "filter.eq.status": "FAILED",
                "filter.eq.type": "STEP",
                "page.size": 300,
            },
        )

    def match_failures(
        self,
        rp_items: list[dict[str, Any]],
        rcz_failures: list[FailureAnalysis],
    ) -> list[tuple[dict[str, Any], FailureAnalysis]]:
        """Match RP test items to rootcoz failure analyses by test name.

        Multiple RP items CAN match the same rootcoz failure (e.g. when a
        flaky test fails multiple times in the same launch).

        Matching strategy (in order):
        1. Exact match on ``name`` or ``codeRef``
        2. Dotted-suffix match on ``name`` in either direction: rootcoz FQN
           ends with ``.{rp_name}`` *or* RP name ends with
           ``.{rcz_name}``.
        3. Dotted-suffix match on ``codeRef`` in either direction: rootcoz
           FQN ends with ``.{rp_codeRef}`` *or* RP codeRef ends with
           ``.{rcz_name}``.

        Args:
            rp_items: List of RP item dicts.
            rcz_failures: List of rootcoz FailureAnalysis objects.

        Returns:
            List of ``(rp_item, rcz_failure)`` tuples.
        """
        matched: list[tuple[dict[str, Any], FailureAnalysis]] = []

        for rp_item in rp_items:
            rp_name = rp_item.get("name", "")
            rp_code_ref = rp_item.get("codeRef", "")

            for failure in rcz_failures:
                rcz_name = failure.test_name

                # Exact match on name or codeRef
                if rcz_name == rp_name or (rp_code_ref and rcz_name == rp_code_ref):
                    matched.append((rp_item, failure))
                    break

                # Dotted-suffix match in either direction (see docstring)
                if rcz_name.endswith(f".{rp_name}") or rp_name.endswith(f".{rcz_name}"):
                    matched.append((rp_item, failure))
                    break

                # Dotted-suffix match against codeRef
                if rp_code_ref and (
                    rcz_name.endswith(f".{rp_code_ref}")
                    or rp_code_ref.endswith(f".{rcz_name}")
                ):
                    matched.append((rp_item, failure))
                    break

        return matched

    def push_classifications(
        self,
        matched_pairs: list[tuple[dict[str, Any], FailureAnalysis]],
        report_url: str,
        history_classifications: dict[str, str] | None = None,
        *,
        push_classifications: bool = True,
        push_rootcoz_url: bool = True,
        push_tracker_links: bool = True,
        tracked_in_links: dict[str, list[dict[str, Any]]] | None = None,
        pushed_by: str = "",
        reviewed_by: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Push rootcoz classifications into RP test items.

        For each matched pair, builds an issue update with:
        - Defect type locator mapped from rootcoz classification
        - Comment with link to rootcoz report page
        - External system issues for Jira matches (if present)

        Each component can be independently toggled:

        Args:
            matched_pairs: List of ``(rp_item, rcz_failure)`` tuples.
            report_url: URL to the rootcoz report page.
            history_classifications: Optional mapping of test name to
                history classification (e.g. ``INFRASTRUCTURE``).
            push_classifications: When ``True`` (default), map rootcoz
                classifications to RP defect types. When ``False``,
                set items to ``TO_INVESTIGATE`` instead of mapping
                the rootcoz classification.
            push_rootcoz_url: When ``True`` (default), include a comment
                with a link to the rootcoz report page.
            push_tracker_links: When ``True`` (default), attach Jira
                matches as external system issues.
            tracked_in_links: Optional mapping of test name to list of
                tracked-in link dicts (each with ``tracked_in_url`` and
                ``tracked_in_type`` keys). Merged with AI Jira matches
                when ``push_tracker_links`` is ``True``.
            pushed_by: Username of the user who triggered the push.
                When non-empty, a "Pushed by <username>" line is
                appended to the RP comment.
            reviewed_by: Mapping of test name to reviewer username.
                When the test has a reviewer, a "Reviewed by <username>"
                line is appended to the RP comment.

        Returns:
            Dict with keys: ``pushed``, ``unmatched``, ``errors``, ``launch_id``.
        """
        if not matched_pairs:
            return {
                "pushed": 0,
                "unmatched": [],
                "errors": [],
                "launch_id": None,
            }

        # Fetch actual locators from project settings
        try:
            locators = self.get_defect_type_locators()
        except (
            _requests.exceptions.RequestException,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
        ):
            logger.warning("Failed to fetch RP defect type locators, using defaults")
            locators = dict(_DEFAULT_LOCATORS)

        history = history_classifications or {}
        reviewed_by = reviewed_by or {}
        unmatched: list[str] = []
        errors: list[str] = []
        launch_id: int | None = None

        base = self._rp_client.base_url_v1

        # Build batch payload — one entry per matched item
        bulk_issues: list[dict[str, Any]] = []

        for rp_item, failure in matched_pairs:
            item_id = rp_item.get("id")
            item_name = rp_item.get("name", "")
            if item_id is None:
                errors.append(f"RP item missing 'id' field ({item_name})")
                continue
            if launch_id is None:
                launch_id = rp_item.get("launchId")

            # Determine classification
            ai_classification = failure.analysis.classification
            hist_cls = history.get(failure.test_name)
            locator = self._map_classification(ai_classification, hist_cls, locators)

            if push_classifications:
                if not locator:
                    unmatched.append(item_name)
                    continue
                issue_type = locator
            else:
                # RP API requires issueType; use TO_INVESTIGATE as neutral default
                issue_type = (
                    locators.get("TO_INVESTIGATE")
                    or _DEFAULT_LOCATORS["TO_INVESTIGATE"]
                )

            # Build issue update payload (RP API uses camelCase)
            issue_payload: dict[str, Any] = {
                "issueType": issue_type,
                "autoAnalyzed": False,
                "ignoreAnalyzer": True,
            }

            if push_rootcoz_url:
                comment = f"See AI failure analysis under: [rootcoz Failure Analysis]({report_url})"
                if pushed_by:
                    comment += f"\nPushed by {pushed_by}"
                reviewer = reviewed_by.get(failure.test_name, "")
                if reviewer:
                    comment += f"\nReviewed by {reviewer}"
                issue_payload["comment"] = comment

            # Add Jira matches as external issues
            if push_tracker_links:
                external_issues = []
                pbr = failure.analysis.product_bug_report
                if pbr and not isinstance(pbr, bool) and pbr.jira_matches:
                    for jira_match in pbr.jira_matches:
                        external_issues.append(
                            {
                                "url": jira_match.url,
                                "btsProject": jira_match.key.split("-")[0]
                                if "-" in jira_match.key
                                else "",
                                "btsUrl": jira_match.url,
                                "ticketId": jira_match.key,
                            }
                        )

                # Add user-tracked links (from tracked_in_links table)
                if tracked_in_links:
                    seen_urls = {ei["url"] for ei in external_issues}
                    for link in tracked_in_links.get(failure.test_name, []):
                        link_url = link.get("tracked_in_url", "")
                        if link_url and link_url not in seen_urls:
                            seen_urls.add(link_url)
                            bts_project, ticket_id = _extract_bts_fields(link_url)
                            external_issues.append(
                                {
                                    "url": link_url,
                                    "btsProject": bts_project,
                                    "btsUrl": link_url,
                                    "ticketId": ticket_id,
                                }
                            )

                if external_issues:
                    issue_payload["externalSystemIssues"] = external_issues

            bulk_issues.append({"testItemId": item_id, "issue": issue_payload})

        # Send single batch PUT to RP
        pushed = 0
        if bulk_issues:
            url = f"{base}/item"
            update_body = {"issues": bulk_issues}
            try:
                logger.debug("RP PUT %s payload: %s", url, update_body)
                response = self._request("put", url, json=update_body)
                response.raise_for_status()
                logger.debug(
                    "RP PUT %s response: %s (length=%s)",
                    url,
                    response.status_code,
                    len(response.content),
                )
                pushed = len(bulk_issues)
                logger.info("Pushed %d classification(s) to RP in one batch", pushed)
            except _requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                rp_message = ""
                response_body = ""
                if exc.response is not None:
                    response_body = exc.response.text
                    try:
                        rp_body = exc.response.json()
                        raw = (
                            rp_body.get("message")
                            if isinstance(rp_body, dict)
                            else None
                        )
                        rp_message = raw if isinstance(raw, str) else ""
                    except (ValueError, KeyError) as e:
                        logger.debug("Failed to parse RP error response: %s", e)
                log_body = response_body.replace("\r", "\\r").replace("\n", "\\n")
                logger.error(
                    "RP batch update failed: status=%s, url=%s,"
                    " items=%d, detail=%s, response_body=%s",
                    status,
                    url,
                    len(bulk_issues),
                    rp_message or "(no detail)",
                    log_body,
                )
                suffix = f": {rp_message}" if rp_message else ""
                error_msg = (
                    f"Error updating {len(bulk_issues)} item(s)"
                    f" (HTTP {status or '?'}){suffix}"
                )
                errors.append(error_msg)
            except (
                _requests.exceptions.RequestException,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                logger.error(
                    "RP batch update failed: url=%s, items=%d, error=%s",
                    url,
                    len(bulk_issues),
                    exc,
                )
                errors.append(f"Error updating {len(bulk_issues)} RP item(s)")

        return {
            "pushed": pushed,
            "unmatched": unmatched,
            "errors": errors,
            "launch_id": launch_id,
        }

    _DEFAULT_TIMEOUT: int = 30

    def _request(
        self, method: Literal["get", "put"], url: str, **kwargs: object
    ) -> _requests.Response:
        """HTTP request with scoped InsecureRequestWarning suppression.

        Applies a default timeout of 30 seconds if none is provided.
        """
        if "timeout" not in kwargs:
            kwargs["timeout"] = self._DEFAULT_TIMEOUT
        with warnings.catch_warnings():
            if self._suppress_ssl_warnings:
                warnings.filterwarnings(
                    "ignore", category=urllib3.exceptions.InsecureRequestWarning
                )
            return getattr(self._session, method)(url, **kwargs)

    def close(self) -> None:
        """Close the underlying RP client and HTTP session."""
        self._session.close()
        self._rp_client.close()
