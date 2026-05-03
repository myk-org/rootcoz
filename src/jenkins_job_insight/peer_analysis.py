"""Peer analysis debate loop for multi-AI consensus.

Main AI analyzes first (same prompt as single-AI path). Peers review in parallel.
Loop until all agree or max rounds hit. No one has veto power — it's a conversation.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Coroutine, TypedDict

from ai_cli_runner import AIResult, run_parallel_with_limit
from simple_logger.logger import get_logger

from jenkins_job_insight.analyzer import (
    PROVIDER_CLI_FLAGS,
    _build_prompt_sections,
    _call_ai_cli_with_retry,
    _parse_json_response,
    _run_single_ai_analysis,
    _JSON_RESPONSE_SCHEMA,
)
from jenkins_job_insight.models import (
    AiConfigEntry,
    FailureAnalysis,
    PeerDebate,
    PeerRound,
    TestFailure,
)
from jenkins_job_insight.storage import update_progress_phase
from jenkins_job_insight.token_tracking import record_ai_usage

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


class PeerResponseSummary(TypedDict):
    ai_provider: str
    ai_model: str
    classification: str
    reasoning: str


async def _safe_update_progress(job_id: str | None, phase: str) -> None:
    """Best-effort progress update; failures are swallowed and logged."""
    if not job_id:
        return
    try:
        await update_progress_phase(job_id, phase)
    except Exception:
        logger.debug("Failed to update progress phase", exc_info=True)


_PEER_RESPONSE_SCHEMA = """CRITICAL: Your response must be ONLY a valid JSON object. No text before or after. No markdown code blocks. No explanation.
{
  "agrees": true or false,
  "classification": "CODE ISSUE" or "PRODUCT BUG",
  "reasoning": "your detailed reasoning for agreeing or disagreeing",
  "suggested_changes": "specific changes you'd suggest to the analysis (empty string if you agree)"
}"""

_VALID_CLASSIFICATIONS = frozenset({"CODE ISSUE", "PRODUCT BUG"})
_PEER_RESPONSE_KEYS = frozenset({"agrees", "classification", "reasoning"})


def _is_peer_response_dict(data: object) -> bool:
    """Check whether *data* contains ALL core peer-response keys with correct types."""
    if not isinstance(data, dict):
        return False
    return (
        _PEER_RESPONSE_KEYS.issubset(data)
        and isinstance(data.get("agrees"), bool)
        and isinstance(data.get("classification"), str)
        and isinstance(data.get("reasoning"), str)
    )


def _normalize_classification(cls: str) -> str:
    """Normalize a classification string for comparison.

    Strips whitespace and converts to uppercase so that case/whitespace
    differences do not break consensus checks.

    Args:
        cls: Raw classification string from an AI response.

    Returns:
        Uppercased, stripped classification string.
    """
    if not isinstance(cls, str):
        return ""
    return re.sub(r"\s+", " ", cls).strip().upper()


def _coerce_supported_classification(value: str) -> str:
    """Normalize a classification and return it only if valid.

    Returns the normalized value when it belongs to ``_VALID_CLASSIFICATIONS``,
    or an empty string otherwise.  This prevents malformed orchestrator outputs
    (e.g. ``"CODEISSUE"`` or ``"maybe product bug"``) from leaking into
    consensus or the final ``FailureAnalysis``.
    """
    normalized = _normalize_classification(value)
    return normalized if normalized in _VALID_CLASSIFICATIONS else ""


def _check_consensus(
    orchestrator_classification: str,
    peer_rounds: list[PeerRound],
) -> bool:
    """Check whether all valid peers agree with the orchestrator's classification.

    Only counts peers with ``agrees_with_orchestrator is not None``.
    Returns False if no valid peer votes exist.

    Args:
        orchestrator_classification: The main AI's current classification.
        peer_rounds: Peer round entries to evaluate.

    Returns:
        True if all valid peers agree, False otherwise.
    """
    valid_peers = [r for r in peer_rounds if r.agrees_with_orchestrator is not None]
    if not valid_peers:
        return False
    return all(
        _normalize_classification(r.classification)
        == _normalize_classification(orchestrator_classification)
        for r in valid_peers
    )


def _parse_peer_response(raw: str) -> dict:
    """Parse a peer's JSON response with fallback extraction.

    Three strategies are tried in order (direct parse, fenced code blocks,
    brace-delimited substrings). Each strategy only accepts a dict that
    contains ALL core peer response keys (``agrees``, ``classification``,
    and ``reasoning``) with correct types (bool, str, str respectively);
    dicts missing any of these keys or with wrong types are skipped so
    later strategies can try.

    On parse failure returns ``{"_failed": True, "raw": raw}`` so the peer
    is excluded from consensus.

    Args:
        raw: Raw text from the peer AI CLI call.

    Returns:
        Parsed dict with peer review fields, or a ``_failed`` marker dict.
    """
    _failed = {"_failed": True, "raw": raw}

    # Strategy 1: direct JSON parse
    try:
        data = json.loads(raw)
        if _is_peer_response_dict(data):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: extract from markdown code blocks (try all blocks)
    for block in re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL):
        try:
            data = json.loads(block.strip())
            if _is_peer_response_dict(data):
                return data
        except (json.JSONDecodeError, TypeError):
            continue

    # Strategy 3: try brace-delimited substrings, starting from the last '{'.
    # AI responses typically put the JSON object at the end, after prefatory text.
    # Starting from the end avoids false starts from shell variables like ${VAR}.
    # Use raw_decode() to tolerate trailing text after the JSON object.
    # Require ALL core keys with correct types to skip inner nested objects.
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"\{", raw))):
        try:
            data, _ = decoder.raw_decode(raw, match.start())
            if _is_peer_response_dict(data):
                return data
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return _failed


def _build_peer_review_prompt(
    failure_summary: str,
    orchestrator_analysis: str,
    custom_prompt: str,
    resources_section: str,
    other_peer_responses: list[PeerResponseSummary] | None = None,
) -> str:
    """Build the prompt for a peer to review the orchestrator's analysis.

    Includes AI-to-AI framing with anti-sycophancy instructions and
    data access (repo path, resources).

    Args:
        failure_summary: Summary of the failure (error and affected tests).
        orchestrator_analysis: The main AI's analysis text to review.
        custom_prompt: Additional user instructions, if any.
        resources_section: Available resources (repo, tools) for the peer.
        other_peer_responses: Previous round responses from other peers
            (excluding the current peer), typed as ``PeerResponseSummary``.
            None or empty list means no prior peer input (round 1).

    Returns:
        Formatted peer review prompt string.
    """
    custom_section = (
        f"\n\nADDITIONAL INSTRUCTIONS:\n{custom_prompt}\n" if custom_prompt else ""
    )

    other_peers_section = ""
    if other_peer_responses:
        lines = []
        for resp in other_peer_responses:
            lines.append(
                f"PEER ({resp['ai_provider']}/{resp['ai_model']}):\n"
                f"  Classification: {resp['classification']}\n"
                f"  Response: {resp['reasoning']}"
            )
        other_peers_section = (
            "\n\nOTHER PEER RESPONSES FROM PREVIOUS ROUND:\n"
            + "\n\n".join(lines)
            + "\n\nConsider their perspectives but form your own independent opinion.\n"
        )

    return f"""IMPORTANT: This is an AI-only conversation. Do NOT be agreeable or sycophantic. \
