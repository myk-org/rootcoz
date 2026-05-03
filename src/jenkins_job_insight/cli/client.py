"""HTTP client for the jenkins-job-insight REST API."""

from typing import Any

import httpx


class JJIError(Exception):
    """Error from the JJI API or connection failure."""

    def __init__(self, status_code: int = 0, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        msg = f"HTTP {status_code}" if status_code else "Connection error"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class JJIClient:
    """Synchronous client for the jenkins-job-insight REST API.

    All methods return parsed JSON dicts. Raises JJIError on HTTP
    errors or connection failures.

    Args:
        server_url: Base URL of the JJI server (required).
        timeout: Request timeout in seconds.
        username: Username sent as a cookie for authenticated actions.
        verify_ssl: Whether to verify SSL certificates (default True).
    """

    def __init__(
        self,
        server_url: str,
        timeout: float = 30.0,
        username: str = "",
        verify_ssl: bool = True,
        api_key: str = "",
    ):
        self.server_url = server_url.rstrip("/")
        cookies = {}
        if username:
            cookies["jji_username"] = username
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=self.server_url,
            timeout=timeout,
            cookies=cookies,
            headers=headers,
            verify=verify_ssl,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        accept_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        """Send an HTTP request and return parsed JSON.

        Args:
            method: HTTP method (GET, POST, DELETE, PUT).
            path: URL path (e.g. "/health").
            params: Query parameters.
            json: JSON body for POST/PUT.
            accept_statuses: HTTP status codes treated as success.

        Returns:
            Parsed JSON response.

        Raises:
            JJIError: On HTTP errors or connection failures.
        """
        # Strip None values from params
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != ""}

        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise JJIError(
                status_code=0,
                detail=f"Request timed out: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise JJIError(
                status_code=0,
                detail=f"Cannot connect to {self.server_url}: {exc}",
            ) from exc

        if response.status_code not in accept_statuses:
            detail = ""
            try:
                body = response.json()
                detail = body.get("detail", str(body))
            except (ValueError, KeyError):
                detail = response.text
            raise JJIError(
                status_code=response.status_code,
                detail=detail,
            )

        return response.json()

    # -- Auth -----------------------------------------------------------------

    def login(self, username: str, api_key: str) -> dict:
        """Login as admin. POST /api/auth/login"""
        return self._request(
            "POST",
            "/api/auth/login",
            json={"username": username, "api_key": api_key},
        )

    def logout(self) -> dict:
        """Logout (clear session). POST /api/auth/logout"""
        return self._request("POST", "/api/auth/logout")

    def auth_me(self) -> dict:
        """Get current user info. GET /api/auth/me"""
        return self._request("GET", "/api/auth/me")

    # -- Admin ----------------------------------------------------------------

    def admin_list_users(self) -> dict:
        """List all users. GET /api/admin/users"""
        return self._request("GET", "/api/admin/users")

    def admin_create_user(self, username: str) -> dict:
        """Create an admin user. POST /api/admin/users"""
        return self._request(
            "POST",
            "/api/admin/users",
            json={"username": username},
        )

    def admin_delete_user(self, username: str) -> dict:
        """Delete an admin user. DELETE /api/admin/users/{username}"""
        return self._request("DELETE", f"/api/admin/users/{username}")

    def admin_rotate_key(self, username: str) -> dict:
        """Rotate an admin user's API key. POST /api/admin/users/{username}/rotate-key"""
        return self._request("POST", f"/api/admin/users/{username}/rotate-key")

    def admin_change_role(self, username: str, role: str) -> dict:
        """Change a user's role. PUT /api/admin/users/{username}/role"""
        return self._request(
            "PUT",
            f"/api/admin/users/{username}/role",
            json={"role": role},
        )

    # -- Health ---------------------------------------------------------------

    def health(self) -> dict:
        """Check server health. GET /api/health

        Falls back to GET /health if /api/health returns 404.
        Accepts both 200 (healthy/degraded) and 503 (unhealthy).
        """
        try:
            return self._request("GET", "/api/health", accept_statuses=(200, 503))
        except JJIError as exc:
            if exc.status_code == 404:
                return self._request("GET", "/health")
            raise

    # -- Results --------------------------------------------------------------

    def list_results(self, limit: int = 50) -> list[dict]:
        """List recent analyzed jobs. GET /results?limit="""
        return self._request("GET", "/results", params={"limit": limit})

    def dashboard(self) -> list[dict]:
        """List analysis jobs with dashboard metadata. GET /api/dashboard"""
        return self._request("GET", "/api/dashboard")

    def get_active_count(self) -> dict:
        """Get count of currently active analyses. GET /api/dashboard/active-count"""
        return self._request("GET", "/api/dashboard/active-count")

    def get_result(self, job_id: str) -> dict:
        """Get a stored result by job_id. GET /results/{job_id}"""
        return self._request("GET", f"/results/{job_id}")

    def delete_job(self, job_id: str) -> dict:
        """Delete a job and all related data. DELETE /results/{job_id}"""
        return self._request("DELETE", f"/results/{job_id}")

    def delete_jobs_bulk(self, job_ids: list[str]) -> dict:
        """Delete multiple jobs. DELETE /api/results/bulk"""
        return self._request("DELETE", "/api/results/bulk", json={"job_ids": job_ids})

    # -- Analysis -------------------------------------------------------------

    def analyze(
        self,
        job_name: str,
        build_number: int,
        **kwargs,
    ) -> dict:
        """Submit a Jenkins job for analysis. POST /analyze

        Args:
            job_name: Jenkins job name.
            build_number: Build number to analyze.
            **kwargs: Additional fields for the AnalyzeRequest body.

        Returns:
            Queued status with job_id for polling.
        """
        body = {"job_name": job_name, "build_number": build_number, **kwargs}
        return self._request(
            "POST",
            "/analyze",
            json=body,
            accept_statuses=(202,),
        )

    def re_analyze(self, job_id: str) -> dict:
        """Re-analyze a previously analyzed job with the same settings. POST /re-analyze/{job_id}

        Args:
            job_id: Job ID of the original analysis to re-run.

        Returns:
            Queued status with new job_id for polling.
        """
        return self._request(
            "POST",
            f"/re-analyze/{job_id}",
            json={},
            accept_statuses=(202,),
        )

    # -- History --------------------------------------------------------------

    def get_test_history(
        self,
        test_name: str,
        limit: int = 20,
        job_name: str = "",
        exclude_job_id: str = "",
    ) -> dict:
        """Get pass/fail history for a test. GET /history/test/{test_name}"""
        return self._request(
            "GET",
            f"/history/test/{test_name}",
            params={
                "limit": limit,
                "job_name": job_name,
                "exclude_job_id": exclude_job_id,
            },
        )

    def search_by_signature(
        self,
        signature: str,
        exclude_job_id: str = "",
    ) -> dict:
        """Find tests by error signature. GET /history/search?signature="""
        return self._request(
            "GET",
            "/history/search",
            params={"signature": signature, "exclude_job_id": exclude_job_id},
        )

    def get_job_stats(
        self,
        job_name: str,
        exclude_job_id: str = "",
    ) -> dict:
        """Get job-level statistics. GET /history/stats/{job_name}"""
        return self._request(
            "GET",
            f"/history/stats/{job_name}",
            params={"exclude_job_id": exclude_job_id},
        )

    def get_all_failures(
        self,
        search: str = "",
        job_name: str = "",
        classification: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Get paginated failure history. GET /history/failures"""
        return self._request(
            "GET",
            "/history/failures",
            params={
                "search": search,
                "job_name": job_name,
                "classification": classification,
                "limit": limit,
                "offset": offset,
            },
        )

    # -- Classifications ------------------------------------------------------

    def classify_test(
        self,
        test_name: str,
        classification: str,
        job_id: str,
        reason: str = "",
        job_name: str = "",
        references: str = "",
        child_build_number: int = 0,
    ) -> dict:
        """Classify a test. POST /history/classify"""
        body = {
            "test_name": test_name,
            "classification": classification,
            "job_id": job_id,
            "reason": reason,
            "job_name": job_name,
            "references": references,
            "child_build_number": child_build_number,
        }
        return self._request(
            "POST", "/history/classify", json=body, accept_statuses=(201,)
        )

    def get_classifications(
        self,
        test_name: str = "",
        classification: str = "",
        job_name: str = "",
        parent_job_name: str = "",
        job_id: str = "",
    ) -> dict:
        """Get test classifications. GET /history/classifications"""
        return self._request(
            "GET",
            "/history/classifications",
            params={
                "test_name": test_name,
                "classification": classification,
                "job_name": job_name,
                "parent_job_name": parent_job_name,
                "job_id": job_id,
            },
        )

    # -- Comments -------------------------------------------------------------

    def get_comments(self, job_id: str) -> dict:
        """Get comments and reviews for a job. GET /results/{job_id}/comments"""
        return self._request("GET", f"/results/{job_id}/comments")

    def add_comment(
        self,
        job_id: str,
        test_name: str,
        comment: str,
        child_job_name: str = "",
        child_build_number: int = 0,
    ) -> dict:
        """Add a comment to a test failure. POST /results/{job_id}/comments"""
        body: dict = {
            "test_name": test_name,
            "comment": comment,
        }
        body = self._with_child_scope(body, child_job_name, child_build_number)
        return self._request(
            "POST",
            f"/results/{job_id}/comments",
            json=body,
            accept_statuses=(201,),
        )

    def delete_comment(self, job_id: str, comment_id: int) -> dict:
        """Delete a comment. DELETE /results/{job_id}/comments/{comment_id}"""
        return self._request("DELETE", f"/results/{job_id}/comments/{comment_id}")

    # -- Review ---------------------------------------------------------------

    def get_review_status(self, job_id: str) -> dict:
        """Get review summary for a job. GET /results/{job_id}/review-status"""
        return self._request("GET", f"/results/{job_id}/review-status")

    def set_reviewed(
        self,
        job_id: str,
        test_name: str,
        reviewed: bool,
        child_job_name: str = "",
        child_build_number: int = 0,
    ) -> dict:
        """Toggle the reviewed state for a test failure. PUT /results/{job_id}/reviewed"""
        body: dict = {"test_name": test_name, "reviewed": reviewed}
        body = self._with_child_scope(body, child_job_name, child_build_number)
        return self._request("PUT", f"/results/{job_id}/reviewed", json=body)

    def enrich_comments(self, job_id: str) -> dict:
        """Enrich comments with live PR/ticket statuses. POST /results/{job_id}/enrich-comments"""
        return self._request("POST", f"/results/{job_id}/enrich-comments")

    # -- Bug Creation ---------------------------------------------------------

    @staticmethod
    def _with_child_scope(
        payload: dict, child_job_name: str = "", child_build_number: int = 0
    ) -> dict:
        """Add child_job_name/child_build_number to *payload* when set."""
        if child_job_name:
            payload["child_job_name"] = child_job_name
            payload["child_build_number"] = child_build_number
        return payload

    def _build_tracker_body(
        self,
        test_name: str,
        child_job_name: str = "",
        child_build_number: int = 0,
        *,
        include_links: bool = False,
        ai_provider: str = "",
        ai_model: str = "",
        title: str = "",
        body_text: str = "",
        github_token: str = "",
        github_repo_url: str = "",
        jira_token: str = "",
        jira_email: str = "",
        jira_project_key: str = "",
        jira_security_level: str = "",
        include_github: bool = True,
        include_jira: bool = True,
        issue_prompt: str = "",
    ) -> dict:
        """Build the common payload for tracker preview/create endpoints."""
        payload: dict = {"test_name": test_name}
        if title:
            payload["title"] = title
        if body_text:
            payload["body"] = body_text
        if include_links:
            payload["include_links"] = include_links
        if ai_provider:
            payload["ai_provider"] = ai_provider
        if ai_model:
            payload["ai_model"] = ai_model
        if issue_prompt:
            payload["issue_prompt"] = issue_prompt
        if include_github and github_token:
            payload["github_token"] = github_token
        if include_github and github_repo_url:
            payload["github_repo_url"] = github_repo_url
        if include_jira and jira_token:
            payload["jira_token"] = jira_token
        if include_jira and jira_email:
            payload["jira_email"] = jira_email
        if include_jira and jira_project_key:
            payload["jira_project_key"] = jira_project_key
        if include_jira and jira_security_level:
            payload["jira_security_level"] = jira_security_level
        return self._with_child_scope(payload, child_job_name, child_build_number)

    def get_issue_prompt(self, job_id: str) -> dict:
        """Get issue prompt from test repo. GET /results/{job_id}/issue-prompt"""
        return self._request("GET", f"/results/{job_id}/issue-prompt")

    def preview_github_issue(
        self,
        job_id: str,
        test_name: str,
        child_job_name: str = "",
        child_build_number: int = 0,
        include_links: bool = False,
        ai_provider: str = "",
        ai_model: str = "",
        github_token: str = "",
        github_repo_url: str = "",
        issue_prompt: str = "",
    ) -> dict:
        """Preview a GitHub issue. POST /results/{job_id}/preview-github-issue"""
        body = self._build_tracker_body(
            test_name,
            child_job_name,
            child_build_number,
            include_links=include_links,
            ai_provider=ai_provider,
            ai_model=ai_model,
            github_token=github_token,
            github_repo_url=github_repo_url,
            include_jira=False,
            issue_prompt=issue_prompt,
        )
        return self._request(
            "POST", f"/results/{job_id}/preview-github-issue", json=body
        )

    def preview_jira_bug(
        self,
        job_id: str,
        test_name: str,
        child_job_name: str = "",
        child_build_number: int = 0,
        include_links: bool = False,
        ai_provider: str = "",
        ai_model: str = "",
        jira_token: str = "",
        jira_email: str = "",
        jira_project_key: str = "",
        jira_security_level: str = "",
        issue_prompt: str = "",
    ) -> dict:
        """Preview a Jira bug. POST /results/{job_id}/preview-jira-bug"""
        body = self._build_tracker_body(
            test_name,
            child_job_name,
            child_build_number,
            include_links=include_links,
            ai_provider=ai_provider,
            ai_model=ai_model,
            jira_token=jira_token,
            jira_email=jira_email,
            jira_project_key=jira_project_key,
            jira_security_level=jira_security_level,
            include_github=False,
            issue_prompt=issue_prompt,
        )
        return self._request("POST", f"/results/{job_id}/preview-jira-bug", json=body)

    def create_github_issue(
        self,
        job_id: str,
        test_name: str,
        title: str,
        body: str,
        child_job_name: str = "",
        child_build_number: int = 0,
        github_token: str = "",
        github_repo_url: str = "",
    ) -> dict:
        """Create a GitHub issue. POST /results/{job_id}/create-github-issue"""
        payload = self._build_tracker_body(
            test_name,
            child_job_name,
            child_build_number,
            title=title,
            body_text=body,
            github_token=github_token,
            github_repo_url=github_repo_url,
            include_jira=False,
        )
        return self._request(
            "POST",
            f"/results/{job_id}/create-github-issue",
            json=payload,
            accept_statuses=(201,),
        )

    def create_jira_bug(
        self,
        job_id: str,
        test_name: str,
        title: str,
        body: str,
        child_job_name: str = "",
        child_build_number: int = 0,
        jira_token: str = "",
        jira_email: str = "",
        jira_project_key: str = "",
        jira_security_level: str = "",
        jira_issue_type: str = "Bug",
    ) -> dict:
        """Create a Jira bug. POST /results/{job_id}/create-jira-bug"""
        payload = self._build_tracker_body(
            test_name,
            child_job_name,
            child_build_number,
            title=title,
            body_text=body,
            jira_token=jira_token,
            jira_email=jira_email,
            jira_project_key=jira_project_key,
            jira_security_level=jira_security_level,
            include_github=False,
        )
        if jira_issue_type and jira_issue_type != "Bug":
            payload["jira_issue_type"] = jira_issue_type
        return self._request(
            "POST",
            f"/results/{job_id}/create-jira-bug",
            json=payload,
            accept_statuses=(201,),
        )

    # -- User Tokens ----------------------------------------------------------

    def get_user_tokens(self) -> dict:
        """Get saved user tokens. GET /api/user/tokens"""
        return self._request("GET", "/api/user/tokens")

    def save_user_tokens(
        self, github_token: str = "", jira_email: str = "", jira_token: str = ""
    ) -> dict:
        """Save user tokens. PUT /api/user/tokens"""
        body: dict = {}
        if github_token:
            body["github_token"] = github_token
        if jira_email:
            body["jira_email"] = jira_email
        if jira_token:
            body["jira_token"] = jira_token
        return self._request("PUT", "/api/user/tokens", json=body)

    # -- Token Validation -----------------------------------------------------

    def validate_token(
        self,
        token_type: str,
        token: str,
        email: str = "",
    ) -> dict:
        """Validate a tracker token. POST /api/validate-token"""
        body: dict = {"token_type": token_type, "token": token}
        if email:
            body["email"] = email
        return self._request("POST", "/api/validate-token", json=body)

    # -- Report Portal --------------------------------------------------------

    def push_reportportal(
        self,
        job_id: str,
        *,
        child_job_name: str | None = None,
        child_build_number: int | None = None,
    ) -> dict:
        """Push classifications to Report Portal. POST /results/{job_id}/push-reportportal"""
        params: dict[str, str | int] = {}
        if child_job_name is not None:
            params["child_job_name"] = child_job_name
        if child_build_number is not None:
            params["child_build_number"] = child_build_number
        return self._request(
            "POST", f"/results/{job_id}/push-reportportal", params=params
        )

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self) -> dict:
        """Get server-level automation capabilities (GitHub issues, Jira bugs). GET /api/capabilities"""
        return self._request("GET", "/api/capabilities")

    # -- Jira Projects --------------------------------------------------------

    @staticmethod
    def _with_jira_auth_fields(
        body: dict, jira_token: str = "", jira_email: str = ""
    ) -> dict:
        """Add jira_token/jira_email to *body* when set."""
        if jira_token and jira_token.strip():
            body["jira_token"] = jira_token.strip()
        if jira_email and jira_email.strip():
            body["jira_email"] = jira_email.strip()
        return body

    def jira_projects(
        self, jira_token: str = "", jira_email: str = "", query: str = ""
    ) -> list[dict]:
        """List Jira projects. POST /api/jira-projects"""
        body: dict = {}
        if query:
            body["query"] = query
        body = self._with_jira_auth_fields(body, jira_token, jira_email)
        return self._request("POST", "/api/jira-projects", json=body)

    def jira_security_levels(
        self, project_key: str, jira_token: str = "", jira_email: str = ""
    ) -> list[dict]:
        """List security levels for a Jira project. POST /api/jira-security-levels"""
        body: dict = {"project_key": project_key}
        body = self._with_jira_auth_fields(body, jira_token, jira_email)
        return self._request("POST", "/api/jira-security-levels", json=body)

    # -- Token Usage ----------------------------------------------------------

    def get_token_usage(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        ai_provider: str | None = None,
        ai_model: str | None = None,
        call_type: str | None = None,
        group_by: str | None = None,
    ) -> dict:
        """Get aggregated token usage with filters. GET /api/admin/token-usage"""
        params: dict = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if ai_provider:
            params["ai_provider"] = ai_provider
        if ai_model:
            params["ai_model"] = ai_model
        if call_type:
            params["call_type"] = call_type
        if group_by:
            params["group_by"] = group_by
        return self._request("GET", "/api/admin/token-usage", params=params)

    def get_token_usage_summary(self) -> dict:
        """Get token usage dashboard summary. GET /api/admin/token-usage/summary"""
        return self._request("GET", "/api/admin/token-usage/summary")

    def get_token_usage_for_job(self, job_id: str) -> dict:
        """Get token usage for a specific job. GET /api/admin/token-usage/{job_id}"""
        return self._request("GET", f"/api/admin/token-usage/{job_id}")

    # -- Users ----------------------------------------------------------------

    def get_mentionable_users(self) -> dict:
        """Get list of mentionable usernames. GET /api/users/mentionable"""
        return self._request("GET", "/api/users/mentionable")

    def get_mentions(
        self, limit: int = 50, offset: int = 0, unread_only: bool = False
    ) -> dict:
        """Get comments that mention the current user. GET /api/users/mentions"""
        params: dict = {"limit": limit, "offset": offset}
        if unread_only:
            params["unread_only"] = "true"
        return self._request("GET", "/api/users/mentions", params=params)

    def mark_mentions_read(self, comment_ids: list[int]) -> dict:
        """Mark specific mentions as read. POST /api/users/mentions/read"""
        return self._request(
            "POST", "/api/users/mentions/read", json={"comment_ids": comment_ids}
        )

    def mark_all_mentions_read(self) -> dict:
        """Mark all mentions as read. POST /api/users/mentions/read-all"""
        return self._request("POST", "/api/users/mentions/read-all")

    # -- AI Models ------------------------------------------------------------

    def list_ai_models(self, provider: str = "") -> dict:
        """List available AI models. GET /api/ai-models"""
        params: dict = {}
        if provider:
            params["provider"] = provider
        return self._request("GET", "/api/ai-models", params=params)

    # -- AI Configs -----------------------------------------------------------

    def get_ai_configs(self) -> list[dict]:
        """Get distinct AI provider/model pairs from completed analyses. GET /ai-configs"""
        return self._request("GET", "/ai-configs")

    # -- Classification Override ----------------------------------------------

    def override_classification(
        self,
        job_id: str,
        test_name: str,
        classification: str,
        child_job_name: str = "",
        child_build_number: int = 0,
    ) -> dict:
        """Override classification. PUT /results/{job_id}/override-classification"""
        payload = self._with_child_scope(
            {"test_name": test_name, "classification": classification},
            child_job_name,
            child_build_number,
        )
        return self._request(
            "PUT", f"/results/{job_id}/override-classification", json=payload
        )

    # -- Job Metadata ---------------------------------------------------------

    def list_jobs_metadata(
        self,
        team: str = "",
        tier: str = "",
        version: str = "",
        labels: list[str] | None = None,
    ) -> list[dict]:
        """List job metadata with optional filters. GET /api/jobs/metadata"""
        params: dict = {
            "team": team,
            "tier": tier,
            "version": version,
        }
        if labels:
            params["label"] = labels
        return self._request("GET", "/api/jobs/metadata", params=params)

    def get_job_metadata(self, job_name: str) -> dict:
        """Get metadata for a job. GET /api/jobs/{job_name}/metadata"""
        return self._request("GET", f"/api/jobs/{job_name}/metadata")

    def set_job_metadata(
        self,
        job_name: str,
        *,
        team: str = "",
        tier: str = "",
        version: str = "",
        labels: list[str] | None = None,
    ) -> dict:
        """Set metadata for a job. PUT /api/jobs/{job_name}/metadata"""
        body: dict = {}
        if team:
            body["team"] = team
        if tier:
            body["tier"] = tier
        if version:
            body["version"] = version
        if labels is not None:
            body["labels"] = labels
        return self._request("PUT", f"/api/jobs/{job_name}/metadata", json=body)

    def delete_job_metadata(self, job_name: str) -> dict:
        """Delete metadata for a job. DELETE /api/jobs/{job_name}/metadata"""
        return self._request("DELETE", f"/api/jobs/{job_name}/metadata")

    def bulk_set_metadata(self, items: list[dict]) -> dict:
        """Bulk import job metadata. PUT /api/jobs/metadata/bulk"""
        return self._request("PUT", "/api/jobs/metadata/bulk", json={"items": items})

    def list_metadata_rules(self) -> dict:
        """List configured metadata rules. GET /api/jobs/metadata/rules"""
        return self._request("GET", "/api/jobs/metadata/rules")

    def preview_metadata_rules(self, job_name: str) -> dict:
        """Preview metadata rule match for a job name. POST /api/jobs/metadata/rules/preview"""
        return self._request(
            "POST", "/api/jobs/metadata/rules/preview", json={"job_name": job_name}
        )

    def analyze_comment_intent(
        self,
        comment: str,
        *,
        job_id: str = "",
        ai_provider: str = "",
        ai_model: str = "",
    ) -> dict:
        """Analyze whether a comment suggests a failure is reviewed. POST /api/analyze-comment-intent"""
        payload: dict = {"comment": comment}
        if job_id:
            payload["job_id"] = job_id
        if ai_provider:
            payload["ai_provider"] = ai_provider
        if ai_model:
            payload["ai_model"] = ai_model
        return self._request("POST", "/api/analyze-comment-intent", json=payload)
