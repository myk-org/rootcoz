"""jji -- CLI tool for the jenkins-job-insight REST API."""

import typer

from jenkins_job_insight.cli.client import JJIClient, JJIError
from jenkins_job_insight.config import parse_additional_repos, parse_peer_configs
from jenkins_job_insight.cli.config import (
    CONFIG_FILE,
    ServerConfig,
    get_default_server_name,
    get_server_config,
    list_servers,
    load_config,
)
from jenkins_job_insight.cli.output import print_output

# -- App and sub-command groups -----------------------------------------------

app = typer.Typer(
    name="jji",
    help="CLI for the jenkins-job-insight REST API.",
    no_args_is_help=True,
)

results_app = typer.Typer(help="Manage analysis results.", no_args_is_help=True)
history_app = typer.Typer(help="Query failure history.", no_args_is_help=True)
comments_app = typer.Typer(
    help="Manage comments on test failures.", no_args_is_help=True
)
classifications_app = typer.Typer(
    help="List test classifications.", no_args_is_help=True
)
config_app = typer.Typer(help="Manage JJI configuration.")
auth_app = typer.Typer(help="Authentication commands.", no_args_is_help=True)
admin_app = typer.Typer(help="Admin management commands.", no_args_is_help=True)
admin_users_app = typer.Typer(help="Manage admin users.", no_args_is_help=True)

metadata_app = typer.Typer(help="Manage job metadata.", no_args_is_help=True)

app.add_typer(results_app, name="results")
app.add_typer(history_app, name="history")
app.add_typer(comments_app, name="comments")
app.add_typer(classifications_app, name="classifications")
app.add_typer(metadata_app, name="metadata")
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")
app.add_typer(admin_app, name="admin")
admin_app.add_typer(admin_users_app, name="users")

# -- Global state managed via app callback ------------------------------------

_state: dict = {}

# Shared option definition reused across leaf commands so --json works
# both globally (before the subcommand) and per-command (after it).
_JSON_OPTION = typer.Option(False, "--json", help="Output as JSON instead of table.")
_JOB_IDS_ARGUMENT = typer.Argument(default=None, help="Job ID(s) to delete.")
_BULK_DELETE_BATCH_SIZE = 500


def _set_json(json_output: bool) -> None:
    """Set JSON mode from a per-command --json flag."""
    if json_output:
        _state["json"] = True


def _get_client(server_url: str = "", username: str = "") -> JJIClient:
    """Build (or return cached) JJIClient from global state."""
    url = server_url or _state.get("server_url", "")
    uname = username or _state.get("username", "")
    verify_ssl = not _state.get("no_verify_ssl", False)
    api_key = _state.get("api_key", "")
    return JJIClient(
        server_url=url, username=uname, verify_ssl=verify_ssl, api_key=api_key
    )


def _handle_error(err: JJIError) -> None:
    """Print a JJIError and exit with code 1."""
    typer.echo(f"Error: {err}", err=True)
    if err.status_code == 401:
        typer.echo(
            "Hint: Use --api-key or set JJI_API_KEY to authenticate as admin.",
            err=True,
        )
    elif err.status_code == 403:
        detail = err.detail.lower() if err.detail else ""
        if "allow list" in detail:
            typer.echo(
                "Hint: Your user is not on the server's allow list. Contact an administrator.",
                err=True,
            )
        else:
            typer.echo(
                "Hint: This action requires admin access. Use --api-key or set JJI_API_KEY.",
                err=True,
            )
    raise typer.Exit(code=1)


def _run_client_command(
    json_output: bool,
    request_fn,
    *,
    columns: list[str] | None = None,
    labels: dict[str, str] | None = None,
    emit_output: bool = True,
):
    """Execute a client request with standard json/table scaffolding.

    Handles ``_set_json``, ``_get_client``, ``JJIError`` handling, and output
    formatting.  Returns the response data for callers that need post-processing.

    When *emit_output* is ``False``, JSON output is still emitted (``--json``
    mode) but human-readable table output is suppressed so callers can print
    bespoke messages instead.
    """
    _set_json(json_output)
    try:
        data = request_fn(_get_client())
    except JJIError as err:
        _handle_error(err)
    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    elif emit_output:
        print_output(data, columns=columns or [], labels=labels, as_json=False)
    return data


def _resolve_jira_cli_auth(
    jira_token: str,
    jira_email: str,
) -> tuple[str, str]:
    """Resolve Jira token and email from CLI flags with config fallback.

    Args:
        jira_token: Value from --jira-token flag (may be empty).
        jira_email: Value from --jira-email flag (may be empty).

    Returns:
        (jira_token, jira_email) with config defaults applied.
    """
    cfg = _state.get("server_config")
    return (
        jira_token.strip() or ((cfg.jira_token or "").strip() if cfg else ""),
        jira_email.strip() or ((cfg.jira_email or "").strip() if cfg else ""),
    )


def _resolve_server(
    server: str | None,
    username: str,
    no_verify_ssl: bool | None,
) -> tuple[str, str, bool, ServerConfig | None]:
    """Resolve server URL, username, and SSL setting from CLI/env/config.

    Priority (highest to lowest):
      1. CLI flags / environment variables
      2. Config file ($XDG_CONFIG_HOME/jji/config.toml)

    Args:
        server: Value from --server / JJI_SERVER (may be a URL or
            a config server name, or empty).
        username: Value from --user / JJI_USERNAME.
        no_verify_ssl: Value from --no-verify-ssl / JJI_NO_VERIFY_SSL.
            None means "inherit from config profile".

    Returns:
        (server_url, username, no_verify_ssl, server_config) with config
        defaults applied where CLI/env did not provide a value.
        server_config is the resolved ServerConfig (or None).

    Raises:
        typer.Exit: If no server can be determined.
    """
    if server and (server.startswith("http://") or server.startswith("https://")):
        # Explicit URL -- treat as self-contained; do not inherit profile config.
        return server, username, no_verify_ssl or False, None

    if server:
        # Treat as a config server name.
        cfg = get_server_config(server)
        if not cfg:
            available = ", ".join(list_servers().keys()) or "(none)"
            typer.echo(
                f"Error: Server '{server}' not found in config. "
                f"Available servers: {available}",
                err=True,
            )
            raise typer.Exit(1)
        if not username:
            username = cfg.username
        if no_verify_ssl is None:
            no_verify_ssl = cfg.no_verify_ssl
        return cfg.url, username, no_verify_ssl, cfg

    # No --server / env -- try default from config.
    cfg = get_server_config()
    if cfg:
        if not username:
            username = cfg.username
        if no_verify_ssl is None:
            no_verify_ssl = cfg.no_verify_ssl
        return cfg.url, username, no_verify_ssl, cfg

    typer.echo(
        "Error: No server specified. Use --server, set JJI_SERVER, "
        f"or configure {CONFIG_FILE}",
        err=True,
    )
    raise typer.Exit(1)