Critically evaluate the analysis below and provide your honest, independent assessment. \
Challenge any conclusions you disagree with.

FAILURE SUMMARY:
{failure_summary}

ORCHESTRATOR'S ANALYSIS:
{orchestrator_analysis}
{other_peers_section}
Your task: Review the orchestrator's analysis above. Do you agree with the classification \
and reasoning? If not, explain why and suggest corrections.
{custom_section}{resources_section}
{_PEER_RESPONSE_SCHEMA}
"""


def _build_revision_prompt(
    failure_summary: str,
    current_analysis: str,
    peer_feedback: str,
    custom_prompt: str,
    resources_section: str,
) -> str:
    """Build a prompt for the main AI to revise its analysis based on peer feedback.

    Args:
        failure_summary: Summary of the failure.
        current_analysis: The main AI's current analysis.
        peer_feedback: Collected feedback from all peers.
        custom_prompt: Additional user instructions.
        resources_section: Available resources for the AI.

    Returns:
        Formatted revision prompt string.
    """
    custom_section = (
        f"\n\nADDITIONAL INSTRUCTIONS:\n{custom_prompt}\n" if custom_prompt else ""
    )

    return f"""IMPORTANT: This is an AI-only conversation. Do NOT be agreeable or sycophantic. \
You are revising your analysis based on peer feedback. Consider the feedback carefully, \
but only change your assessment if the arguments are convincing.

