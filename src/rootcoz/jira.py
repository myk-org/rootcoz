"""Jira integration for product bug deduplication.

Searches Jira for existing issues that match product bug failures,
then uses AI to determine actual relevance, helping teams avoid
filing duplicate bug reports.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from rootcoz import get_child_logger
from rootcoz.config import Settings, normalize_jira_auth_mode
from rootcoz.issue_matching import filter_issue_matches_with_ai
from rootcoz.models import (
    AnalysisDetail,
    FailureAnalysis,
    JiraMatch,
    ProductBugReport,
)

logger = get_child_logger(__name__)

# JQL reserved characters that need to be stripped from search keywords
_JQL_SPECIAL_CHARS = set(r'"\'{}[]()~^&|!?*%+-:')

# Atlassian scoped API gateway for OAuth 2.0 credential (service
# account) connections
ATLASSIAN_API_GATEWAY = "https://api.atlassian.com/ex/jira"

# OAuth 2.0 token endpoint for "OAuth 2.0 credentials for service accounts"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
# Re-mint this many seconds before a cached access token expires
_SA_TOKEN_REFRESH_MARGIN_SECONDS = 120


@dataclass(frozen=True)
class JiraConnection:
    """Resolved Jira connection parameters — the single source of truth.

    Every Jira call site (client, bug creation, comment enrichment, chat
    tools, health check) resolves its connection through this instead of
    rebuilding the auth/base-URL/api-version logic locally. Three additive
    modes, detected from the configured credentials:

    - ``sa``: Atlassian "OAuth 2.0 credential for service accounts"
      (client ID + secret; a short-lived access token is minted and sent
      as Bearer against the scoped ``api.atlassian.com`` gateway, REST v3)
    - ``cloud``: Cloud basic auth (email + token, REST v3)
    - ``server``: Server/DC Bearer PAT (REST v2)
    """

    mode: str
    # Base URL for REST calls. Empty in SA mode until the cloud ID resolves
    # (``complete_jira_connection``); callers must complete it first.
    api_base: str
    # Site URL for /browse/KEY links — never the scoped gateway URL
    browse_base: str
    # Atlassian site URL for the tenant-info lookup ("" when unknown)
    site_url: str
    auth: tuple[str, str] | None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    api_path: str = "/rest/api/2"
    search_path: str = "/rest/api/2/search"
    # TLS verification for this method's connections — every httpx call,
    # test probe, and token mint made through this connection uses it.
    ssl_verify: bool = True
    # OAuth 2.0 credential (SA mode): client ID + permanent client secret.
    # The secret is NEVER sent to the API gateway — complete_jira_connection
    # exchanges it for a short-lived access token (``Authorization`` header).
    # repr=False keeps both out of accidental logging.
    sa_client_id: str = field(default="", repr=False)
    sa_client_secret: str = field(default="", repr=False)


def _is_atlassian_cloud_host(url: str) -> bool:
    """True when the URL's hostname is atlassian.net or a subdomain of it.

    Hostname-anchored — a Server/DC URL that merely contains the substring
    ``atlassian.net`` (path or lookalike domain) is NOT Atlassian Cloud.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "atlassian.net" or host.endswith(".atlassian.net")