@app.callback()
def main_callback(
    ctx: typer.Context,
    server: str = typer.Option(
        None,
        "--server",
        "-s",
        envvar="JJI_SERVER",
        help="Server name from config or URL (required unless configured in config).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output as JSON instead of table.",
    ),
    username: str = typer.Option(
        "",
        "--user",
        envvar="JJI_USERNAME",
        help="Username displayed in comments and reviews.",
    ),
    api_key: str = typer.Option(
        "",
        "--api-key",
        envvar="JJI_API_KEY",
        help="Admin API key for Bearer token authentication.",
    ),
    no_verify_ssl: bool | None = typer.Option(
        None,
        "--no-verify-ssl",
        envvar="JJI_NO_VERIFY_SSL",
        help="Disable SSL certificate verification for HTTPS connections.",
    ),
    verify_ssl: bool | None = typer.Option(
        None,
        "--verify-ssl",
        help="Force SSL certificate verification on (overrides config profile).",
    ),
    insecure: bool = typer.Option(
        False,
        "--insecure",
        help="Alias for --no-verify-ssl.",
    ),
):
    """jji -- CLI for the jenkins-job-insight REST API."""
    _state["json"] = json_output

    # Merge --insecure / --verify-ssl into no_verify_ssl.
    # --verify-ssl explicitly forces verification on (no_verify_ssl=False).
    # --insecure unconditionally disables verification, overriding env/config.
    if verify_ssl and insecure:
        typer.echo(
            "Error: --verify-ssl and --insecure are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(1)
    if verify_ssl:
        no_verify_ssl = False
    elif insecure:
        no_verify_ssl = True

    # Config subcommands do not require a server connection.
    if ctx.invoked_subcommand == "config":
        return

    server_url, username, no_verify_ssl, cfg = _resolve_server(
        server, username, no_verify_ssl
    )

    _state["server_url"] = server_url
    _state["username"] = username
    _state["no_verify_ssl"] = no_verify_ssl
    _state["server_config"] = cfg

    if api_key:
        _state["api_key"] = api_key
    elif cfg and cfg.api_key:
        _state["api_key"] = cfg.api_key
    else:
        _state["api_key"] = ""


# -- Health -------------------------------------------------------------------


@app.command()
def health(
    json_output: bool = _JSON_OPTION,
):
    """Check server health."""
    data = _run_client_command(json_output, lambda c: c.health(), emit_output=False)
    if not _state.get("json", False):
        status = data.get("status", "unknown")
        typer.echo(f"Status: {status}")
        checks = data.get("checks", {})
        if checks:
            typer.echo("\nChecks:")
            for name, check in checks.items():
                detail = check.get("detail", "")
                suffix = f" ({detail})" if detail else ""
                typer.echo(f"  {name}: {check.get('status', 'unknown')}{suffix}")
        error_rates = data.get("error_rates", {})
        if error_rates:
            typer.echo(
                f"\nError rate: {error_rates.get('error_rate', 0):.1%} "
                f"({error_rates.get('total_errors', 0)}/"
                f"{error_rates.get('total_requests', 0)} in "
                f"{error_rates.get('window_seconds', 0):.0f}s window)"
            )


# -- Results ------------------------------------------------------------------


@results_app.command("list")
def results_list(
    limit: int = typer.Option(50, "--limit", "-l", help="Max results to return."),
    json_output: bool = _JSON_OPTION,
):
    """List recent analyzed jobs."""
    _run_client_command(
        json_output,
        lambda c: c.list_results(limit=limit),
        columns=["job_id", "status", "jenkins_url", "created_at"],
        labels={
            "job_id": "JOB ID",
            "jenkins_url": "JENKINS URL",
            "created_at": "CREATED",
        },
    )


@results_app.command("dashboard")
def dashboard(
    json_output: bool = _JSON_OPTION,
):
    """List analysis jobs with dashboard metadata (failure counts, review progress)."""
    _run_client_command(
        json_output,
        lambda c: c.dashboard(),
        columns=[
            "job_id",
            "job_name",
            "build_number",
            "status",
            "failure_count",
            "reviewed_count",
            "comment_count",
            "created_at",
        ],
    )


@results_app.command("show")
def results_show(
    job_id: str = typer.Argument(help="Job ID to show."),
    full: bool = typer.Option(False, "--full", "-f", help="Show complete JSON result."),
    json_output: bool = _JSON_OPTION,
):
    """Show analysis result for a job."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_result(job_id)
    except JJIError as err:
        _handle_error(err)

    if full or _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        summary = {
            "job_id": data.get("job_id", ""),
            "status": data.get("status", ""),
            "jenkins_url": data.get("jenkins_url", ""),
            "created_at": data.get("created_at", ""),
        }
        result = data.get("result")
        if isinstance(result, dict):
            summary["summary"] = result.get("summary", "")
            summary["ai_provider"] = result.get("ai_provider", "")
            summary["failures"] = len(result.get("failures", []))
            summary["children"] = len(result.get("child_job_analyses", []))
        print_output(
            summary,
            columns=[
                "job_id",
                "status",
                "summary",
                "failures",
                "children",
                "ai_provider",
                "created_at",
            ],
            labels={"ai_provider": "AI PROVIDER", "created_at": "CREATED"},
            as_json=False,
        )


@results_app.command("delete")
def results_delete(
    job_ids: list[str] | None = _JOB_IDS_ARGUMENT,
    all_jobs: bool = typer.Option(False, "--all", help="Delete all jobs."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Confirm deleting all jobs (required with --all)."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Delete one or more jobs and all related data."""
    _set_json(json_output)
    try:
        client = _get_client()
        if all_jobs and job_ids:
            typer.echo(
                "Error: --all cannot be combined with explicit JOB_ID values.", err=True
            )
            raise typer.Exit(code=1)
        if all_jobs:
            if not confirm:
                typer.echo("Error: --all requires --confirm.", err=True)
                raise typer.Exit(code=1)
            # Fetch all job IDs from the dashboard
            dashboard_jobs = client.dashboard()
            job_ids = [j["job_id"] for j in dashboard_jobs]
            if not job_ids:
                if _state.get("json", False):
                    print_output(
                        {"deleted": [], "failed": [], "total": 0},
                        columns=[],
                        as_json=True,
                    )
                else:
                    typer.echo("No jobs to delete.")
                raise typer.Exit()
        elif not job_ids:
            typer.echo("Error: provide at least one JOB_ID or use --all.", err=True)
            raise typer.Exit(code=1)

        job_ids = list(dict.fromkeys(job_ids))

        if len(job_ids) == 1:
            data = client.delete_job(job_ids[0])
            if _state.get("json", False):
                print_output(data, columns=[], as_json=True)
            else:
                typer.echo(f"Deleted job {data.get('job_id', job_ids[0])}")
        else:
            deleted: list[str] = []
            failed: list[dict] = []
            for start in range(0, len(job_ids), _BULK_DELETE_BATCH_SIZE):
                chunk = job_ids[start : start + _BULK_DELETE_BATCH_SIZE]
                try:
                    chunk_data = client.delete_jobs_bulk(chunk)
                    deleted.extend(chunk_data.get("deleted", []))
                    failed.extend(chunk_data.get("failed", []))
                except JJIError as err:
                    if err.status_code in (401, 403):
                        raise
                    failed.extend(
                        {"job_id": jid, "reason": "batch request failed"}
                        for jid in chunk
                    )
            data = {"deleted": deleted, "failed": failed, "total": len(job_ids)}
            if _state.get("json", False):
                print_output(data, columns=[], as_json=True)
            else:
                typer.echo(
                    f"Deleted {len(deleted)} of {data.get('total', len(job_ids))} jobs"
                )
                for f in failed:
                    typer.echo(f"  Failed: {f['job_id']} - {f['reason']}", err=True)
    except JJIError as err:
        _handle_error(err)


# -- Review -------------------------------------------------------------------


@results_app.command("review-status")
def review_status(
    job_id: str = typer.Argument(help="Job ID."),
    json_output: bool = _JSON_OPTION,
):
    """Show review status for an analysis."""
    _run_client_command(
        json_output,
        lambda c: c.get_review_status(job_id),
        columns=["total_failures", "reviewed_count", "comment_count"],
    )


@results_app.command("set-reviewed")
def set_reviewed_cmd(
    job_id: str = typer.Argument(help="Job ID."),
    test_name: str = typer.Option(..., "--test", "-t", help="Test name."),
    reviewed: bool = typer.Option(
        ..., "--reviewed/--not-reviewed", help="Mark as reviewed or not."
    ),
    child_job_name: str = typer.Option("", "--child-job"),
    child_build_number: int = typer.Option(0, "--child-build"),
    json_output: bool = _JSON_OPTION,
):
    """Set or clear the reviewed state for a test failure."""
    if child_build_number < 0:
        typer.echo("Error: --child-build must be non-negative.", err=True)
        raise typer.Exit(1)
    if child_build_number > 0 and not child_job_name:
        typer.echo("Error: --child-build requires --child-job.", err=True)
        raise typer.Exit(1)

    data = _run_client_command(
        json_output,
        lambda c: c.set_reviewed(
            job_id=job_id,
            test_name=test_name,
            reviewed=reviewed,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
        ),
        emit_output=False,
    )
    if not _state.get("json", False):
        state = "reviewed" if reviewed else "not reviewed"
        reviewer = data.get("reviewed_by", "")
        msg = f"Marked as {state}"
        if reviewer:
            msg += f" (by {reviewer})"
        typer.echo(msg)


@results_app.command("enrich-comments")
def enrich_comments_cmd(
    job_id: str = typer.Argument(help="Job ID."),
    json_output: bool = _JSON_OPTION,
):
    """Enrich comments with live PR/ticket statuses."""
    data = _run_client_command(
        json_output,
        lambda c: c.enrich_comments(job_id),
        emit_output=False,
    )
    if not _state.get("json", False):
        enriched = data.get("enriched", 0)
        typer.echo(f"Enriched {enriched} comment(s).")


# -- Analyze ------------------------------------------------------------------


@app.command()
def analyze(
    job_name: str = typer.Option(..., "--job-name", "-j", help="Jenkins job name."),
    build_number: int = typer.Option(
        ..., "--build-number", "-b", help="Build number to analyze."
    ),
    provider: str = typer.Option(
        "", "--provider", help="AI provider (e.g. claude, gemini, cursor)."
    ),
    model: str = typer.Option("", "--model", help="AI model to use."),
    jira: bool | None = typer.Option(
        None, "--jira/--no-jira", help="Enable/disable Jira integration."
    ),
    jenkins_url: str = typer.Option(
        "", "--jenkins-url", envvar="JENKINS_URL", help="Jenkins server URL."
    ),
    jenkins_user: str = typer.Option(
        "", "--jenkins-user", envvar="JENKINS_USER", help="Jenkins username."
    ),
    jenkins_password: str = typer.Option(
        "",
        "--jenkins-password",
        envvar="JENKINS_PASSWORD",
        help="Jenkins password or API token.",
    ),
    jenkins_ssl_verify: bool | None = typer.Option(
        None,
        "--jenkins-ssl-verify/--no-jenkins-ssl-verify",
        help="Jenkins SSL certificate verification.",
    ),
    jenkins_timeout: int | None = typer.Option(
        None,
        "--jenkins-timeout",
        help="Jenkins API request timeout in seconds.",
    ),
    jenkins_artifacts_max_size_mb: int = typer.Option(
        None,
        "--jenkins-artifacts-max-size-mb",
        help="Maximum Jenkins artifacts size in MB.",
    ),
    get_job_artifacts: bool | None = typer.Option(
        None,
        "--get-job-artifacts/--no-get-job-artifacts",
        help="Download all build artifacts for AI context.",
    ),
    tests_repo_url: str = typer.Option(
        "", "--tests-repo-url", envvar="TESTS_REPO_URL", help="Tests repository URL."
    ),
    tests_repo_token: str = typer.Option(
        "",
        "--tests-repo-token",
        envvar="TESTS_REPO_TOKEN",
        help="Token for cloning private tests repository.",
    ),
    jira_url: str = typer.Option(
        "", "--jira-url", envvar="JIRA_URL", help="Jira instance URL."
    ),
    jira_email: str = typer.Option(
        "", "--jira-email", envvar="JIRA_EMAIL", help="Jira Cloud email."
    ),
    jira_api_token: str = typer.Option(
        "", "--jira-api-token", envvar="JIRA_API_TOKEN", help="Jira Cloud API token."
    ),
    jira_pat: str = typer.Option(
        "",
        "--jira-pat",
        envvar="JIRA_PAT",
        help="Jira Server/DC personal access token.",
    ),
    jira_project_key: str = typer.Option(
        "",
        "--jira-project-key",
        envvar="JIRA_PROJECT_KEY",
        help="Jira project key to scope searches.",
    ),
    jira_ssl_verify: bool | None = typer.Option(
        None,
        "--jira-ssl-verify/--no-jira-ssl-verify",
        help="Jira SSL certificate verification.",
    ),
    jira_max_results: int = typer.Option(
        None, "--jira-max-results", help="Max Jira search results."
    ),
    github_token: str = typer.Option(
        "", "--github-token", envvar="GITHUB_TOKEN", help="GitHub API token."
    ),
    ai_cli_timeout: int = typer.Option(
        None, "--ai-cli-timeout", help="AI CLI timeout in minutes."
    ),
    raw_prompt: str = typer.Option(
        "", "--raw-prompt", help="Raw prompt to append as additional AI instructions."
    ),
    peers: str = typer.Option(
        "",
        "--peers",
        help='Peer AI configs as "provider:model,provider:model" (e.g. "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro").',
    ),
    peer_analysis_max_rounds: int | None = typer.Option(
        None,
        "--peer-analysis-max-rounds",
        help="Maximum debate rounds (1-10, default: 3).",
    ),
    additional_repos: str = typer.Option(
        "",
        "--additional-repos",
        help='Additional repos for AI context as "name:url,name:url" (e.g. "infra:https://github.com/org/infra,product:https://github.com/org/product").',
    ),
    wait_for_completion: bool | None = typer.Option(
        None,
        "--wait/--no-wait",
        help="Wait for Jenkins job to complete before analyzing.",
    ),
    poll_interval: int = typer.Option(
        None, "--poll-interval", help="Minutes between Jenkins polls."
    ),
    max_wait: int = typer.Option(
        None, "--max-wait", help="Maximum minutes to wait for completion."
    ),
    force: bool | None = typer.Option(
        None,
        "--force/--no-force",
        help="Force analysis even if the build succeeded.",
    ),
    max_concurrent: int = typer.Option(
        0,
        "--max-concurrent",
        help="Max concurrent AI CLI calls (0 = no CLI override; config or server default will be used).",
    ),
    json_output: bool = _JSON_OPTION,
):
    """Submit a Jenkins job for analysis."""
    _set_json(json_output)

    _positive_int_fields = {
        "--build-number": build_number,
        "--poll-interval": poll_interval,
        "--jira-max-results": jira_max_results,
        "--ai-cli-timeout": ai_cli_timeout,
        "--jenkins-artifacts-max-size-mb": jenkins_artifacts_max_size_mb,
        "--jenkins-timeout": jenkins_timeout,
    }
    for flag_name, flag_value in _positive_int_fields.items():
        if flag_value is not None and flag_value <= 0:
            typer.echo(f"Error: {flag_name} must be greater than 0.", err=True)
            raise typer.Exit(1)
    if max_wait is not None and max_wait < 0:
        typer.echo("Error: --max-wait must be non-negative.", err=True)
        raise typer.Exit(1)
    if max_concurrent < 0:
        typer.echo("Error: --max-concurrent must be non-negative.", err=True)
        raise typer.Exit(1)

    # Start from config defaults (lowest priority), then overlay CLI flags.
    extras: dict = {}
    cfg = _state.get("server_config")
    if cfg:
        # String fields from config -- only set if non-empty.
        _cfg_str_fields = {
            "jenkins_url": cfg.jenkins_url,
            "jenkins_user": cfg.jenkins_user,
            "jenkins_password": cfg.jenkins_password,
            "tests_repo_url": cfg.tests_repo_url,
            "tests_repo_token": cfg.tests_repo_token,
            "jira_url": cfg.jira_url,
            "jira_email": cfg.jira_email,
            "jira_api_token": cfg.jira_api_token,
            "jira_pat": cfg.jira_pat,
            "jira_project_key": cfg.jira_project_key,
            "github_token": cfg.github_token,
            "ai_provider": cfg.ai_provider,
            "ai_model": cfg.ai_model,
        }
        for key, value in _cfg_str_fields.items():
            if value:
                extras[key] = value

        # Integer fields from config -- only forward values that differ from
        # the dataclass default (0 = "use server default" for all these fields).
        _cfg_int_fields = {
            "ai_cli_timeout": cfg.ai_cli_timeout,
            "max_concurrent_ai_calls": cfg.max_concurrent_ai_calls,
            "jira_max_results": cfg.jira_max_results,
            "jenkins_timeout": cfg.jenkins_timeout,
            "poll_interval_minutes": cfg.poll_interval_minutes,
            "max_wait_minutes": cfg.max_wait_minutes,
        }
        _cfg_int_defaults = {
            "ai_cli_timeout": ServerConfig.ai_cli_timeout,
            "max_concurrent_ai_calls": ServerConfig.max_concurrent_ai_calls,
            "jira_max_results": ServerConfig.jira_max_results,
            "jenkins_timeout": ServerConfig.jenkins_timeout,
            "poll_interval_minutes": ServerConfig.poll_interval_minutes,
            "max_wait_minutes": ServerConfig.max_wait_minutes,
        }
        for key, value in _cfg_int_fields.items():
            if value == _cfg_int_defaults[key]:
                continue
            if key == "max_wait_minutes":
                if value < 0:
                    typer.echo(f"Error: config {key} must be non-negative.", err=True)
                    raise typer.Exit(1)
            elif value <= 0:
                typer.echo(f"Error: config {key} must be greater than 0.", err=True)
                raise typer.Exit(1)
            extras[key] = value

        # Boolean fields from config -- forward when they differ from the
        # dataclass default so that explicit ``enable_jira = false`` in the
        # config is not silently dropped.
        if cfg.enable_jira is not None:
            extras["enable_jira"] = cfg.enable_jira
        if cfg.jenkins_ssl_verify is not None:
            extras["jenkins_ssl_verify"] = cfg.jenkins_ssl_verify
        if cfg.jira_ssl_verify is not None:
            extras["jira_ssl_verify"] = cfg.jira_ssl_verify
        if cfg.wait_for_completion is not None:
            extras["wait_for_completion"] = cfg.wait_for_completion
        if cfg.force is not None:
            extras["force"] = cfg.force

    # CLI flags override config (highest priority).
    if provider:
        extras["ai_provider"] = provider
    if model:
        extras["ai_model"] = model
    if jira is not None:
        extras["enable_jira"] = jira

    # String options: include only if non-empty.
    _str_fields = {
        "jenkins_url": jenkins_url,
        "jenkins_user": jenkins_user,
        "jenkins_password": jenkins_password,
        "tests_repo_url": tests_repo_url,
        "tests_repo_token": tests_repo_token,
        "jira_url": jira_url,
        "jira_email": jira_email,
        "jira_api_token": jira_api_token,
        "jira_pat": jira_pat,
        "jira_project_key": jira_project_key,
        "github_token": github_token,
        "raw_prompt": raw_prompt,
    }
    for key, value in _str_fields.items():
        if value:
            extras[key] = value

    # Integer options: include only if provided.
    _int_fields = {
        "jira_max_results": jira_max_results,
        "ai_cli_timeout": ai_cli_timeout,
        "jenkins_artifacts_max_size_mb": jenkins_artifacts_max_size_mb,
        "jenkins_timeout": jenkins_timeout,
        "poll_interval_minutes": poll_interval,
        "max_wait_minutes": max_wait,
    }
    for key, value in _int_fields.items():
        if value is not None:
            extras[key] = value

    # max_concurrent: 0 means "not set" (use server default).
    if max_concurrent > 0:
        extras["max_concurrent_ai_calls"] = max_concurrent

    # Boolean options: include only if explicitly set (not None).
    _bool_fields = {
        "jenkins_ssl_verify": jenkins_ssl_verify,
        "jira_ssl_verify": jira_ssl_verify,
        "get_job_artifacts": get_job_artifacts,
        "wait_for_completion": wait_for_completion,
        "force": force,
    }
    for key, value in _bool_fields.items():
        if value is not None:
            extras[key] = value

    # Peer analysis: CLI flag overrides config, parse into list of dicts.
    peers_raw = (peers.strip() if peers else "") or (cfg.peers if cfg else "")
    if peers_raw and peers_raw.strip():
        try:
            extras["peer_ai_configs"] = parse_peer_configs(peers_raw)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None
    if peer_analysis_max_rounds is not None:
        if not 1 <= peer_analysis_max_rounds <= 10:
            typer.echo(
                "Error: --peer-analysis-max-rounds must be between 1 and 10.", err=True
            )
            raise typer.Exit(1) from None
        extras["peer_analysis_max_rounds"] = peer_analysis_max_rounds
    elif cfg and cfg.peer_analysis_max_rounds:
        if not 1 <= cfg.peer_analysis_max_rounds <= 10:
            typer.echo(
                "Error: config peer_analysis_max_rounds must be between 1 and 10.",
                err=True,
            )
            raise typer.Exit(1) from None
        extras["peer_analysis_max_rounds"] = cfg.peer_analysis_max_rounds

    # Additional repos: CLI flag overrides config, parse into list of dicts.
    additional_repos_raw = (additional_repos.strip() if additional_repos else "") or (
        cfg.additional_repos if cfg else ""
    )
    if additional_repos_raw and additional_repos_raw.strip():
        try:
            extras["additional_repos"] = parse_additional_repos(additional_repos_raw)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None

    try:
        client = _get_client()
        data = client.analyze(job_name, build_number, **extras)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Job queued: {data.get('job_id', '')}")
        typer.echo(f"Status: {data.get('status', '')}")
        typer.echo(f"Poll: {data.get('result_url', '')}")


@app.command("re-analyze")
def re_analyze_cmd(
    job_id: str = typer.Argument(help="Job ID of the analysis to re-run."),
    json_output: bool = _JSON_OPTION,
):
    """Re-analyze a previously analyzed job with the same settings."""
    data = _run_client_command(
        json_output,
        lambda c: c.re_analyze(job_id),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Re-analysis queued: {data.get('job_id', '')}")
        typer.echo(f"Status: {data.get('status', '')}")
        typer.echo(f"Poll: {data.get('result_url', '')}")


# -- Status -------------------------------------------------------------------


@app.command()
def status(
    job_id: str = typer.Argument(help="Job ID to check."),
    json_output: bool = _JSON_OPTION,
):
    """Check analysis status for a job."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_result(job_id)
    except JJIError as err:
        _handle_error(err)
    if _state.get("json"):
        print_output(data, columns=[], as_json=True)
    else:
        print_output(
            {"job_id": data.get("job_id", ""), "status": data.get("status", "")},
            columns=["job_id", "status"],
            as_json=False,
        )


# -- History ------------------------------------------------------------------


@history_app.command("test")
def history_test(
    test_name: str = typer.Argument(help="Fully qualified test name."),
    limit: int = typer.Option(20, "--limit", "-l"),
    job_name: str = typer.Option("", "--job-name", "-j"),
    exclude_job_id: str = typer.Option(
        "", "--exclude-job-id", help="Exclude results from this job ID."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Show failure history for a specific test."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_test_history(
            test_name, limit=limit, job_name=job_name, exclude_job_id=exclude_job_id
        )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        failure_rate = data.get("failure_rate")
        rate_str = f"{failure_rate:.1%}" if failure_rate is not None else "N/A"
        typer.echo(f"Test: {data.get('test_name', '')}")
        typer.echo(f"Failures: {data.get('failures', 0)}")
        typer.echo(f"Failure rate: {rate_str}")
        typer.echo(f"Last classification: {data.get('last_classification', 'N/A')}")
        runs = data.get("recent_runs", [])
        if runs:
            typer.echo("\nRecent runs:")
            print_output(
                runs,
                columns=["job_name", "build_number", "classification", "analyzed_at"],
                labels={"build_number": "BUILD", "analyzed_at": "DATE"},
                as_json=False,
            )
        comments = data.get("comments", [])
        if comments:
            typer.echo("\nComments:")
            print_output(
                comments,
                columns=["username", "comment", "created_at"],
                labels={"created_at": "DATE"},
                as_json=False,
            )


@history_app.command("search")
def history_search(
    signature: str = typer.Option(
        ..., "--signature", "-s", help="Error signature hash."
    ),
    exclude_job_id: str = typer.Option(
        "", "--exclude-job-id", help="Exclude results from this job ID."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Find tests that failed with the same error signature."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.search_by_signature(signature, exclude_job_id=exclude_job_id)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Signature: {data.get('signature', '')}")
        typer.echo(f"Total occurrences: {data.get('total_occurrences', 0)}")
        typer.echo(f"Unique tests: {data.get('unique_tests', 0)}")
        tests = data.get("tests", [])
        if tests:
            print_output(
                tests,
                columns=["test_name", "occurrences"],
                as_json=False,
            )


@history_app.command("stats")
def history_stats(
    job_name: str = typer.Argument(help="Jenkins job name."),
    exclude_job_id: str = typer.Option(
        "", "--exclude-job-id", help="Exclude results from this job ID."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Show aggregate statistics for a job."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_job_stats(job_name, exclude_job_id=exclude_job_id)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        overall_rate = data.get("overall_failure_rate")
        overall_rate_str = f"{overall_rate:.1%}" if overall_rate is not None else "N/A"
        typer.echo(f"Job: {data.get('job_name', '')}")
        typer.echo(f"Builds analyzed: {data.get('total_builds_analyzed', 0)}")
        typer.echo(f"Builds with failures: {data.get('builds_with_failures', 0)}")
        typer.echo(f"Failure rate: {overall_rate_str}")
        failures = data.get("most_common_failures", [])
        if failures:
            typer.echo("\nMost common failures:")
            print_output(
                failures,
                columns=["test_name", "count", "classification"],
                as_json=False,
            )


@history_app.command("failures")
def history_failures(
    limit: int = typer.Option(50, "--limit", "-l"),
    offset: int = typer.Option(0, "--offset", "-o"),
    search: str = typer.Option("", "--search", "-s", help="Search test names."),
    classification: str = typer.Option("", "--classification", "-c"),
    job_name: str = typer.Option("", "--job-name", "-j"),
    json_output: bool = _JSON_OPTION,
):
    """List paginated failure history."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_all_failures(
            search=search,
            job_name=job_name,
            classification=classification,
            limit=limit,
            offset=offset,
        )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        failures = data.get("failures", [])
        typer.echo(
            f"Total: {data.get('total', 0)} (showing {len(failures)}, offset {data.get('offset', 0)})"
        )
        if failures:
            print_output(
                failures,
                columns=["test_name", "job_name", "classification", "analyzed_at"],
                labels={"analyzed_at": "DATE"},
                as_json=False,
            )


# -- Classify -----------------------------------------------------------------


@app.command()
def classify(
    test_name: str = typer.Argument(help="Fully qualified test name."),
    classification: str = typer.Option(
        ...,
        "--type",
        "-t",
        help="FLAKY, REGRESSION, INFRASTRUCTURE, KNOWN_BUG, or INTERMITTENT.",
    ),
    job_id: str = typer.Option(
        ..., "--job-id", help="Job ID this classification applies to."
    ),
    reason: str = typer.Option("", "--reason", "-r", help="Reason for classification."),
    job_name: str = typer.Option("", "--job-name", "-j"),
    references: str = typer.Option("", "--references", help="Bug URLs or ticket keys."),
    child_job: str = typer.Option("", "--child-job", help="Child job name."),
    child_build: int = typer.Option(0, "--child-build", help="Child build number."),
    json_output: bool = _JSON_OPTION,
):
    """Classify a test failure."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.classify_test(
            test_name=test_name,
            classification=classification.upper(),
            job_id=job_id,
            reason=reason,
            job_name=child_job if child_job else job_name,
            references=references,
            child_build_number=child_build,
        )
    except JJIError as err:
        _handle_error(err)
    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Classification created (id: {data.get('id', '')})")


# -- Classifications ---------------------------------------------------------


@classifications_app.command("list")
def classifications_list(
    job_id: str = typer.Option("", "--job-id"),
    test_name: str = typer.Option("", "--test-name", "-t"),
    classification: str = typer.Option("", "--type", "-c"),
    job_name: str = typer.Option("", "--job-name", "-j"),
    parent_job_name: str = typer.Option(
        "", "--parent-job-name", help="Filter by parent job name."
    ),
    json_output: bool = _JSON_OPTION,
):
    """List test classifications."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_classifications(
            test_name=test_name,
            classification=classification.upper() if classification else "",
            job_name=job_name,
            parent_job_name=parent_job_name,
            job_id=job_id,
        )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        items = data.get("classifications", [])
        if items:
            print_output(
                items,
                columns=[
                    "test_name",
                    "classification",
                    "reason",
                    "created_by",
                    "created_at",
                ],
                labels={"created_by": "BY", "created_at": "DATE"},
                as_json=False,
            )
        else:
            typer.echo("No classifications found.")


