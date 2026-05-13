"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

from pathlib import Path

from simple_logger.logger import get_logger

from rootcoz.engine.core import (
    PROVIDER_CLI_FLAGS,
    call_ai_and_record,
)

logger = get_logger(name=__name__)


def build_system_prompt(
    result_data: dict,
    job_id: str,
    server_url: str,
    auth_header: str = "",
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job.

    The prompt provides:
    - Job metadata (name, build number, status, summary)
    - List of failures with test_name and UUID for reference
    - API endpoints the AI can use to query details
    - Instructions constraining the AI to read-only, job-scoped behavior
    """
    job_name = result_data.get("job_name", "unknown")
    build_number = result_data.get("build_number", 0)
    summary = result_data.get("summary", "")
    ai_provider = result_data.get("ai_provider", "")
    ai_model = result_data.get("ai_model", "")
    jenkins_url = result_data.get("jenkins_url", "")

    # Build failure reference list
    failure_lines = []
    for i, f in enumerate(result_data.get("failures", []), 1):
        fid = f.get("id", "?")
        name = f.get("test_name", "unknown")
        classification = f.get("analysis", {}).get("classification", "?")
        failure_lines.append(f"  {i}. {name} (UUID: {fid}) — {classification}")

    # Include child job failures
    for child in result_data.get("child_job_analyses", []):
        child_name = child.get("job_name", "?")
        child_num = child.get("build_number", 0)
        for f in child.get("failures", []):
            fid = f.get("id", "?")
            name = f.get("test_name", "unknown")
            classification = f.get("analysis", {}).get("classification", "?")
            failure_lines.append(
                f"  - [{child_name}#{child_num}] {name} (UUID: {fid}) — {classification}"
            )
        # Recurse one level into failed_children
        for nested in child.get("failed_children", []):
            nested_name = nested.get("job_name", "?")
            nested_num = nested.get("build_number", 0)
            for f in nested.get("failures", []):
                fid = f.get("id", "?")
                name = f.get("test_name", "unknown")
                classification = f.get("analysis", {}).get("classification", "?")
                failure_lines.append(
                    f"  - [{nested_name}#{nested_num}] {name} (UUID: {fid}) — {classification}"
                )

    failures_section = "\n".join(failure_lines) if failure_lines else "  (no failures)"

    # API endpoints the AI can use
    api_section = ""
    if server_url:
        auth_flag = f" -H 'Authorization: {auth_header}'" if auth_header else ""
        api_section = f"""
## Available API Endpoints (read-only)
You can use curl to query these endpoints for more details:
- `curl{auth_flag} {server_url}/api/results/{job_id}` — Full job result with all failure analyses
- `curl{auth_flag} {server_url}/api/results/{job_id}/comments` — Get comments on this job
"""

    return f"""You are a read-only assistant helping a user understand a CI/CD failure analysis.

## Scope
You MUST only discuss this specific analyzed job. Do NOT answer general questions unrelated to this job.
You MUST NOT modify any data, create issues, or perform any mutations.
You are here to help the user understand the analysis results, explore failure details, and suggest next steps.

## Job Context
- **Job:** {job_name} #{build_number}
- **Job ID:** {job_id}
- **Summary:** {summary}
- **AI Provider/Model:** {ai_provider} / {ai_model}
- **Jenkins URL:** {jenkins_url or "N/A"}

## Failures in this job
{failures_section}

When the user references a test, use the UUID to look up its details.
{api_section}
## Instructions
- Answer questions about the failures, their root causes, classifications, and suggested fixes
- If the user asks about a specific test, use the UUID to reference it
- You can explore the cloned repository (if available in your working directory) to understand test code
- Be concise and technical
- If you don't have enough information, say so and suggest what data would help
"""


def build_chat_prompt(
    system_prompt: str,
    history: list[dict],
    new_message: str,
) -> str:
    """Build a complete prompt from system prompt + conversation history + new message.

    Since AI CLI is stateless (no conversation mode), we build the full
    context each time by concatenating system prompt + history + new message.
    """
    parts = [system_prompt, "\n## Conversation History\n"]

    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            parts.append(f"**User:** {content}\n")
        else:
            parts.append(f"**Assistant:** {content}\n")

    parts.append(f"**User:** {new_message}\n")
    parts.append("\n**Assistant:** ")

    return "\n".join(parts)


async def chat_with_ai(
    *,
    job_id: str,
    result_data: dict,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    server_url: str = "",
    repo_path: Path | None = None,
    ai_cli_timeout: int | None = None,
    auth_header: str = "",
) -> tuple[bool, str]:
    """Send a chat message and get an AI response.

    Args:
        job_id: The analyzed job ID.
        result_data: The job's result data (stripped of sensitive fields).
        message: The user's new message.
        history: Previous chat messages (list of dicts with role/content).
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        server_url: Internal server URL for AI to query APIs.
        repo_path: Path to cloned repos (if available).
        ai_cli_timeout: Timeout for AI CLI call.
        auth_header: Bearer auth header for AI to use when curling API.

    Returns:
        Tuple of (success, response_text).
    """
    system_prompt = build_system_prompt(
        result_data, job_id, server_url, auth_header=auth_header
    )
    full_prompt = build_chat_prompt(system_prompt, history, message)

    logger.info(
        f"Chat message for job {job_id}: {len(message)} chars, "
        f"history: {len(history)} messages, prompt: {len(full_prompt)} chars"
    )

    result, _ = await call_ai_and_record(
        full_prompt,
        job_id=job_id,
        call_type="chat",
        cwd=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
    )

    if not result.success:
        logger.error(f"Chat AI call failed for job {job_id}: {result.text}")
        return False, result.text

    return True, result.text