def resolve_jira_connection(settings: Settings) -> JiraConnection:
    """Resolve the Jira connection from settings.

    ``jira_auth_mode`` is REQUIRED: only the selected method's fields are
    used — no prefix sniffing, no precedence chain, no fallback. Unset
    mode resolves to ``"none"`` regardless of which credentials are
    configured (Jira is disabled — see ``Settings.jira_enabled``).

    Each method is fully self-contained — its own URL and TLS
    verification: ``basic`` uses ``jira_cloud_url``/
    ``jira_cloud_ssl_verify``, ``pat`` uses ``jira_server_url``/
    ``jira_server_ssl_verify``, and ``oauth`` derives its API base solely
    from ``atlassian_oauth_cloud_id`` (the scoped api.atlassian.com
    gateway) with ``jira_oauth_ssl_verify`` for minting and gateway calls.

    In ``oauth`` mode the connection carries the OAuth 2.0 credential's
    client ID and secret; the actual ``Authorization`` header is minted
    asynchronously in :func:`complete_jira_connection` (the secret itself
    is never sent to the API gateway).
    """
    mode = normalize_jira_auth_mode(settings.jira_auth_mode)

    sa_secret = ""
    sa_client_id = ""
    token_value = ""
    if mode == "oauth":
        # Both halves of the OAuth 2.0 credential are required — a secret
        # without a client ID cannot mint an access token.
        if settings.jira_oauth_client_secret:
            sa_secret = settings.jira_oauth_client_secret.get_secret_value()
        sa_client_id = (settings.atlassian_oauth_client_id or "").strip()
    elif mode == "basic":
        if settings.jira_api_token:
            token_value = settings.jira_api_token.get_secret_value()
    elif mode == "pat" and settings.jira_pat:
        token_value = settings.jira_pat.get_secret_value()

    if sa_secret and sa_client_id:
        # SA mode: the cloud ID is the sole API locator — REST calls go to
        # the scoped gateway. Browse links must still point at the site;
        # the site URL cannot be derived from the scoped URL, so it comes
        # from the shared JIRA_SITE_URL.
        site = (settings.jira_site_url or "").rstrip("/")
        if not site:
            logger.warning("JIRA_SITE_URL is unset — Jira browse links cannot be built")
        cloud_id = (settings.atlassian_oauth_cloud_id or "").strip()
        api_base = f"{ATLASSIAN_API_GATEWAY}/{cloud_id}" if cloud_id else ""
        return JiraConnection(
            mode="sa",
            api_base=api_base,
            browse_base=site,
            site_url=site,
            auth=None,
            # Authorization is minted in complete_jira_connection — the
            # client secret must never be sent to the API gateway.
            headers={},
            api_path="/rest/api/3",
            search_path="/rest/api/3/search/jql",
            sa_client_id=sa_client_id,
            sa_client_secret=sa_secret,
            ssl_verify=settings.jira_oauth_ssl_verify,
        )

    if mode == "basic":
        # Basic auth keeps v3 regardless of host — Atlassian removed the v2
        # search endpoint, so a Cloud site behind a custom domain or proxy
        # would hard-fail on v2 paths. (A Server/DC instance configured with
        # email+token basic auth keeps its historical pre-connection behavior.)
        url = settings.jira_cloud_url.strip()
        if settings.jira_email and token_value:
            return JiraConnection(
                mode="cloud",
                api_base=url,
                browse_base=url,
                site_url=url,
                auth=(settings.jira_email, token_value),
                headers={},
                api_path="/rest/api/3",
                search_path="/rest/api/3/search/jql",
                ssl_verify=settings.jira_cloud_ssl_verify,
            )
    elif mode == "pat" and token_value:
        # Bearer auth (Server/DC PAT, or a Cloud PAT): v3 only when the
        # hostname is positively Atlassian Cloud — a lookalike or
        # substring-containing Server/DC host must keep v2.
        url = settings.jira_server_url.strip()
        host_is_atlassian_cloud = _is_atlassian_cloud_host(url)
        return JiraConnection(
            mode="server",
            api_base=url,
            browse_base=url,
            site_url=url,
            auth=None,
            headers={"Authorization": f"Bearer {token_value}"},
            api_path="/rest/api/3" if host_is_atlassian_cloud else "/rest/api/2",
            search_path=(
                "/rest/api/3/search/jql"
                if host_is_atlassian_cloud
                else "/rest/api/2/search"
            ),
            ssl_verify=settings.jira_server_ssl_verify,
        )
    # No method selected, or the selected method's fields are missing:
    # no credentials are used — no inference, no fallback.
    return JiraConnection(
        mode="none",
        api_base="",
        browse_base="",
        site_url="",
        auth=None,
        headers={},
    )


def jira_method_url(settings: Settings) -> str:
    """API base URL of the selected Jira auth method ("" when unusable).

    ``oauth`` → the scoped gateway URL derived from the cloud ID;
    ``basic`` → ``jira_cloud_url``; ``pat`` → ``jira_server_url``.
    Empty when no method is selected or the method's locator is unset.
    """
    mode = normalize_jira_auth_mode(settings.jira_auth_mode)
    if mode == "oauth":
        cloud_id = (settings.atlassian_oauth_cloud_id or "").strip()
        return f"{ATLASSIAN_API_GATEWAY}/{cloud_id}" if cloud_id else ""
    if mode == "basic":
        return settings.jira_cloud_url.strip()
    if mode == "pat":
        return settings.jira_server_url.strip()
    return ""