# -- Comments -----------------------------------------------------------------


@comments_app.command("list")
def comments_list(
    job_id: str = typer.Argument(help="Job ID to list comments for."),
    json_output: bool = _JSON_OPTION,
):
    """List comments for a job."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_comments(job_id)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        items = data.get("comments", [])
        if items:
            print_output(
                items,
                columns=["id", "test_name", "comment", "username", "created_at"],
                labels={"created_at": "DATE"},
                as_json=False,
            )
        else:
            typer.echo("No comments for this job.")


@comments_app.command("add")
def comments_add(
    job_id: str = typer.Argument(help="Job ID."),
    test_name: str = typer.Option(..., "--test", "-t", help="Test name to comment on."),
    message: str = typer.Option(..., "--message", "-m", help="Comment text."),
    child_job_name: str = typer.Option("", "--child-job"),
    child_build_number: int = typer.Option(0, "--child-build"),
    json_output: bool = _JSON_OPTION,
):
    """Add a comment to a test failure."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.add_comment(
            job_id=job_id,
            test_name=test_name,
            comment=message,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
        )
    except JJIError as err:
        _handle_error(err)
    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Comment added (id: {data.get('id', '')})")


@comments_app.command("delete")
def comments_delete(
    job_id: str = typer.Argument(help="Job ID."),
    comment_id: int = typer.Argument(help="Comment ID to delete."),
    json_output: bool = _JSON_OPTION,
):
    """Delete a comment."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.delete_comment(job_id, comment_id)
    except JJIError as err:
        _handle_error(err)
    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo("Comment deleted.")


# -- AI Configs ---------------------------------------------------------------


@app.command()
def capabilities(
    json_output: bool = _JSON_OPTION,
):
    """Show which post-analysis automation features the server supports.

    Reports whether the server is configured to create GitHub issues
    and Jira bugs automatically. Requires server-level credentials.
    """
    data = _run_client_command(
        json_output,
        lambda c: c.capabilities(),
        emit_output=False,
    )
    if not _state.get("json", False):
        print_output(data, columns=[], as_json=True)


@app.command("jira-projects")
def jira_projects_cmd(
    query: str = typer.Option("", help="Search query to filter projects."),
    jira_token: str = typer.Option(
        "", "--jira-token", help="Jira token (uses config fallback)."
    ),
    jira_email: str = typer.Option(
        "", "--jira-email", help="Jira email (uses config fallback)."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """List available Jira projects."""
    _jira_token, _jira_email = _resolve_jira_cli_auth(jira_token, jira_email)
    data = _run_client_command(
        json_output,
        lambda c: c.jira_projects(
            jira_token=_jira_token, jira_email=_jira_email, query=query
        ),
        emit_output=False,
    )
    if not _state.get("json", False):
        if not data:
            typer.echo("No Jira projects found.")
        else:
            print_output(
                data,
                columns=["key", "name"],
                labels={"key": "KEY", "name": "NAME"},
                as_json=False,
            )


@app.command("jira-security-levels")
def jira_security_levels_cmd(
    project_key: str = typer.Argument(help="Jira project key."),
    jira_token: str = typer.Option("", "--jira-token", help="Jira token."),
    jira_email: str = typer.Option("", "--jira-email", help="Jira email."),
    json_output: bool = _JSON_OPTION,
) -> None:
    """List security levels for a Jira project."""
    _jira_token, _jira_email = _resolve_jira_cli_auth(jira_token, jira_email)
    data = _run_client_command(
        json_output,
        lambda c: c.jira_security_levels(
            project_key=project_key, jira_token=_jira_token, jira_email=_jira_email
        ),
        emit_output=False,
    )
    if not _state.get("json", False):
        if not data:
            typer.echo("No security levels found.")
        else:
            print_output(
                data,
                columns=["name", "description"],
                labels={"name": "NAME", "description": "DESCRIPTION"},
                as_json=False,
            )


@app.command("ai-configs")
def ai_configs(
    json_output: bool = _JSON_OPTION,
):
    """List known AI provider/model configurations from successful analyses."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_ai_configs()
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        if not data:
            typer.echo("No AI configurations found from completed analyses.")
            raise typer.Exit()
        print_output(
            data,
            columns=["ai_provider", "ai_model"],
            labels={"ai_provider": "AI PROVIDER", "ai_model": "AI MODEL"},
            as_json=False,
        )