FAILURE SUMMARY:
{failure_summary}

YOUR CURRENT ANALYSIS:
{current_analysis}

PEER FEEDBACK:
{peer_feedback}

Revise your analysis considering the peer feedback above. You may keep your original \
classification if you believe the peers are wrong — justify your reasoning.
{custom_section}{resources_section}
{_JSON_RESPONSE_SCHEMA}
"""


def _build_failure_summary(
    failures: list[TestFailure],
    error_signature: str,
) -> str:
    """Build a concise failure summary for peer prompts.

    Peers get the summary — not raw console dumps. They have data access
    via resources_section if they need to dig deeper.

    Args:
        failures: The failure group.
        error_signature: SHA-256 hash of the failure signature.

    Returns:
        Formatted failure summary string.
    """
    representative = failures[0]
    test_names = [f.test_name for f in failures]
    return (
        f"ERROR SIGNATURE: {error_signature}\n"
        f"AFFECTED TESTS ({len(failures)} tests with same error):\n"
        + "\n".join(f"- {name}" for name in test_names)
        + f"\n\nERROR: {representative.error_message}"
    )


async def analyze_failure_group_with_peers(
    failures: list[TestFailure],
    console_context: str,
    repo_path: Path | None,
    main_ai_provider: str,
    main_ai_model: str,
    peer_ai_configs: list[AiConfigEntry],
    max_rounds: int = 3,
    ai_cli_timeout: int | None = None,
    custom_prompt: str = "",
    artifacts_context: str = "",
    server_url: str = "",
    job_id: str = "",
    group_label: str = "",
    additional_repos: dict[str, Path] | None = None,
    max_concurrent_ai_calls: int = 3,
) -> list[FailureAnalysis]:
    """Analyze a failure group using multi-AI peer consensus.

    The main AI analyzes first (identical prompt to the single-AI path),
    then peers review in parallel. The loop continues until consensus
    is reached or max_rounds is exhausted.

    From round 2 onwards, each peer sees the other peers' responses from the
    previous round (excluding its own), enabling richer group debate.

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to cloned test repo (optional).
        main_ai_provider: AI provider for the main/orchestrator analysis.
        main_ai_model: AI model for the main/orchestrator analysis.
        peer_ai_configs: List of peer AI configurations.
        max_rounds: Maximum debate rounds before accepting main AI result.
        ai_cli_timeout: Timeout in minutes for AI CLI calls.
        custom_prompt: Additional user instructions.
        artifacts_context: Jenkins artifacts context.
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        group_label: Human-readable label identifying which failure group is
            being analyzed (e.g. ``"2/3"`` for group 2 of 3). Used in progress
            phase names to disambiguate concurrent groups.
        additional_repos: Extra cloned repositories for AI context.
        max_concurrent_ai_calls: Maximum concurrent AI CLI processes for
            peer analysis parallelism (default: 3).

    Returns:
        List of FailureAnalysis objects, one per failure in the group.
    """
    # Step 1: Main AI analyzes (shared helper — same prompt as single-AI path)
    logger.info(
        f"Peer analysis: calling main AI ({main_ai_provider}/{main_ai_model}) "
        f"for failure group ({len(failures)} tests)"
    )
    parsed_analysis, error_signature = await _run_single_ai_analysis(
        failures=failures,
        console_context=console_context,
        repo_path=repo_path,
        ai_provider=main_ai_provider,
        ai_model=main_ai_model,
        ai_cli_timeout=ai_cli_timeout,
        custom_prompt=custom_prompt,
        artifacts_context=artifacts_context,
        server_url=server_url,
        job_id=job_id,
        additional_repos=additional_repos,
    )

    # Validate orchestrator classification before feeding into consensus
    normalized_main = _coerce_supported_classification(parsed_analysis.classification)
    if normalized_main:
        parsed_analysis = parsed_analysis.model_copy(
            update={"classification": normalized_main}
        )
    elif parsed_analysis.classification:
        logger.warning(
            f"Main AI returned invalid classification: {parsed_analysis.classification!r}"
        )
        parsed_analysis = parsed_analysis.model_copy(update={"classification": ""})

    # Build failure summary and resources section for peer prompts
    failure_summary = _build_failure_summary(failures, error_signature)
    _, _, resources_section, _ = _build_prompt_sections(
        custom_prompt,
        artifacts_context,
        repo_path,
        server_url,
        job_id,
        additional_repos=additional_repos,
    )
    all_rounds: list[PeerRound] = []
    consensus_reached = False
    rounds_used = 0
    group_suffix = f" (group {group_label})" if group_label else ""

    # Step 2: Debate loop
    for round_num in range(1, max_rounds + 1):
        rounds_used = round_num
        logger.info(f"Peer analysis: starting debate round {round_num}/{max_rounds}")

        await _safe_update_progress(
            job_id, f"peer_review_round_{round_num}{group_suffix}"
        )

        # Build orchestrator analysis text for peers
        orchestrator_analysis_text = (
            f"Classification: {parsed_analysis.classification}\n"
            f"Details: {parsed_analysis.details}"
        )

        # Record orchestrator entry for this round
        all_rounds.append(
            PeerRound(
                round=round_num,
                ai_provider=main_ai_provider,
                ai_model=main_ai_model,
                role="orchestrator",
                classification=parsed_analysis.classification,
                details=parsed_analysis.details,
                agrees_with_orchestrator=True,
            )
        )

        # Collect previous round peer data for cross-peer visibility.
        # Build a mapping from peer_ai_configs index to PeerResponseSummary
        # (None for peers that failed).  Peer entries in all_rounds appear in
        # the same order as peer_ai_configs because run_parallel_with_limit
        # preserves input order, so we can zip them by position.
        prev_round_by_idx: dict[int, PeerResponseSummary] = {}
        if round_num > 1:
            prev_round_entries = [
                r for r in all_rounds if r.round == round_num - 1 and r.role == "peer"
            ]
            for peer_idx, entry in enumerate(prev_round_entries):
                if entry.agrees_with_orchestrator is not None:
                    prev_round_by_idx[peer_idx] = PeerResponseSummary(
                        ai_provider=entry.ai_provider,
                        ai_model=entry.ai_model,
                        classification=entry.classification,
                        reasoning=entry.details,
                    )

        # Build per-peer prompts (each peer sees others' responses, excluding self by index)
        peer_prompts: dict[int, str] = {}
        for idx, _cfg in enumerate(peer_ai_configs):
            other_responses: list[PeerResponseSummary] = [
                resp for peer_idx, resp in prev_round_by_idx.items() if peer_idx != idx
            ]
            peer_prompts[idx] = _build_peer_review_prompt(
                failure_summary=failure_summary,
                orchestrator_analysis=orchestrator_analysis_text,
                custom_prompt=custom_prompt,
                resources_section=resources_section,
                other_peer_responses=other_responses if other_responses else None,
            )

        async def _call_peer(
            idx: int,
            config: AiConfigEntry,
        ) -> tuple[AiConfigEntry, AIResult]:
            prompt = peer_prompts[idx]
            ai_result = await _call_ai_cli_with_retry(
                prompt,
                cwd=repo_path,
                ai_provider=config.ai_provider,
                ai_model=config.ai_model,
                ai_cli_timeout=ai_cli_timeout,
                cli_flags=PROVIDER_CLI_FLAGS.get(config.ai_provider, []),
            )
            await record_ai_usage(
                job_id=job_id,
                result=ai_result,
                call_type="peer",
                prompt_chars=len(prompt),
                ai_provider=config.ai_provider,
                ai_model=config.ai_model,
            )
            return config, ai_result

        peer_tasks: list[Coroutine[Any, Any, Any]] = [
            _call_peer(idx, cfg) for idx, cfg in enumerate(peer_ai_configs)
        ]
        peer_results = await run_parallel_with_limit(
            peer_tasks, max_concurrency=max_concurrent_ai_calls
        )

        # Process peer responses
        round_peer_entries: list[PeerRound] = []
        for i, result in enumerate(peer_results):
            if isinstance(result, Exception):
                exc_config = peer_ai_configs[i]
                logger.warning(
                    f"Peer {exc_config.ai_provider}/{exc_config.ai_model} "
                    f"raised exception: {result}"
                )
                entry = PeerRound(
                    round=round_num,
                    ai_provider=exc_config.ai_provider,
                    ai_model=exc_config.ai_model,
                    role="peer",
                    classification="",
                    details=str(result),
                    agrees_with_orchestrator=None,
                )
                round_peer_entries.append(entry)
                all_rounds.append(entry)
                continue
            config, ai_result = result
            if not ai_result.success:
                logger.warning(
                    f"Peer {config.ai_provider}/{config.ai_model} CLI failed: "
                    f"{ai_result.text if ai_result.text else 'no output'}"
                )
                entry = PeerRound(
                    round=round_num,
                    ai_provider=config.ai_provider,
                    ai_model=config.ai_model,
                    role="peer",
                    classification="",
                    details=ai_result.text,
                    agrees_with_orchestrator=None,
                )
            else:
                peer_data = _parse_peer_response(ai_result.text)
                if peer_data.get("_failed"):
                    logger.warning(
                        f"Peer {config.ai_provider}/{config.ai_model} returned "
                        f"unparseable response"
                    )
                    entry = PeerRound(
                        round=round_num,
                        ai_provider=config.ai_provider,
                        ai_model=config.ai_model,
                        role="peer",
                        classification="",
                        details=ai_result.text,
                        agrees_with_orchestrator=None,
                    )
                else:
                    raw_peer_classification = peer_data.get("classification", "")
                    peer_classification = (
                        raw_peer_classification
                        if isinstance(raw_peer_classification, str)
                        else ""
                    )
                    peer_reasoning = str(peer_data.get("reasoning", "") or "")
                    peer_suggested_changes = str(
                        peer_data.get("suggested_changes", "") or ""
                    )
                    peer_details = peer_reasoning
                    if peer_suggested_changes:
                        peer_details = (
                            f"{peer_reasoning}\n\nSuggested changes:\n{peer_suggested_changes}"
                            if peer_reasoning
                            else f"Suggested changes:\n{peer_suggested_changes}"
                        )
                    normalized = _normalize_classification(peer_classification)
                    if normalized not in _VALID_CLASSIFICATIONS:
                        # Invalid classification -- exclude from consensus
                        logger.warning(
                            f"Peer {config.ai_provider}/{config.ai_model} returned "
                            f"invalid classification: {raw_peer_classification!r}"
                        )
                        entry = PeerRound(
                            round=round_num,
                            ai_provider=config.ai_provider,
                            ai_model=config.ai_model,
                            role="peer",
                            classification=peer_classification,
                            details=peer_details,
                            agrees_with_orchestrator=None,
                        )
                    else:
                        # Derive agreement from normalized classification match
                        agrees = normalized == _normalize_classification(
                            parsed_analysis.classification
                        )
                        entry = PeerRound(
                            round=round_num,
                            ai_provider=config.ai_provider,
                            ai_model=config.ai_model,
                            role="peer",
                            classification=normalized,
                            details=peer_details,
                            agrees_with_orchestrator=agrees,
                        )
            round_peer_entries.append(entry)
            all_rounds.append(entry)

        # Check if all peers failed this round
        if all(r.agrees_with_orchestrator is None for r in round_peer_entries):
            logger.warning(
                f"All peers failed in round {round_num}; using main AI result"
            )
            break

        # Check consensus
        orchestrator_classification = parsed_analysis.classification
        if _check_consensus(orchestrator_classification, round_peer_entries):
            logger.info(f"Peer analysis: consensus reached in round {round_num}")
            consensus_reached = True
            break

        # No consensus and more rounds available -> main AI revises
        if round_num < max_rounds:
            logger.info(f"No consensus in round {round_num}; main AI revising analysis")

            await _safe_update_progress(
                job_id, f"orchestrator_revising_round_{round_num}{group_suffix}"
            )
            # Collect peer feedback
            feedback_parts = []
            for entry in round_peer_entries:
                if entry.agrees_with_orchestrator is not None:
                    feedback_parts.append(
                        f"Peer ({entry.ai_provider}/{entry.ai_model}):\n"
                        f"  Agrees: {entry.agrees_with_orchestrator}\n"
                        f"  Classification: {entry.classification}\n"
                        f"  Reasoning: {entry.details}"
                    )
            peer_feedback = "\n\n".join(feedback_parts)

            revision_prompt = _build_revision_prompt(
                failure_summary=failure_summary,
                current_analysis=orchestrator_analysis_text,
                peer_feedback=peer_feedback,
                custom_prompt=custom_prompt,
                resources_section=resources_section,
            )

            previous_analysis = parsed_analysis
            try:
                rev_result = await _call_ai_cli_with_retry(
                    revision_prompt,
                    cwd=repo_path,
                    ai_provider=main_ai_provider,
                    ai_model=main_ai_model,
                    ai_cli_timeout=ai_cli_timeout,
                    cli_flags=PROVIDER_CLI_FLAGS.get(main_ai_provider, []),
                )
                await record_ai_usage(
                    job_id=job_id,
                    result=rev_result,
                    call_type="revision",
                    prompt_chars=len(revision_prompt),
                    ai_provider=main_ai_provider,
                    ai_model=main_ai_model,
                )
            except Exception as exc:
                logger.warning(
                    f"Revision round {round_num} raised {type(exc).__name__}: {exc}; keeping prior analysis"
                )
                parsed_analysis = previous_analysis
                continue

            if rev_result.success:
                revised = _parse_json_response(rev_result.text)
                normalized_revised = _coerce_supported_classification(
                    revised.classification
                )
                if normalized_revised:
                    revised = revised.model_copy(
                        update={"classification": normalized_revised}
                    )
                    # Merge forward: when revision keeps the same classification
                    # but drops structured fields, preserve non-empty fields from
                    # the prior analysis so a partial revision doesn't erase a
                    # richer earlier result.
                    if _normalize_classification(
                        revised.classification
                    ) == _normalize_classification(previous_analysis.classification):
                        _merge_fields = (
                            "details",
                            "artifacts_evidence",
                            "code_fix",
                            "product_bug_report",
                        )
                        updates: dict = {}
                        for field in _merge_fields:
                            revised_val = getattr(revised, field)
                            prev_val = getattr(previous_analysis, field)
                            # Keep previous value when revised dropped it
                            if not revised_val and prev_val:
                                updates[field] = prev_val
                        if updates:
                            revised = revised.model_copy(update=updates)
                    parsed_analysis = revised
                elif revised.classification:
                    logger.warning(
                        f"Revision round {round_num} returned invalid classification: "
                        f"{revised.classification!r}; keeping prior analysis"
                    )
                    parsed_analysis = previous_analysis
                else:
                    logger.warning(
                        f"Revision round {round_num} returned no classification; keeping prior analysis"
                    )
                    parsed_analysis = previous_analysis
            else:
                logger.warning(
                    f"Revision round {round_num} failed; keeping prior analysis"
                )
                parsed_analysis = previous_analysis

    # Build PeerDebate trail
    peer_debate = PeerDebate(
        consensus_reached=consensus_reached,
        rounds_used=rounds_used,
        max_rounds=max_rounds,
        ai_configs=[
            AiConfigEntry(
                ai_provider=main_ai_provider,  # type: ignore[arg-type]
                ai_model=main_ai_model,
            ),
            *peer_ai_configs,
        ],
        rounds=all_rounds,
    )

    # Apply analysis to all failures in the group.
    # All failures share the same signature (that's how they were grouped),
    # so reuse the already-computed value instead of calling get_failure_signature() again.
    return [
        FailureAnalysis(
            test_name=f.test_name,
            error=f.error_message,
            analysis=parsed_analysis,
            error_signature=error_signature,
            peer_debate=peer_debate,
        )
        for f in failures
    ]