def resolve_jira_connection_from_params(
    jira_cloud_url: str = "",
    jira_server_url: str = "",
    jira_email: str = "",
    jira_token: str = "",
    jira_pat: str = "",
    jira_oauth_client_secret: str = "",
    atlassian_oauth_client_id: str = "",
    atlassian_oauth_cloud_id: str = "",
    jira_site_url: str = "",
    jira_cloud_ssl_verify: bool = True,
    jira_server_ssl_verify: bool = True,
    jira_oauth_ssl_verify: bool = True,
) -> JiraConnection:
    """Resolve a Jira connection from raw credential strings.

    For call sites that receive credentials as plain strings (chat tools,
    user token validation) rather than a :class:`Settings` instance. Uses
    ``model_construct`` so the process environment is never consulted —
    server deployment secrets must not leak into user-scoped resolution.

    These are per-user credentials whose fields the user chose explicitly:
    the supplied field(s) pin the auth method (OAuth 2.0 credential secret
    + client ID → oauth, token+email → basic, bare token/PAT → pat) — the
    same choice the user made in the credentials UI. No fallback across
    methods. A client secret without a client ID cannot mint an access
    token, so it does not select oauth. Each method reads its own URL and
    TLS verification, mirroring :func:`resolve_jira_connection`.
    """
    from pydantic import SecretStr

    email = jira_email.strip()
    token = jira_token.strip()
    pat = jira_pat.strip()
    sa_secret = jira_oauth_client_secret.strip()
    sa_client_id = atlassian_oauth_client_id.strip()
    if sa_secret and sa_client_id:
        auth_mode = "oauth"
    elif email and (token or pat):
        # Email + token is the Cloud basic-auth pair the user entered.
        auth_mode = "basic"
        if not token:
            token = pat
            pat = ""
    elif pat or token:
        auth_mode = "pat"
        if not pat:
            pat = token
            token = ""
    else:
        auth_mode = ""

    settings = Settings.model_construct(
        jira_cloud_url=jira_cloud_url.strip(),
        jira_server_url=jira_server_url.strip(),
        jira_email=email or None,
        jira_api_token=SecretStr(token) if token else None,
        jira_pat=SecretStr(pat) if pat else None,
        jira_oauth_client_secret=SecretStr(sa_secret) if sa_secret else None,
        atlassian_oauth_client_id=sa_client_id or None,
        atlassian_oauth_cloud_id=atlassian_oauth_cloud_id.strip() or None,
        jira_site_url=jira_site_url.strip() or None,
        jira_cloud_ssl_verify=jira_cloud_ssl_verify,
        jira_server_ssl_verify=jira_server_ssl_verify,
        jira_oauth_ssl_verify=jira_oauth_ssl_verify,
        jira_auth_mode=auth_mode,
    )
    return resolve_jira_connection(settings)


def jira_missing_api_base_error(connection: JiraConnection) -> ValueError:
    """Mode-aware error for a connection whose API base could not resolve.

    Shared by every call site that needs a controlled failure when a
    resolved connection has an empty ``api_base`` — the message points at
    the missing field of the connection's OWN method instead of a
    generic (or wrong-method) configuration hint.
    """
    if connection.mode == "sa":
        return ValueError(
            "Jira service account mode could not resolve the Atlassian cloud "
            "ID: set ATLASSIAN_OAUTH_CLOUD_ID or JIRA_SITE_URL"
        )
    if connection.mode == "cloud":
        return ValueError("jira_cloud_url is not configured for the basic auth method")
    if connection.mode == "server":
        return ValueError("jira_server_url is not configured for the pat auth method")
    return ValueError("No Jira auth method selected — choose one in Server Settings")


# Cloud IDs resolved via tenant_info, cached in-process per site URL
_cloud_id_cache: dict[str, str] = {}

# Minted access tokens, cached in-process per OAuth 2.0 credential:
# (client_id, client_secret) -> (access_token, expires_at_monotonic)
_sa_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


class JiraOAuthMintError(RuntimeError):
    """Minting an Atlassian access token from an OAuth 2.0 credential failed.

    The message is safe to surface to users and logs — it never contains
    the client secret.
    """