# -- AI Models ----------------------------------------------------------------


def _print_ai_model_rows(models: list[dict]) -> None:
    """Render an id/name table for AI model rows."""
    print_output(
        models,
        columns=["id", "name"],
        labels={"id": "MODEL ID", "name": "DISPLAY NAME"},
        as_json=False,
    )


@app.command("ai-models")
def ai_models_cmd(
    provider: str = typer.Option(
        "",
        "--provider",
        "-p",
        help="Filter by AI provider (e.g. cursor, claude, gemini).",
    ),
    json_output: bool = _JSON_OPTION,
):
    """List available AI models per provider."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.list_ai_models(provider=provider)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        if provider:
            models = data.get("models", [])
            if not models:
                typer.echo(f"No models found for provider '{provider}'.")
                raise typer.Exit()
            typer.echo(f"Models for {provider}:")
            _print_ai_model_rows(models)
        else:
            providers_data = data.get("providers", {})
            if not providers_data:
                typer.echo("No models found.")
                raise typer.Exit()
            for prov, models in sorted(providers_data.items()):
                typer.echo(f"\n{prov} ({len(models)} models):")
                if models:
                    _print_ai_model_rows(models)


# -- Users ----------------------------------------------------------------
# Note: /api/notifications/subscribe, /api/notifications/unsubscribe, and
# /api/notifications/vapid-public-key are intentionally browser-only (Web Push
# subscriptions require a browser service worker). No CLI equivalent needed.
# See AGENTS.md CLI Parity exceptions.


@app.command("mentionable-users")
def mentionable_users_cmd(
    json_output: bool = _JSON_OPTION,
):
    """List users that can be mentioned in comments."""
    data = _run_client_command(
        json_output,
        lambda c: c.get_mentionable_users(),
        emit_output=False,
    )
    if not _state.get("json", False):
        usernames = data.get("usernames", [])
        if usernames:
            for name in usernames:
                typer.echo(name)
        else:
            typer.echo("No mentionable users found.")


@app.command("mentions")
def mentions_cmd(
    limit: int = typer.Option(50, "--limit", "-l", help="Max mentions to return."),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination."),
    unread: bool = typer.Option(False, "--unread", help="Show only unread mentions."),
    json_output: bool = _JSON_OPTION,
):
    """List your @mentions across all reports."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_mentions(limit=limit, offset=offset, unread_only=unread)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        mentions = data.get("mentions", [])
        total = data.get("total", 0)
        unread_count = data.get("unread_count", 0)
        typer.echo(f"Total: {total} mention(s), {unread_count} unread")
        if mentions:
            print_output(
                mentions,
                columns=[
                    "id",
                    "job_id",
                    "test_name",
                    "comment",
                    "username",
                    "is_read",
                    "created_at",
                ],
                labels={"username": "BY", "is_read": "READ", "created_at": "DATE"},
                as_json=False,
            )
        else:
            typer.echo("No mentions found.")


