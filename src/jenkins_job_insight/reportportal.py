"""Report Portal integration for pushing JJI classifications into RP test items.

Maps JJI AI classifications to Report Portal defect types and pushes
classification results, analysis text, and Jira matches into RP launches.
"""

from __future__ import annotations

import os
import threading
import warnings
from typing import TYPE_CHECKING, Literal

import requests as _requests
import urllib3
from reportportal_client import RPClient
from simple_logger.logger import get_logger

if TYPE_CHECKING:
    from jenkins_job_insight.models import FailureAnalysis

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


def _noop_prefetch(self: RPClient) -> None:  # type: ignore[no-untyped-def]
    """No-op replacement: skip the background thread, mark event done."""
    self._api_info_prefetched.set()


setattr(RPClient, _PREFETCH_ATTR, _noop_prefetch)


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


# JJI classification -> RP defect type category
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


class ReportPortalClient:
    """Client for pushing JJI classifications into Report Portal.

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
        self, url: str, token: str, project: str, *, verify_ssl: bool = True
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

    def __enter__(self) -> ReportPortalClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _map_classification(
        self,
        classification: str,
        history_classification: str | None = None,
        locators: dict[str, str] | None = None,
    ) -> str | None:
        """Map a JJI classification to an RP defect type locator.

        If *history_classification* is ``INFRASTRUCTURE``, maps to System Issue
        regardless of the AI classification.

        Args:
            classification: JJI AI classification (e.g. ``PRODUCT BUG``).
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

    def _paginate_get(self, url: str, params: dict[str, str | int]) -> list[dict]:
        """Paginate a GET endpoint that returns ``{content, page}``.

        Args:
            url: RP API endpoint URL.
            params: Base query parameters (``page.page`` is managed internally).

        Returns:
            Aggregated list of items from all pages.
        """
        all_items: list[dict] = []
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

    def get_failed_items(self, launch_id: int) -> list[dict]:
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
        rp_items: list[dict],
        jji_failures: list[FailureAnalysis],
    ) -> list[tuple[dict, FailureAnalysis]]:
        """Match RP test items to JJI failure analyses by test name.

        Multiple RP items CAN match the same JJI failure (e.g. when a
        flaky test fails multiple times in the same launch).

        Matching strategy (in order):
        1. Exact match on ``name`` or ``codeRef``
        2. Dotted-suffix match on ``name`` in either direction: JJI FQN
           ends with ``.{rp_name}`` *or* RP name ends with
           ``.{jji_name}``.
        3. Dotted-suffix match on ``codeRef`` in either direction: JJI
           FQN ends with ``.{rp_codeRef}`` *or* RP codeRef ends with
           ``.{jji_name}``.

        Args:
            rp_items: List of RP item dicts.
            jji_failures: List of JJI FailureAnalysis objects.

        Returns:
            List of ``(rp_item, jji_failure)`` tuples.
        """
        matched: list[tuple[dict, FailureAnalysis]] = []

        for rp_item in rp_items:
            rp_name = rp_item.get("name", "")
            rp_code_ref = rp_item.get("codeRef", "")

            for failure in jji_failures:
                jji_name = failure.test_name

                # Exact match on name or codeRef
                if jji_name == rp_name or (rp_code_ref and jji_name == rp_code_ref):
                    matched.append((rp_item, failure))
                    break

                # Dotted-suffix match in either direction (see docstring)
                if jji_name.endswith(f".{rp_name}") or rp_name.endswith(f".{jji_name}"):
                    matched.append((rp_item, failure))
                    break

                # Dotted-suffix match against codeRef
                if rp_code_ref and (
                    jji_name.endswith(f".{rp_code_ref}")
                    or rp_code_ref.endswith(f".{jji_name}")
                ):
                    matched.append((rp_item, failure))
                    break

        return matched

    def push_classifications(
        self,
        matched_pairs: list[tuple[dict, FailureAnalysis]],
        report_url: str,
        history_classifications: dict[str, str] | None = None,
    ) -> dict:
        """Push JJI classifications into RP test items.

        For each matched pair, builds an issue update with:
        - Defect type locator mapped from JJI classification
        - Comment with link to JJI report page
        - External system issues for Jira matches (if present)

        Args:
            matched_pairs: List of ``(rp_item, jji_failure)`` tuples.
            report_url: URL to the JJI report page.
            history_classifications: Optional mapping of test name to
                history classification (e.g. ``INFRASTRUCTURE``).

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
        except Exception:
            logger.warning("Failed to fetch RP defect type locators, using defaults")
            locators = dict(_DEFAULT_LOCATORS)

        history = history_classifications or {}
        unmatched: list[str] = []
        errors: list[str] = []
        launch_id: int | None = None

        base = self._rp_client.base_url_v1

        # Build batch payload — one entry per matched item
        bulk_issues: list[dict] = []

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

            if not locator:
                unmatched.append(item_name)
                continue

            # Build comment
            comment = (
                f"See AI failure analysis under: [JJI Failure Analysis]({report_url})"
            )

            # Build issue update payload (RP API uses camelCase)
            issue_payload: dict = {
                "issueType": locator,
                "comment": comment,
                "autoAnalyzed": False,
                "ignoreAnalyzer": True,
            }

            # Add Jira matches as external issues
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
                    except Exception:
                        pass
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
            except Exception as exc:
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