async def mint_atlassian_access_token(
    client_id: str, client_secret: str, ssl_verify: bool = True
) -> str:
    """Mint a short-lived access token for an OAuth 2.0 credential.

    POSTs ``client_credentials`` to ``https://auth.atlassian.com/oauth/token``
    and caches the result per ``(client_id, client_secret)`` until 120
    seconds before expiry (``time.monotonic`` + ``expires_in``), so repeated
    connections reuse one token. The client secret is never logged.

    Raises:
        JiraOAuthMintError: Non-200 response, malformed payload, or network
            failure — the message carries the HTTP status where available.
    """
    cache_key = (client_id, client_secret)
    now = time.monotonic()
    cached = _sa_token_cache.get(cache_key)
    if cached and cached[1] - _SA_TOKEN_REFRESH_MARGIN_SECONDS > now:
        logger.debug("Reusing cached Atlassian access token (client_id set)")
        return cached[0]
    try:
        async with httpx.AsyncClient(verify=ssl_verify, timeout=15) as client:
            resp = await client.post(
                ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
    except httpx.HTTPError as exc:
        raise JiraOAuthMintError(
            "could not mint Atlassian access token from the OAuth 2.0 "
            f"credential ({type(exc).__name__})"
        ) from exc
    if resp.status_code != 200:
        raise JiraOAuthMintError(
            "could not mint Atlassian access token from the OAuth 2.0 "
            f"credential (HTTP {resp.status_code} — check client ID and secret)"
        )
    try:
        payload = resp.json()
        access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in") or 3600)
        if not access_token:
            raise KeyError("access_token")
    except (ValueError, KeyError, TypeError) as exc:
        raise JiraOAuthMintError(
            "could not mint Atlassian access token from the OAuth 2.0 "
            "credential (unexpected token endpoint response)"
        ) from exc
    _sa_token_cache[cache_key] = (access_token, now + expires_in)
    logger.info(
        "Minted Atlassian access token from OAuth 2.0 credential (expires in %ds)",
        int(expires_in),
    )
    return access_token


async def resolve_jira_cloud_id(site_url: str, ssl_verify: bool = True) -> str | None:
    """Resolve the Atlassian cloud ID anonymously from a site's tenant info.

    ``GET {site}/_edge/tenant_info`` requires no authentication. Only
    successful lookups are cached; failures return None so a transient
    outage is retried on the next call.
    """
    site = (site_url or "").rstrip("/")
    if not site:
        return None
    cached = _cloud_id_cache.get(site)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(verify=ssl_verify, timeout=10) as client:
            resp = await client.get(f"{site}/_edge/tenant_info")
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Cloud ID lookup failed for %s: %s", site, type(exc).__name__)
        return None
    # A successful response may still be malformed (null, array) — treat any
    # non-object payload as unresolvable rather than crashing callers.
    if isinstance(payload, dict):
        cloud_id = payload.get("cloudId")
        if isinstance(cloud_id, str) and cloud_id:
            _cloud_id_cache[site] = cloud_id
            return cloud_id
    logger.warning("Cloud ID lookup for %s returned no usable cloudId", site)
    return None


async def complete_jira_connection(
    connection: JiraConnection,
) -> JiraConnection:
    """Complete an SA connection: resolve the cloud ID and mint the token.

    Idempotent: non-SA connections pass through unchanged. For SA (OAuth
    2.0 credential) connections, first the API base is resolved when the
    cloud ID is unknown (anonymous tenant_info lookup against the site
    URL), then a short-lived access token is minted from the credential
    (module-level cache) and installed as the Bearer header — the client
    secret itself is never sent to the API gateway. Both network calls
    use the connection's own ``ssl_verify`` flag (the oauth method's).

    Returns the input unchanged when the cloud ID cannot be resolved —
    callers surface the configuration error at request time (no token is
    minted for an unusable connection). Minting failures raise
    :class:`JiraOAuthMintError`.
    """
    if connection.mode != "sa":
        return connection
    result = connection
    if not result.api_base and result.site_url:
        cloud_id = await resolve_jira_cloud_id(result.site_url, result.ssl_verify)
        if cloud_id:
            result = replace(result, api_base=f"{ATLASSIAN_API_GATEWAY}/{cloud_id}")
    if result.api_base and result.sa_client_id and result.sa_client_secret:
        access_token = await mint_atlassian_access_token(
            result.sa_client_id, result.sa_client_secret, result.ssl_verify
        )
        result = replace(
            result,
            headers={**result.headers, "Authorization": f"Bearer {access_token}"},
        )
    return result


def _sanitize_jql_keyword(keyword: str) -> str:
    """Strip JQL-reserved characters from a search keyword.

    Args:
        keyword: Raw keyword from AI output.

    Returns:
        Sanitized keyword safe for JQL text search.
    """
    return "".join(c for c in keyword if c not in _JQL_SPECIAL_CHARS).strip()


class JiraClient:
    """HTTP client for Jira REST API.

    Resolves its connection through :func:`resolve_jira_connection` — the
    shared single source of truth for all Jira call sites. Three
    connection methods, selected by the required ``jira_auth_mode``:

    - Atlassian "OAuth 2.0 credential for service accounts" (``oauth``):
      Bearer against the scoped ``api.atlassian.com`` gateway, REST v3.
      The cloud ID is resolved and the access token minted on entry
      (``__aenter__``) when not already done.
    - Cloud (``basic``): basic auth with email + API token, REST v3.
    - Server/DC (``pat``): Bearer PAT, REST v2 (v3 on Atlassian Cloud hosts).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        connection = resolve_jira_connection(settings)
        self._connection = connection
        self._base_url = connection.api_base
        self._browse_base = connection.browse_base
        self._project_key = settings.jira_project_key
        self._max_results = settings.jira_max_results
        self._auth: tuple[str, str] | None = connection.auth
        self._headers: dict[str, str] = connection.headers
        self._search_path = connection.search_path
        self._api_path = connection.api_path

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            headers=self._headers,
            verify=connection.ssl_verify,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        # SA mode may still need its cloud ID resolved (tenant_info lookup)
        # and its access token minted (client_credentials)
        try:
            connection = await complete_jira_connection(self._connection)
        except Exception:
            # __aexit__ never runs when __aenter__ raises — close the client
            # created in __init__ before surfacing the mint error.
            await self._client.aclose()
            raise
        if connection is not self._connection:
            self._connection = connection
            self._base_url = connection.api_base
            self._client.base_url = connection.api_base
            # Minting replaced the Authorization header — sync the client
            self._headers = dict(connection.headers)
            self._client.headers.update(connection.headers)
        if connection.mode == "sa" and not connection.api_base:
            # __aexit__ never runs when __aenter__ raises — close the client
            # created in __init__ before surfacing the configuration error.
            await self._client.aclose()
            raise ValueError(
                "Jira service account mode could not resolve the Atlassian "
                "cloud ID: set ATLASSIAN_OAUTH_CLOUD_ID or JIRA_SITE_URL"
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def list_projects(self, query: str = "") -> list[dict[str, Any]]:
        """List all accessible Jira projects.

        Tries ``/project/search`` first (returns only projects the caller
        can see) and falls back to ``/project`` (requires broader
        permissions).  Paginates ``/project/search`` to fetch all results.
        """
        # /project/search returns paginated results scoped to the caller;
        # /project requires global Browse Projects and may 403 on Cloud.
        paths_to_try = [
            f"{self._api_path}/project/search",
            f"{self._api_path}/project",
        ]

        for path in paths_to_try:
            try:
                if "/project/search" in path:
                    return await self._list_projects_paginated(path, query=query)
                response = await self._client.get(path)
                response.raise_for_status()
                data = response.json()
                projects = data if isinstance(data, list) else data.get("values", [])
                result = [
                    {"key": p["key"], "name": p.get("name", p["key"])}
                    for p in projects
                    if isinstance(p, dict) and "key" in p
                ]
                if query:
                    _q = query.lower()
                    result = [
                        p
                        for p in result
                        if _q in p["key"].lower() or _q in p["name"].lower()
                    ]
                if result:
                    return result
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Failed to fetch Jira projects from %s: %s",
                    path,
                    exc,
                )
        return []

    async def _list_projects_paginated(
        self, path: str, query: str = ""
    ) -> list[dict[str, Any]]:
        """Fetch projects via paginated /project/search endpoint."""
        all_projects: list[dict[str, Any]] = []
        start_at = 0
        page_size = 50

        while True:
            params: dict[str, Any] = {"startAt": start_at, "maxResults": page_size}
            if query:
                params["query"] = query
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            data = response.json()
            values = data if isinstance(data, list) else data.get("values", [])
            for p in values:
                if isinstance(p, dict) and "key" in p:
                    all_projects.append(
                        {"key": p["key"], "name": p.get("name", p["key"])}
                    )
            # Use Jira pagination metadata when available
            if isinstance(data, dict) and "isLast" in data:
                if data["isLast"]:
                    break
                start_at = data.get("startAt", start_at) + data.get(
                    "maxResults", page_size
                )
            elif len(values) < page_size:
                break
            else:
                start_at += page_size

        return all_projects

    async def list_security_levels(self, project_key: str) -> list[dict[str, Any]]:
        """List available security levels for a Jira project."""
        if not project_key:
            return []
        try:
            response = await self._client.get(
                f"{self._api_path}/project/{project_key}/securitylevel"
            )
            response.raise_for_status()
            data = response.json()
            levels = data.get("levels", [])
            return [
                {
                    "id": lv["id"],
                    "name": lv["name"],
                    "description": lv.get("description", ""),
                }
                for lv in levels
                if isinstance(lv, dict) and "id" in lv and "name" in lv
            ]
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Failed to fetch security levels for project %s: %s",
                project_key,
                exc,
            )
            return []

    async def search(self, keywords: list[str]) -> list[dict[str, Any]]:
        """Search Jira for Bug issues matching the given keywords.

        Builds a JQL query using ``summary ~ "keyword"`` clauses joined
        with OR, filtered to ``issuetype = Bug``, optionally scoped to
        a project.

        Args:
            keywords: Search terms to look for in Jira issue summaries.

        Returns:
            List of dicts with key, summary, description, status, priority, url.
        """
        if not keywords:
            return []

        # Build JQL: summary ~ "kw1" OR summary ~ "kw2" ...
        text_clauses = " OR ".join(
            f'summary ~ "{_sanitize_jql_keyword(kw)}"' for kw in keywords
        )
        jql = f"issuetype = Bug AND ({text_clauses})"
        if self._project_key:
            jql = f'project = "{self._project_key}" AND {jql}'

        jql += " ORDER BY updated DESC"

        params = {
            "jql": jql,
            "maxResults": self._max_results,
            "fields": "summary,description,status,priority",
        }

        response = await self._client.get(
            self._search_path,
            params=params,  # type: ignore[arg-type]
        )
        response.raise_for_status()
        data = response.json()

        candidates: list[dict[str, Any]] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})

            # Extract status name (handles both Cloud and Server response shapes)
            status_obj = fields.get("status") or {}
            status_name = (
                status_obj.get("name", "") if isinstance(status_obj, dict) else ""
            )

            # Extract priority name
            priority_obj = fields.get("priority") or {}
            priority_name = (
                priority_obj.get("name", "") if isinstance(priority_obj, dict) else ""
            )

            # Extract description text
            desc = fields.get("description") or ""
            # Cloud API v3 returns description as ADF (Atlassian Document Format)
            if isinstance(desc, dict):
                desc = _extract_text_from_adf(desc)

            issue_key = issue.get("key", "")
            # An SA connection without JIRA_SITE_URL cannot produce a browse
            # link — omit it rather than emitting a relative "/browse/..."
            # URL that downstream tracker detection would misclassify.
            issue_url = (
                f"{self._browse_base}/browse/{issue_key}" if self._browse_base else ""
            )
            candidates.append(
                {
                    "key": issue_key,
                    "summary": fields.get("summary", ""),
                    "description": desc,
                    "status": status_name,
                    "priority": priority_name,
                    "url": issue_url,
                }
            )

        return candidates


def _extract_text_from_adf(adf: dict[str, Any]) -> str:
    """Extract plain text from Atlassian Document Format (ADF).

    Jira Cloud API v3 returns descriptions as ADF JSON.
    This recursively extracts all text nodes.

    Args:
        adf: ADF document dict.

    Returns:
        Plain text content.
    """
    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return " ".join(parts)


async def filter_matches_with_ai(
    bug_title: str,
    bug_description: str,
    candidates: list[dict[str, Any]],
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None = None,
    job_id: str = "",
) -> list[JiraMatch]:
    """Use AI to determine which Jira candidates are relevant to the bug.

    Delegates to the shared :func:`filter_issue_matches_with_ai` and
    converts the results into Jira-specific ``JiraMatch`` objects.

    Args:
        bug_title: The product bug report title.
        bug_description: The product bug report description.
        candidates: List of candidate dicts from Jira search.
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_call_timeout: Timeout in minutes (overrides AI_CALL_TIMEOUT env var).
        job_id: Job identifier for token usage tracking.

    Returns:
        List of JiraMatch objects for relevant candidates only.
    """
    # Ensure candidates have a consistent 'key' field for the shared filter
    evaluations = await filter_issue_matches_with_ai(
        bug_title=bug_title,
        bug_description=bug_description,
        candidates=candidates,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_call_timeout=ai_call_timeout,
        job_id=job_id,
        call_type="jira_filter",
    )

    # Convert evaluations to JiraMatch objects
    candidate_by_key = {c["key"]: c for c in candidates}
    relevant_matches: list[JiraMatch] = []
    for ev in evaluations:
        key = ev["key"]
        if key in candidate_by_key:
            c = candidate_by_key[key]
            relevant_matches.append(
                JiraMatch(
                    key=key,
                    summary=c["summary"],
                    status=c["status"],
                    priority=c["priority"],
                    url=c["url"],
                    score=ev["score"],
                )
            )

    return relevant_matches


def _collect_product_bug_reports(
    failures: Sequence[FailureAnalysis],
) -> list[ProductBugReport]:
    """Collect all ProductBugReport instances from a list of failures.

    Args:
        failures: List of failure analyses to scan.

    Returns:
        List of ProductBugReport objects found.
    """
    reports: list[ProductBugReport] = []
    for failure in failures:
        detail: AnalysisDetail = failure.analysis
        if isinstance(detail.product_bug_report, ProductBugReport):
            reports.append(detail.product_bug_report)
    return reports


async def enrich_with_jira_matches(
    failures: Sequence[FailureAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
) -> None:
    """Search Jira for matching issues and attach results in-place.

    Collects PRODUCT BUG failures, deduplicates by keyword set,
    searches Jira in parallel, uses AI to filter relevant matches,
    and attaches results to each ``ProductBugReport.jira_matches``.

    This function never raises — all errors are logged and swallowed
    so the analysis pipeline is never interrupted.

    Args:
        failures: Failure analyses to enrich (modified in-place).
        settings: Application settings with Jira configuration.
        ai_provider: AI provider for relevance filtering.
        ai_model: AI model for relevance filtering.
        job_id: Job identifier for token usage tracking.
    """
    if not settings.jira_enabled:
        return

    reports = _collect_product_bug_reports(failures)
    if not reports:
        return

    # Deduplicate by keyword set — same keywords = one Jira search
    keyword_to_reports: dict[tuple[str, ...], list[ProductBugReport]] = {}
    for report in reports:
        if not report.jira_search_keywords:
            continue
        key = tuple(sorted(report.jira_search_keywords))
        keyword_to_reports.setdefault(key, []).append(report)

    if not keyword_to_reports:
        logger.debug(
            "No PRODUCT BUG failures with jira_search_keywords, skipping Jira lookup"
        )
        return

    logger.info(
        "Searching Jira for %d unique keyword set(s) across %d PRODUCT BUG failure(s)",
        len(keyword_to_reports),
        len(reports),
    )

    total_matches = 0
    try:
        async with JiraClient(settings) as client:
            # Search Jira for each unique keyword set in parallel
            async def _search_safe(keywords: list[str]) -> list[dict[str, Any]]:
                try:
                    return await client.search(keywords)
                except Exception:
                    logger.exception("Jira search failed for keywords: %s", keywords)
                    return []

            tasks = [_search_safe(list(kw_tuple)) for kw_tuple in keyword_to_reports]
            search_results = await asyncio.gather(*tasks)

            # AI relevance filtering for each keyword set
            for kw_tuple, candidates in zip(
                keyword_to_reports, search_results, strict=False
            ):
                if not candidates:
                    continue

                # Use the first report's title/description as context for AI filtering
                representative = keyword_to_reports[kw_tuple][0]

                if ai_provider and ai_model:
                    matches = await filter_matches_with_ai(
                        bug_title=representative.title,
                        bug_description=representative.description,
                        candidates=candidates,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        ai_call_timeout=settings.ai_call_timeout,
                        job_id=job_id,
                    )
                else:
                    # No AI config — fall back to returning all candidates as matches
                    logger.debug(
                        "No AI provider configured for Jira relevance filtering, returning all candidates"
                    )
                    matches = [
                        JiraMatch(
                            key=c["key"],
                            summary=c["summary"],
                            status=c["status"],
                            priority=c["priority"],
                            url=c["url"],
                            score=0.0,
                        )
                        for c in candidates
                    ]

                # Attach matches to all reports sharing the same keyword set
                for report in keyword_to_reports[kw_tuple]:
                    report.jira_matches = matches

                total_matches += len(matches)
            logger.info(
                "Jira search complete: %d relevant match(es) found", total_matches
            )
    except Exception:
        logger.exception("Jira enrichment failed unexpectedly")