@app.command("mentions-mark-read")
def mentions_mark_read_cmd(
    ids: str = typer.Option(
        ..., "--ids", help="Comma-separated comment IDs to mark as read."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Mark specific mentions as read."""
    raw_parts = [x.strip() for x in ids.split(",") if x.strip()]
    if not raw_parts:
        typer.echo("Error: --ids must contain at least one integer ID.", err=True)
        raise typer.Exit(1)
    comment_ids = []
    for part in raw_parts:
        try:
            cid = int(part)
        except ValueError:
            cid = -1
        if cid <= 0:
            typer.echo(
                f"Error: invalid ID '{part}' — must be a positive integer.", err=True
            )
            raise typer.Exit(1)
        comment_ids.append(cid)
    _run_client_command(
        json_output,
        lambda c: c.mark_mentions_read(comment_ids),
    )


@app.command("mentions-mark-all-read")
def mentions_mark_all_read_cmd(
    json_output: bool = _JSON_OPTION,
):
    """Mark all mentions as read."""
    _run_client_command(
        json_output,
        lambda c: c.mark_all_mentions_read(),
    )


# -- Bug Creation -------------------------------------------------------------


def _resolve_tracker_tokens(
    github_token: str,
    jira_token: str,
    jira_email: str,
    jira_project_key: str = "",
    github_repo_url: str = "",
    jira_security_level: str = "",
) -> tuple[str, str, str, str, str, str]:
    """Resolve tracker tokens and related fields with config fallback."""
    cfg = _state.get("server_config")
    return (
        github_token.strip() or ((cfg.github_token or "").strip() if cfg else ""),
        jira_token.strip() or ((cfg.jira_token or "").strip() if cfg else ""),
        jira_email.strip() or ((cfg.jira_email or "").strip() if cfg else ""),
        jira_project_key.strip()
        or ((cfg.jira_project_key or "").strip() if cfg else ""),
        github_repo_url.strip() or ((cfg.github_repo_url or "").strip() if cfg else ""),
        jira_security_level.strip()
        or ((cfg.jira_security_level or "").strip() if cfg else ""),
    )


def _validate_issue_type(issue_type: str) -> str:
    """Validate and normalize issue type, exit on invalid input."""
    normalized = issue_type.lower()
    if normalized not in ("github", "jira"):
        typer.echo(
            f"Error: --type must be 'github' or 'jira', got '{issue_type}'",
            err=True,
        )
        raise typer.Exit(code=1)
    return normalized


@app.command("preview-issue")
def preview_issue(
    job_id: str = typer.Argument(help="Job ID."),
    test_name: str = typer.Option(..., "--test", "-t", help="Test name."),
    issue_type: str = typer.Option(..., "--type", help="github or jira."),
    child_job_name: str = typer.Option("", "--child-job"),
    child_build_number: int = typer.Option(0, "--child-build"),
    include_links: bool = typer.Option(
        False, "--include-links", help="Include full URLs as clickable links."
    ),
    ai_provider: str = typer.Option(
        "", "--ai-provider", help="AI provider for content generation."
    ),
    ai_model: str = typer.Option(
        "", "--ai-model", help="AI model for content generation."
    ),
    github_token: str = typer.Option(
        "",
        "--github-token",
        help="GitHub PAT for issue creation (overrides server config).",
    ),
    github_repo_url: str = typer.Option(
        "",
        "--github-repo-url",
        help="GitHub repository URL (overrides server config).",
    ),
    jira_token: str = typer.Option(
        "",
        "--jira-token",
        help="Jira token for bug creation (overrides server config).",
    ),
    jira_email: str = typer.Option(
        "", "--jira-email", help="Jira email for Cloud auth (used with --jira-token)."
    ),
    jira_project_key: str = typer.Option(
        "",
        "--jira-project-key",
        help="Jira project key for bug creation (overrides server config).",
    ),
    jira_security_level: str = typer.Option(
        "",
        "--jira-security-level",
        help="Jira security level name for restricted issues.",
    ),
    issue_prompt: str = typer.Option(
        "",
        "--issue-prompt",
        help="Additional AI instructions for issue generation.",
    ),
    json_output: bool = _JSON_OPTION,
):
    """Preview generated issue content (GitHub or Jira)."""
    _set_json(json_output)
    normalized_type = _validate_issue_type(issue_type)
    (
        _github_token,
        _jira_token,
        _jira_email,
        _jira_project_key,
        _github_repo_url,
        _jira_security_level,
    ) = _resolve_tracker_tokens(
        github_token,
        jira_token,
        jira_email,
        jira_project_key,
        github_repo_url,
        jira_security_level,
    )
    try:
        client = _get_client()
        if normalized_type == "github":
            data = client.preview_github_issue(
                job_id=job_id,
                test_name=test_name,
                child_job_name=child_job_name,
                child_build_number=child_build_number,
                include_links=include_links,
                ai_provider=ai_provider,
                ai_model=ai_model,
                github_token=_github_token,
                github_repo_url=_github_repo_url,
                issue_prompt=issue_prompt,
            )
        else:
            data = client.preview_jira_bug(
                job_id=job_id,
                test_name=test_name,
                child_job_name=child_job_name,
                child_build_number=child_build_number,
                include_links=include_links,
                ai_provider=ai_provider,
                ai_model=ai_model,
                jira_token=_jira_token,
                jira_email=_jira_email,
                jira_project_key=_jira_project_key,
                jira_security_level=_jira_security_level,
                issue_prompt=issue_prompt,
            )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Title: {data.get('title', '')}")
        typer.echo(f"\nBody:\n{data.get('body', '')}")
        similar = data.get("similar_issues", [])
        if similar:
            typer.echo(f"\nSimilar issues ({len(similar)}):")
            for s in similar:
                label = s.get("key") or f"#{s.get('number', '')}"
                typer.echo(f"  {label}: {s.get('title', '')} ({s.get('url', '')})")


@app.command("get-issue-prompt")
def get_issue_prompt(
    job_id: str = typer.Argument(help="Job ID."),
    json_output: bool = _JSON_OPTION,
):
    """Get the issue prompt from the test repo associated with a job."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.get_issue_prompt(job_id=job_id)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        prompt = data.get("prompt", "")
        if prompt:
            typer.echo(prompt)
        else:
            typer.echo("No issue prompt found.")


@app.command("create-issue")
def create_issue(
    job_id: str = typer.Argument(help="Job ID."),
    test_name: str = typer.Option(..., "--test", "-t", help="Test name."),
    issue_type: str = typer.Option(..., "--type", help="github or jira."),
    title: str = typer.Option(..., "--title", help="Issue title."),
    body: str = typer.Option(..., "--body", help="Issue body."),
    child_job_name: str = typer.Option("", "--child-job"),
    child_build_number: int = typer.Option(0, "--child-build"),
    github_token: str = typer.Option(
        "",
        "--github-token",
        help="GitHub PAT for issue creation (overrides server config).",
    ),
    github_repo_url: str = typer.Option(
        "",
        "--github-repo-url",
        help="GitHub repository URL (overrides server config).",
    ),
    jira_token: str = typer.Option(
        "",
        "--jira-token",
        help="Jira token for bug creation (overrides server config).",
    ),
    jira_email: str = typer.Option(
        "", "--jira-email", help="Jira email for Cloud auth (used with --jira-token)."
    ),
    jira_project_key: str = typer.Option(
        "",
        "--jira-project-key",
        help="Jira project key for bug creation (overrides server config).",
    ),
    jira_security_level: str = typer.Option(
        "",
        "--jira-security-level",
        help="Jira security level name for restricted issues.",
    ),
    jira_issue_type: str = typer.Option(
        "Bug",
        "--jira-issue-type",
        help="Jira issue type name (e.g. Bug, Story, Task). Default: Bug.",
    ),
    json_output: bool = _JSON_OPTION,
):
    """Create a GitHub issue or Jira bug from a failure analysis."""
    _set_json(json_output)
    normalized_type = _validate_issue_type(issue_type)
    (
        _github_token,
        _jira_token,
        _jira_email,
        _jira_project_key,
        _github_repo_url,
        _jira_security_level,
    ) = _resolve_tracker_tokens(
        github_token,
        jira_token,
        jira_email,
        jira_project_key,
        github_repo_url,
        jira_security_level,
    )
    try:
        client = _get_client()
        if normalized_type == "github":
            data = client.create_github_issue(
                job_id=job_id,
                test_name=test_name,
                title=title,
                body=body,
                child_job_name=child_job_name,
                child_build_number=child_build_number,
                github_token=_github_token,
                github_repo_url=_github_repo_url,
            )
        else:
            data = client.create_jira_bug(
                job_id=job_id,
                test_name=test_name,
                title=title,
                body=body,
                child_job_name=child_job_name,
                child_build_number=child_build_number,
                jira_token=_jira_token,
                jira_email=_jira_email,
                jira_project_key=_jira_project_key,
                jira_security_level=_jira_security_level,
                jira_issue_type=jira_issue_type,
            )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        key_or_number = data.get("key") or f"#{data.get('number', '')}"
        typer.echo(f"Created: {key_or_number}")
        typer.echo(f"URL: {data.get('url', '')}")
        if data.get("comment_id"):
            typer.echo(f"Comment added (id: {data['comment_id']})")


@app.command("validate-token")
def validate_token_cmd(
    token_type: str = typer.Argument(help="Token type: 'github' or 'jira'"),
    token: str = typer.Option(
        ..., help="Token value to validate", prompt=True, hide_input=True
    ),
    email: str = typer.Option("", help="Email for Jira Cloud auth"),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Validate a GitHub or Jira token."""
    token_type = _validate_issue_type(token_type)
    data = _run_client_command(
        json_output,
        lambda c: c.validate_token(token_type=token_type, token=token, email=email),
        emit_output=False,
    )
    if data.get("valid"):
        if not _state.get("json", False):
            typer.echo(f"\u2713 Valid \u2014 {data.get('message', '')}")
        return

    if not _state.get("json", False):
        typer.echo(f"\u2717 Invalid \u2014 {data.get('message', '')}")
    raise typer.Exit(code=1)


@app.command("push-reportportal")
@app.command("push-rp", hidden=True)
def push_rp_cmd(
    job_id: str = typer.Argument(help="Job ID to push classifications for."),
    child_job_name: str | None = typer.Option(
        None, "--child-job-name", help="Child job name (for pipeline child push)."
    ),
    child_build_number: int | None = typer.Option(
        None,
        "--child-build-number",
        help="Child build number (for pipeline child push).",
    ),
    json_output: bool = _JSON_OPTION,
):
    """Push JJI classifications into Report Portal test items."""
    data = _run_client_command(
        json_output,
        lambda c: c.push_reportportal(
            job_id,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
        ),
        emit_output=False,
    )
    if not _state.get("json", False):
        pushed = data.get("pushed", 0)
        errors = data.get("errors", [])
        unmatched = data.get("unmatched", [])
        launch_id = data.get("launch_id")
        typer.echo(f"Pushed {pushed} classification(s) to Report Portal")
        if launch_id is not None:
            typer.echo(f"Launch ID: {launch_id}")
        if unmatched:
            typer.echo(f"Unmatched: {', '.join(unmatched)}")
        if errors:
            typer.echo(f"Errors: {len(errors)}")
            for err in errors:
                typer.echo(f"  - {err}")


@app.command("override-classification")
def override_classification_cmd(
    job_id: str = typer.Argument(help="Job ID."),
    test_name: str = typer.Option(..., "--test", "-t", help="Test name."),
    classification: str = typer.Option(
        ...,
        "--classification",
        "-c",
        help="CODE ISSUE, PRODUCT BUG, or INFRASTRUCTURE.",
    ),
    child_job_name: str = typer.Option("", "--child-job"),
    child_build_number: int = typer.Option(0, "--child-build"),
    json_output: bool = _JSON_OPTION,
):
    """Override the classification of a failure."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.override_classification(
            job_id=job_id,
            test_name=test_name,
            classification=classification,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
        )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Classification overridden to: {data.get('classification', '')}")


@app.command("analyze-comment-intent")
def analyze_comment_intent_cmd(
    comment: str = typer.Argument(help="Comment text to analyze."),
    job_id: str = typer.Option(
        "", "--job-id", help="Job ID to resolve AI config from the analyzed job."
    ),
    ai_provider: str = typer.Option(
        "", "--ai-provider", help="AI provider for content generation."
    ),
    ai_model: str = typer.Option(
        "", "--ai-model", help="AI model for content generation."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Analyze whether a comment suggests a failure has been reviewed/resolved."""
    _set_json(json_output)
    try:
        client = _get_client()
        data = client.analyze_comment_intent(
            comment=comment, job_id=job_id, ai_provider=ai_provider, ai_model=ai_model
        )
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        reviewed = data.get("suggests_reviewed", False)
        reason = data.get("reason", "")
        typer.echo(f"Suggests reviewed: {reviewed}")
        if reason:
            typer.echo(f"Reason: {reason}")


# -- Auth ---------------------------------------------------------------------


@auth_app.command("login")
def auth_login(
    username: str = typer.Option(..., "--username", "-u", help="Admin username."),
    api_key: str = typer.Option(..., "--api-key", "-k", help="Admin API key."),
    json_output: bool = _JSON_OPTION,
):
    """Validate admin credentials. This does not persist a session.

    For persistent auth, set api_key in ~/.config/jji/config.toml or use
    --api-key / JJI_API_KEY on each command.
    """
    data = _run_client_command(
        json_output,
        lambda c: c.login(username, api_key),
        emit_output=False,
    )
    if not _state.get("json", False):
        role = data.get("role", "user")
        is_admin = data.get("is_admin", False)
        typer.echo(
            f"Logged in as {data.get('username', username)} (role: {role}, admin: {is_admin})"
        )


@auth_app.command("logout")
def auth_logout(
    json_output: bool = _JSON_OPTION,
):
    """Logout (clear admin session)."""
    _run_client_command(json_output, lambda c: c.logout())


@auth_app.command("whoami")
def auth_whoami(
    json_output: bool = _JSON_OPTION,
):
    """Show current authenticated user info."""
    _run_client_command(
        json_output,
        lambda c: c.auth_me(),
        columns=["username", "role", "is_admin"],
    )


# -- Admin --------------------------------------------------------------------


@admin_users_app.command("list")
def admin_users_list(
    json_output: bool = _JSON_OPTION,
):
    """List all users (admin and regular)."""
    data = _run_client_command(
        json_output,
        lambda c: c.admin_list_users(),
        emit_output=False,
    )
    if not _state.get("json", False):
        users = data.get("users", [])
        if users:
            print_output(
                users,
                columns=["username", "role", "created_at", "last_seen"],
                labels={"created_at": "CREATED", "last_seen": "LAST SEEN"},
                as_json=False,
            )
        else:
            typer.echo("No users found.")


@admin_users_app.command("create")
def admin_users_create(
    username: str = typer.Argument(..., help="Username for the new admin user."),
    json_output: bool = _JSON_OPTION,
):
    """Create a new admin user. The API key is shown once \u2014 save it."""
    data = _run_client_command(
        json_output,
        lambda c: c.admin_create_user(username),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Created admin user: {data.get('username', username)}")
        typer.echo(f"API Key: {data.get('api_key', '(not returned)')}")
        typer.echo("")
        typer.echo(
            "\u26a0\ufe0f  Save this API key now \u2014 it cannot be retrieved later."
        )


@admin_users_app.command("delete")
def admin_users_delete(
    username: str = typer.Argument(..., help="Username of the admin to delete."),
    json_output: bool = _JSON_OPTION,
    force: bool = typer.Option(
        False, "--force", "-f", help="Skip confirmation prompt."
    ),
):
    """Delete an admin user."""
    if not force:
        confirm = typer.confirm(f"Delete admin user '{username}'?")
        if not confirm:
            raise typer.Abort()
    _run_client_command(
        json_output,
        lambda c: c.admin_delete_user(username),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Deleted admin user: {username}")


@admin_users_app.command("rotate-key")
def admin_users_rotate_key(
    username: str = typer.Argument(
        ..., help="Username of the admin to rotate key for."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Rotate an admin user's API key. The new key is shown once."""
    data = _run_client_command(
        json_output,
        lambda c: c.admin_rotate_key(username),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Rotated API key for: {data.get('username', username)}")
        typer.echo(f"New API Key: {data.get('new_api_key', '(not returned)')}")
        typer.echo("")
        typer.echo(
            "\u26a0\ufe0f  Save this API key now \u2014 it cannot be retrieved later."
        )


@admin_users_app.command("change-role")
def admin_users_change_role(
    username: str = typer.Argument(..., help="Username to change role for."),
    role: str = typer.Argument(..., help="New role: 'admin' or 'user'."),
    json_output: bool = _JSON_OPTION,
):
    """Change a user's role. Promoting to admin generates an API key."""
    data = _run_client_command(
        json_output,
        lambda c: c.admin_change_role(username, role),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Changed role of '{data.get('username', username)}' to '{role}'")
        api_key = data.get("api_key")
        if api_key:
            typer.echo(f"API Key: {api_key}")
            typer.echo("")
            typer.echo(
                "\u26a0\ufe0f  Save this API key now \u2014 it cannot be retrieved later."
            )


# -- Token Usage (Admin) ------------------------------------------------------


def _date_offset(days: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


def _format_cost(value: float | None, precision: int = 2) -> str:
    """Format a USD cost value, returning 'N/A' when absent."""
    if value is None:
        return "N/A"
    return f"${value:.{precision}f}"


def _print_token_summary(data: dict) -> None:
    """Print dashboard summary."""
    for period_name in ["today", "this_week", "this_month"]:
        period = data.get(period_name, {})
        label = period_name.replace("_", " ").title()
        typer.echo(f"\n{label}:")
        typer.echo(f"  Calls: {period.get('calls', 0)}")
        typer.echo(f"  Tokens: {period.get('tokens', 0):,}")
        typer.echo(f"  Cost: {_format_cost(period.get('cost_usd'))}")

    top_models = data.get("top_models", [])
    if top_models:
        typer.echo("\nTop Models (30 days):")
        for m in top_models:
            typer.echo(
                f"  {m.get('model', 'unknown')}: {m.get('calls', 0)} calls, {_format_cost(m.get('cost_usd'))}"
            )


def _print_token_usage_table(data: dict) -> None:
    """Print aggregated token usage."""
    typer.echo(f"Total calls: {data.get('total_calls', 0)}")
    typer.echo(f"Input tokens: {data.get('total_input_tokens', 0):,}")
    typer.echo(f"Output tokens: {data.get('total_output_tokens', 0):,}")
    typer.echo(f"Cache read: {data.get('total_cache_read_tokens', 0):,}")
    typer.echo(f"Cache write: {data.get('total_cache_write_tokens', 0):,}")
    typer.echo(f"Cost: {_format_cost(data.get('total_cost_usd'))}")
    typer.echo(f"Duration: {data.get('total_duration_ms', 0):,}ms")

    breakdown = data.get("breakdown", [])
    if breakdown:
        typer.echo("\nBreakdown:")
        for row in breakdown:
            typer.echo(
                f"  {row.get('group_key', 'N/A')}: "
                f"{row.get('call_count', 0)} calls, "
                f"{_format_cost(row.get('cost_usd'))}, "
                f"avg {row.get('avg_duration_ms', 0)}ms"
            )


def _print_job_token_usage(data: dict) -> None:
    """Print per-job token usage."""
    typer.echo(f"Job: {data.get('job_id', 'N/A')}")
    records = data.get("records", [])
    for rec in records:
        typer.echo(
            f"\n  [{rec.get('call_type', '')}] "
            f"{rec.get('ai_provider', '')}/{rec.get('ai_model', '')}"
        )
        typer.echo(
            f"    Input: {rec.get('input_tokens', 0):,}  "
            f"Output: {rec.get('output_tokens', 0):,}"
        )
        typer.echo(
            f"    Cost: {_format_cost(rec.get('cost_usd'), precision=4)}  Duration: {rec.get('duration_ms', 0)}ms"
        )


def _print_token_usage_csv(rows: list[dict]) -> None:
    """Print token usage as CSV."""
    import csv
    import sys

    if not rows:
        typer.echo("No data", err=True)
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys())
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in rows[0].keys()})


@admin_app.command("token-usage")
def token_usage_cmd(
    period: str | None = typer.Option(None, help="Period: today, week, month, all"),
    start_date: str | None = typer.Option(
        None, "--start-date", help="Start date (YYYY-MM-DD)"
    ),
    end_date: str | None = typer.Option(
        None, "--end-date", help="End date (YYYY-MM-DD)"
    ),
    provider: str | None = typer.Option(None, help="Filter by AI provider"),
    model: str | None = typer.Option(None, help="Filter by AI model"),
    call_type: str | None = typer.Option(None, help="Filter by call type"),
    group_by: str | None = typer.Option(
        None,
        help="Group by: provider, model, call_type, day, week, month, job",
    ),
    job_id: str | None = typer.Option(None, help="Get usage for specific job"),
    output_format: str = typer.Option(
        "table", "--format", help="Output format: table, json, csv"
    ),
    json_output: bool = _JSON_OPTION,
):
    """View AI token usage and costs. Admin only."""
    _set_json(json_output)
    # --json flag overrides --format
    effective_format = "json" if _state.get("json", False) else output_format

    valid_periods = {"today", "week", "month", "all"}
    valid_formats = {"table", "json", "csv"}
    if period and period not in valid_periods:
        typer.echo(
            f"Invalid --period: {period}. Valid: {', '.join(sorted(valid_periods))}",
            err=True,
        )
        raise typer.Exit(1)
    if effective_format not in valid_formats:
        typer.echo(
            f"Invalid --format: {effective_format}. Valid: {', '.join(sorted(valid_formats))}",
            err=True,
        )
        raise typer.Exit(1)

    try:
        client = _get_client()

        if job_id:
            data = client.get_token_usage_for_job(job_id)
            if effective_format == "json":
                print_output(data, columns=[], as_json=True)
            elif effective_format == "csv":
                _print_token_usage_csv(data.get("records", []))
            else:
                _print_job_token_usage(data)
            return

        if period == "today":
            start_date = start_date or _date_offset(0)
        elif period == "week":
            start_date = start_date or _date_offset(7)
        elif period == "month":
            start_date = start_date or _date_offset(30)
        # period == "all" — no date filter, falls through to main query

        if (
            period is None
            and not start_date
            and not end_date
            and not provider
            and not model
            and not call_type
            and not group_by
        ):
            # Summary dashboard mode — only when no period or filters specified
            data = client.get_token_usage_summary()
            if effective_format == "json":
                print_output(data, columns=[], as_json=True)
            elif effective_format == "csv":
                # Flatten period rows for CSV output
                rows = []
                for period_name in ["today", "this_week", "this_month"]:
                    period_data = data.get(period_name, {})
                    if isinstance(period_data, dict):
                        rows.append({"period": period_name, **period_data})
                _print_token_usage_csv(rows)
            else:
                _print_token_summary(data)
            return

        data = client.get_token_usage(
            start_date=start_date,
            end_date=end_date,
            ai_provider=provider,
            ai_model=model,
            call_type=call_type,
            group_by=group_by,
        )
        if effective_format == "json":
            print_output(data, columns=[], as_json=True)
        elif effective_format == "csv":
            breakdown = data.get("breakdown", [])
            if breakdown:
                _print_token_usage_csv(breakdown)
            else:
                # No group_by — output the totals row as CSV
                totals = {k: v for k, v in data.items() if k != "breakdown"}
                _print_token_usage_csv([totals] if totals else [])
        else:
            _print_token_usage_table(data)
    except JJIError as err:
        _handle_error(err)


# -- Metadata -----------------------------------------------------------------


_METADATA_COLUMNS = ["job_name", "team", "tier", "version", "labels"]
_METADATA_COLUMN_LABELS = {"job_name": "JOB NAME"}


@metadata_app.command("list")
def metadata_list(
    team: str = typer.Option("", "--team", help="Filter by team."),
    tier: str = typer.Option("", "--tier", help="Filter by tier."),
    version: str = typer.Option("", "--version", help="Filter by version."),
    label: list[str] = typer.Option(  # noqa: B008
        [], "--label", "-l", help="Filter by label (can repeat)."
    ),
    json_output: bool = _JSON_OPTION,
):
    """List job metadata with optional filters."""
    _run_client_command(
        json_output,
        lambda c: c.list_jobs_metadata(
            team=team, tier=tier, version=version, labels=label or None
        ),
        columns=_METADATA_COLUMNS,
        labels=_METADATA_COLUMN_LABELS,
    )


@metadata_app.command("get")
def metadata_get(
    job_name: str = typer.Argument(help="Job name."),
    json_output: bool = _JSON_OPTION,
):
    """Show metadata for a specific job."""
    _run_client_command(
        json_output,
        lambda c: c.get_job_metadata(job_name),
        columns=_METADATA_COLUMNS,
    )


@metadata_app.command("set")
def metadata_set(
    job_name: str = typer.Argument(help="Job name."),
    team: str = typer.Option("", "--team", help="Team owning this job."),
    tier: str = typer.Option("", "--tier", help="Service tier."),
    version: str = typer.Option("", "--version", help="Version label."),
    label: list[str] = typer.Option(  # noqa: B008
        [], "--label", "-l", help="Label (can repeat)."
    ),
    json_output: bool = _JSON_OPTION,
):
    """Set or update metadata for a job."""
    data = _run_client_command(
        json_output,
        lambda c: c.set_job_metadata(
            job_name, team=team, tier=tier, version=version, labels=label or None
        ),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Metadata set for {data.get('job_name', job_name)}")


@metadata_app.command("delete")
def metadata_delete(
    job_name: str = typer.Argument(help="Job name."),
    json_output: bool = _JSON_OPTION,
):
    """Delete metadata for a job."""
    data = _run_client_command(
        json_output,
        lambda c: c.delete_job_metadata(job_name),
        emit_output=False,
    )
    if not _state.get("json", False):
        typer.echo(f"Metadata deleted for {data.get('job_name', job_name)}")


@metadata_app.command("import")
def metadata_import(
    file_path: str = typer.Argument(help="Path to JSON or YAML file."),
    json_output: bool = _JSON_OPTION,
):
    """Bulk import metadata from a JSON or YAML file.

    File format: a list of objects with job_name, team, tier, version, labels.
    """
    import json as json_mod
    from pathlib import Path

    _set_json(json_output)
    path = Path(file_path)
    if not path.exists():
        typer.echo(f"Error: file not found: {file_path}", err=True)
        raise typer.Exit(code=1)

    content = path.read_text(encoding="utf-8")
    items: list[dict] = []

    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        try:
            items = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            typer.echo(f"Error: invalid YAML: {exc}", err=True)
            raise typer.Exit(code=1) from None
    else:
        try:
            items = json_mod.loads(content)
        except json_mod.JSONDecodeError as exc:
            typer.echo(f"Error: invalid JSON: {exc}", err=True)
            raise typer.Exit(code=1) from None

    if not isinstance(items, list):
        typer.echo("Error: file must contain a JSON/YAML array of objects.", err=True)
        raise typer.Exit(code=1)

    try:
        client = _get_client()
        data = client.bulk_set_metadata(items)
    except JJIError as err:
        _handle_error(err)

    if _state.get("json", False):
        print_output(data, columns=[], as_json=True)
    else:
        typer.echo(f"Imported {data.get('updated', 0)} metadata entries.")


@metadata_app.command("rules")
def metadata_rules(
    json_output: bool = _JSON_OPTION,
):
    """List configured metadata rules for auto-assignment."""
    data = _run_client_command(
        json_output,
        lambda c: c.list_metadata_rules(),
        emit_output=False,
    )
    if not _state.get("json", False):
        rules = data.get("rules", [])
        rules_file = data.get("rules_file")
        if rules_file:
            typer.echo(f"Rules file: {rules_file}")
        if not rules:
            typer.echo("No metadata rules configured.")
        else:
            typer.echo(f"{len(rules)} rule(s):")
            for i, rule in enumerate(rules, 1):
                parts = [f"  {i}. pattern={rule['pattern']!r}"]
                for key in ("team", "tier", "version"):
                    if key in rule:
                        parts.append(f"{key}={rule[key]!r}")
                if "labels" in rule:
                    parts.append(f"labels={rule['labels']}")
                typer.echo(", ".join(parts))


@metadata_app.command("preview")
def metadata_preview(
    job_name: str = typer.Argument(help="Job name to preview rules against."),
    json_output: bool = _JSON_OPTION,
):
    """Preview what metadata rules would assign to a job name."""
    data = _run_client_command(
        json_output,
        lambda c: c.preview_metadata_rules(job_name),
        emit_output=False,
    )
    if not _state.get("json", False):
        if data.get("matched"):
            meta = data.get("metadata", {})
            typer.echo(f"Match for '{job_name}':")
            for key in ("team", "tier", "version"):
                if meta.get(key):
                    typer.echo(f"  {key}: {meta[key]}")
            if meta.get("labels"):
                typer.echo(f"  labels: {meta['labels']}")
        else:
            typer.echo(f"No rules matched '{job_name}'.")


# -- Config -------------------------------------------------------------------


@config_app.callback(invoke_without_command=True)
def config_callback(ctx: typer.Context):
    """Manage JJI configuration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_show)


@config_app.command("show")
def config_show():
    """Show current configuration."""
    config = load_config()
    if not config:
        typer.echo(f"No config file found at {CONFIG_FILE}")
        typer.echo(
            "\nCreate one with:\n"
            f"  mkdir -p {CONFIG_FILE.parent}\n"
            f"  cat > {CONFIG_FILE} << 'EOF'\n"
            "  [default]\n"
            '  server = "dev"\n\n'
            "  [servers.dev]\n"
            '  url = "http://localhost:8000"\n'
            '  username = "myuser"\n'
            "  EOF"
        )
        return

    default_name = get_default_server_name(config)
    typer.echo(f"Config file: {CONFIG_FILE}")
    typer.echo(f"Default server: {default_name or '(not set)'}")
    servers = list_servers(config)
    if servers:
        typer.echo(f"\nServers ({len(servers)}):")
        for name, cfg in servers.items():
            marker = " *" if name == default_name else ""
            ssl_note = " (no-verify-ssl)" if cfg.no_verify_ssl else ""
            user_note = f" user={cfg.username}" if cfg.username else ""
            typer.echo(f"  {name}{marker}: {cfg.url}{user_note}{ssl_note}")


@config_app.command("completion")
def config_completion(
    shell: str = typer.Argument("zsh", help="Shell type: bash or zsh"),
):
    """Show shell completion setup instructions."""
    if shell not in ("zsh", "bash"):
        typer.echo(f"Unsupported shell: {shell}. Use 'bash' or 'zsh'.")
        raise typer.Exit(1)

    rc_file = "~/.zshrc" if shell == "zsh" else "~/.bashrc"
    typer.echo(f"# Add to {rc_file}:")
    typer.echo("if command -v jji &> /dev/null; then")
    typer.echo(f'  eval "$(jji --show-completion {shell})"')
    typer.echo("fi")


@config_app.command("servers")
def config_servers(
    json_output: bool = _JSON_OPTION,
):
    """List configured servers."""
    _set_json(json_output)
    config = load_config()
    servers = list_servers(config)
    default_name = get_default_server_name(config)

    if _state.get("json", False):
        out: dict = {}
        for name, cfg in servers.items():
            out[name] = {
                "url": cfg.url,
                "username": cfg.username,
                "no_verify_ssl": cfg.no_verify_ssl,
                "default": name == default_name,
            }
        print_output(out, columns=[], as_json=True)
    else:
        if not servers:
            typer.echo("No servers configured.")
            return
        rows = []
        for name, cfg in servers.items():
            rows.append(
                {
                    "name": name,
                    "url": cfg.url,
                    "username": cfg.username or "",
                    "no_verify_ssl": str(cfg.no_verify_ssl),
                    "default": "*" if name == default_name else "",
                }
            )
        print_output(
            rows,
            columns=["name", "url", "username", "no_verify_ssl", "default"],
            labels={"no_verify_ssl": "NO VERIFY SSL"},
            as_json=False,
        )
