"""Tests for peer analysis debate loop module."""

import json
from unittest.mock import patch

import pytest

from ai_cli_runner import AIResult
from jenkins_job_insight.models import AiConfigEntry, TestFailure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_failure(
    test_name: str = "com.example.TestClass.testMethod",
    error_message: str = "AssertionError: expected true",
    stack_trace: str = "at com.example.TestClass.testMethod(TestClass.java:42)",
) -> TestFailure:
    return TestFailure(
        test_name=test_name,
        error_message=error_message,
        stack_trace=stack_trace,
    )


async def _run_peer_analysis(
    monkeypatch,
    cli_side_effect,
    peer_configs=None,
    max_rounds=3,
    job_id="test-job",
    group_label="",
):
    """Helper to run analyze_failure_group_with_peers with mocked CLI."""
    from unittest.mock import AsyncMock

    from jenkins_job_insight.models import AiConfigEntry, TestFailure
    from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

    monkeypatch.setattr(
        "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry", cli_side_effect
    )
    monkeypatch.setattr(
        "jenkins_job_insight.analyzer._call_ai_cli_with_retry", cli_side_effect
    )
    monkeypatch.setattr(
        "jenkins_job_insight.peer_analysis.update_progress_phase", AsyncMock()
    )

    if peer_configs is None:
        peer_configs = [AiConfigEntry(ai_provider="gemini", ai_model="pro")]

    return await analyze_failure_group_with_peers(
        failures=[
            TestFailure(test_name="test_foo", error_message="err", stack_trace="trace")
        ],
        console_context="console output",
        repo_path=None,
        main_ai_provider="claude",
        main_ai_model="opus",
        peer_ai_configs=peer_configs,
        max_rounds=max_rounds,
        job_id=job_id,
        group_label=group_label,
    )


def _peer_configs() -> list[AiConfigEntry]:
    return [
        AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
        AiConfigEntry(ai_provider="claude", ai_model="claude-sonnet-4-20250514"),
    ]


def _make_ai_json_response(
    classification: str = "CODE ISSUE",
    details: str = "Test is broken",
) -> str:
    return json.dumps(
        {
            "classification": classification,
            "affected_tests": ["com.example.TestClass.testMethod"],
            "details": details,
            "code_fix": {
                "file": "src/TestClass.java",
                "line": "42",
                "change": "fix the assertion",
            },
        }
    )


def _make_peer_json_response(
    agrees: bool = True,
    classification: str = "CODE ISSUE",
    reasoning: str = "I agree with the analysis",
    suggested_changes: str = "",
) -> str:
    return json.dumps(
        {
            "agrees": agrees,
            "classification": classification,
            "reasoning": reasoning,
            "suggested_changes": suggested_changes,
        }
    )


# ===========================================================================
# _check_consensus tests
# ===========================================================================


class TestCheckConsensus:
    def test_check_consensus_all_agree(self) -> None:
        """All non-failed peers agree with orchestrator -> True."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="CODE ISSUE",
                details="agree",
                agrees_with_orchestrator=True,
            ),
            PeerRound(
                round=1,
                ai_provider="claude",
                ai_model="claude-sonnet-4-20250514",
                role="peer",
                classification="CODE ISSUE",
                details="also agree",
                agrees_with_orchestrator=True,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is True

    def test_check_consensus_disagreement(self) -> None:
        """At least one peer disagrees -> False."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="CODE ISSUE",
                details="agree",
                agrees_with_orchestrator=True,
            ),
            PeerRound(
                round=1,
                ai_provider="claude",
                ai_model="claude-sonnet-4-20250514",
                role="peer",
                classification="PRODUCT BUG",
                details="disagree",
                agrees_with_orchestrator=False,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is False

    def test_check_consensus_no_valid_peers(self) -> None:
        """All peers failed (agrees=None) -> False."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="",
                details="failed",
                agrees_with_orchestrator=None,
            ),
            PeerRound(
                round=1,
                ai_provider="claude",
                ai_model="claude-sonnet-4-20250514",
                role="peer",
                classification="",
                details="also failed",
                agrees_with_orchestrator=None,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is False

    def test_check_consensus_failed_peer_excluded(self) -> None:
        """One peer failed (None), remaining peer agrees -> True."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="",
                details="failed",
                agrees_with_orchestrator=None,
            ),
            PeerRound(
                round=1,
                ai_provider="claude",
                ai_model="claude-sonnet-4-20250514",
                role="peer",
                classification="CODE ISSUE",
                details="agree",
                agrees_with_orchestrator=True,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is True

    def test_check_consensus_derives_from_classification_match(self) -> None:
        """Consensus is derived from classification match, not self-reported agrees field.

        A peer that self-reports agrees=True but has a different classification
        should NOT count as consensus.
        """
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        # Peer says agrees=True but classification differs from orchestrator
        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="PRODUCT BUG",
                details="I agree",
                agrees_with_orchestrator=True,  # self-reported, should be ignored
            ),
        ]
        # Consensus should be False because classification != orchestrator's "CODE ISSUE"
        assert _check_consensus("CODE ISSUE", rounds) is False

    def test_check_consensus_classification_match_overrides_disagrees(self) -> None:
        """Peer that says disagrees but has matching classification -> consensus True."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="CODE ISSUE",
                details="I disagree but",
                agrees_with_orchestrator=False,  # self-reported, should be ignored
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is True

    def test_check_consensus_case_insensitive(self) -> None:
        """Classification comparison is case-insensitive."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="code issue",
                details="agree",
                agrees_with_orchestrator=True,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is True

    def test_check_consensus_whitespace_tolerance(self) -> None:
        """Classification comparison tolerates leading/trailing whitespace."""
        from jenkins_job_insight.peer_analysis import _check_consensus
        from jenkins_job_insight.models import PeerRound

        rounds = [
            PeerRound(
                round=1,
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                role="peer",
                classification="  CODE ISSUE  ",
                details="agree",
                agrees_with_orchestrator=True,
            ),
        ]
        assert _check_consensus("CODE ISSUE", rounds) is True


# ===========================================================================
# _normalize_classification tests
# ===========================================================================


class TestNormalizeClassification:
    def test_normalizes_lowercase(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification("code issue") == "CODE ISSUE"

    def test_normalizes_mixed_case(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification("Product Bug") == "PRODUCT BUG"

    def test_strips_whitespace(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification("  CODE ISSUE  ") == "CODE ISSUE"

    def test_already_normalized(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification("PRODUCT BUG") == "PRODUCT BUG"

    def test_collapses_internal_whitespace(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification("CODE   ISSUE") == "CODE ISSUE"

    def test_handles_none_input(self) -> None:
        from jenkins_job_insight.peer_analysis import _normalize_classification

        assert _normalize_classification(None) == ""


# ===========================================================================
# _parse_peer_response tests
# ===========================================================================


class TestParsePeerResponse:
    def test_parse_peer_response_valid_json(self) -> None:
        """Valid JSON string is parsed correctly."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        raw = _make_peer_json_response(agrees=True, classification="CODE ISSUE")
        result = _parse_peer_response(raw)
        assert result["agrees"] is True
        assert result["classification"] == "CODE ISSUE"
        assert "_failed" not in result

    def test_parse_peer_response_json_in_text(self) -> None:
        """JSON embedded in markdown code block is extracted."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        raw = f"Here is my analysis:\n```json\n{_make_peer_json_response()}\n```\n"
        result = _parse_peer_response(raw)
        assert result["agrees"] is True
        assert "_failed" not in result

    def test_parse_peer_response_unparseable(self) -> None:
        """Completely unparseable text returns _failed marker."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        result = _parse_peer_response("I cannot provide JSON output sorry")
        assert result["_failed"] is True

    @pytest.mark.parametrize(
        "raw_json,description",
        [
            ('"just a string"', "JSON string"),
            ("[1, 2, 3]", "JSON array"),
            ("42", "JSON number"),
            ("true", "JSON boolean"),
            ("null", "JSON null"),
        ],
    )
    def test_parse_peer_response_non_dict_json_returns_failed(
        self, raw_json: str, description: str
    ) -> None:
        """Non-dict JSON (string, array, number, etc.) returns _failed marker."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        result = _parse_peer_response(raw_json)
        assert result.get("_failed") is True, (
            f"Expected _failed for {description}: {raw_json}"
        )

    def test_parse_peer_response_non_dict_in_code_block(self) -> None:
        """Non-dict JSON inside markdown code block returns _failed marker."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        raw = '```json\n["not", "a", "dict"]\n```'
        result = _parse_peer_response(raw)
        assert result.get("_failed") is True

    def test_parse_peer_response_non_dict_brace_extraction(self) -> None:
        """When brace extraction yields a valid dict, it should succeed."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        # This has leading text but valid JSON dict inside
        raw = f"Some preamble text {_make_peer_json_response()}"
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is True

    def test_parse_peer_response_json_in_second_code_block(self) -> None:
        """JSON dict in a later fenced block is found when the first block is non-dict JSON."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        valid_json = _make_peer_json_response(
            agrees=False, classification="PRODUCT BUG"
        )
        # First code block contains valid JSON but not a dict (an array).
        # Strategy 2 tries each fenced block in order, skipping non-dict
        # blocks, so the valid dict in the second block is found.
        raw = (
            "Here is my thinking:\n"
            '```json\n[{"step": "analysis"}, {"step": "review"}]\n```\n'
            "And here is my answer:\n"
            f"```json\n{valid_json}\n```\n"
        )
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is False
        assert result["classification"] == "PRODUCT BUG"

    @pytest.mark.parametrize(
        "preamble,agrees,classification,reasoning,suggested_changes",
        [
            pytest.param(
                (
                    "I've verified the key claims in the source code:\n"
                    "1. `infraUtils.groovy` line 39: `cp -fv ${JIRA_CFG_FILE} "
                    "${WORKSPACE}/mtv-api-tests/jira.cfg` -- confirmed\n"
                    "2. `pyproject.toml` line 59: `pytest-jira>=0.3.23` is a "
                    "dependency -- confirmed\n\n"
                ),
                True,
                "CODE ISSUE",
                "The analysis is correct",
                "",
                id="shell_variables_in_preamble",
            ),
            pytest.param(
                (
                    "Checking the pipeline script:\n"
                    "- Line 12: `echo ${BUILD_NUMBER}`\n"
                    "- Line 45: `def config = ${env.CONFIG_MAP}`\n"
                    '- Line 78: `sh "curl -X POST ${API_URL}/v2/results"`\n\n'
                    "All references verified.\n\n"
                ),
                False,
                "PRODUCT BUG",
                "The root cause is in the product",
                "Fix the API endpoint",
                id="multiple_braces_in_preamble",
            ),
            pytest.param(
                (
                    "I've carefully reviewed the orchestrator's analysis and the "
                    "relevant source code.\n\n"
                    "Key observations:\n"
                    "1. The test `test_network_timeout` asserts a 30s timeout\n"
                    "2. The config in `${PROJECT_ROOT}/settings.yaml` sets it to 60s\n"
                    '3. The Jenkinsfile runs: `sh "export TIMEOUT=${DEFAULT_TIMEOUT}"` '
                    "on line 142\n"
                    "4. The `Makefile` target uses `${CURDIR}/bin/run-tests`\n\n"
                    "Given these findings, the classification is accurate.\n\n"
                ),
                True,
                "CODE ISSUE",
                "Timeout mismatch between config and test assertion",
                "Update the test to expect 60s",
                id="json_at_end_after_text",
            ),
        ],
    )
    def test_parse_peer_response_preamble_with_braces(
        self, preamble, agrees, classification, reasoning, suggested_changes
    ) -> None:
        """JSON is correctly extracted when prefatory text contains curly braces."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        raw = preamble + _make_peer_json_response(
            agrees=agrees,
            classification=classification,
            reasoning=reasoning,
            suggested_changes=suggested_changes,
        )
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is agrees
        assert result["classification"] == classification
        assert result["reasoning"] == reasoning

    def test_parse_peer_response_trailing_text_after_json(self) -> None:
        """JSON followed by trailing text is still parsed correctly."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        raw = (
            _make_peer_json_response(
                agrees=True,
                classification="CODE ISSUE",
                reasoning="Test is broken",
            )
            + "\n\nHope this helps! Let me know if you need more details."
        )
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is True
        assert result["classification"] == "CODE ISSUE"

    def test_parse_peer_response_nested_object_not_returned(self) -> None:
        """Inner nested objects are skipped; the root peer response is returned."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        # JSON with a nested object — iterating from last '{' should NOT
        # return the inner {"nested": true} dict.
        raw = (
            "Some preamble text\n"
            '{"agrees": true, "classification": "CODE ISSUE", '
            '"reasoning": "Valid analysis", "extra": {"nested": true}}'
        )
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is True
        assert result["classification"] == "CODE ISSUE"
        assert result["reasoning"] == "Valid analysis"

    def test_parse_peer_response_wrong_shape_dict_falls_through(self) -> None:
        """Top-level JSON dict without peer keys falls through to later strategies."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        # The AI wraps its response in extra metadata — Strategy 1 should
        # reject the top-level dict (no peer keys) and Strategy 3 should
        # recover the actual peer response embedded inside.
        inner = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="Correct analysis",
        )
        raw = f'{{"metadata": "wrapper", "response": {inner}}}'
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is True
        assert result["classification"] == "CODE ISSUE"

    def test_parse_peer_response_wrapper_with_peer_key_falls_through(self) -> None:
        """Wrapper dict with a top-level peer key does not short-circuit parsing."""
        from jenkins_job_insight.peer_analysis import _parse_peer_response

        # A wrapper that has 'classification' at top level but the real peer
        # payload is nested under 'response'. Strategy 1 should reject the
        # outer dict (missing agrees/reasoning) and Strategy 3 should recover
        # the inner peer response.
        inner = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="Correct analysis",
        )
        raw = f'{{"classification": "CODE ISSUE", "response": {inner}}}'
        result = _parse_peer_response(raw)
        assert "_failed" not in result
        assert result["agrees"] is True
        assert result["classification"] == "CODE ISSUE"
        assert result["reasoning"] == "Correct analysis"


# ===========================================================================
# _build_peer_review_prompt tests
# ===========================================================================


class TestBuildFailureSummary:
    def test_no_stack_trace_in_summary(self) -> None:
        """Failure summary should NOT contain stack trace -- peers have repo access."""
        from jenkins_job_insight.peer_analysis import _build_failure_summary

        failures = [_make_failure(stack_trace="at com.example.Foo.bar(Foo.java:10)")]
        summary = _build_failure_summary(failures, error_signature="abc123")
        assert "STACK TRACE" not in summary
        assert "Foo.java:10" not in summary

    def test_failure_summary_contains_essentials(self) -> None:
        """Failure summary includes error signature, test names, and error message."""
        from jenkins_job_insight.peer_analysis import _build_failure_summary

        failures = [
            _make_failure(
                test_name="test_alpha",
                error_message="NullPointerException",
                stack_trace="ignored",
            )
        ]
        summary = _build_failure_summary(failures, error_signature="sig456")
        assert "sig456" in summary
        assert "test_alpha" in summary
        assert "NullPointerException" in summary


class TestBuildPeerReviewPrompt:
    def test_build_peer_review_prompt_contains_framing(self) -> None:
        """Prompt contains AI-to-AI anti-sycophancy framing."""
        from jenkins_job_insight.peer_analysis import _build_peer_review_prompt

        prompt = _build_peer_review_prompt(
            failure_summary="Test failed with assertion error",
            orchestrator_analysis="classification: CODE ISSUE, details: broken test",
            custom_prompt="",
            resources_section="",
        )
        assert "AI-only conversation" in prompt
        assert "sycophantic" in prompt.lower()

    def test_build_peer_review_prompt_contains_analysis(self) -> None:
        """Prompt contains the orchestrator's analysis for review."""
        from jenkins_job_insight.peer_analysis import _build_peer_review_prompt

        prompt = _build_peer_review_prompt(
            failure_summary="Test failed with assertion error",
            orchestrator_analysis="classification: CODE ISSUE, details: broken test",
            custom_prompt="Focus on network issues",
            resources_section="- Git repo at /tmp/repo",
        )
        assert "CODE ISSUE" in prompt
        assert "broken test" in prompt
        assert "Focus on network issues" in prompt
        assert "Git repo at /tmp/repo" in prompt


# ===========================================================================
# Integration tests: analyze_failure_group_with_peers
# ===========================================================================


class TestAnalyzeWithPeers:
    @pytest.mark.asyncio
    async def test_analyze_with_peers_consensus_round_1(self) -> None:
        """All peers agree in round 1 -> consensus reached, 1 round used."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        peer_response = _make_peer_json_response(
            agrees=True, classification="CODE ISSUE"
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            return AIResult(success=True, text=peer_response)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="some console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        assert len(results) == 1
        assert results[0].analysis.classification == "CODE ISSUE"
        assert results[0].peer_debate is not None
        assert results[0].peer_debate.consensus_reached is True
        assert results[0].peer_debate.rounds_used == 1

    @pytest.mark.asyncio
    async def test_analyze_with_peers_consensus_after_revision(self) -> None:
        """Peers disagree round 1, main AI revises, consensus round 2."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        main_response_r2 = _make_ai_json_response(
            classification="PRODUCT BUG",
            details="Revised: this is a product bug",
        )
        # Round 1 peers disagree
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="This is clearly a product bug",
        )
        # Round 2 peers agree
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="PRODUCT BUG",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers (disagree)
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Call 3: main AI revision
            if call_count == 3:
                return AIResult(success=True, text=main_response_r2)
            # Calls 4-5: round 2 peers (agree)
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="some console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        assert len(results) == 1
        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        assert debate.rounds_used == 2

    @pytest.mark.asyncio
    async def test_failed_revision_preserves_previous_analysis(self) -> None:
        """When revision call fails, previous valid analysis is kept."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        # Orchestrator initial: CODE ISSUE
        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="initial"),
                "sig123",
            )
        )

        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="disagree",
        )
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="ok",
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers disagree
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Call 3: orchestrator revision FAILS
            if call_count == 3:
                return AIResult(success=False, text="CLI error: connection refused")
            # Calls 4-5: round 2 peers agree (should see CODE ISSUE, preserved)
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="some console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=2,
            )

        assert len(results) == 1
        # Classification should be CODE ISSUE (preserved from initial, not overwritten)
        assert results[0].analysis.classification == "CODE ISSUE"
        assert results[0].analysis.details == "initial"
        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        assert debate.rounds_used == 2

    @pytest.mark.asyncio
    async def test_revision_exception_preserves_previous_analysis(self) -> None:
        """When revision call raises an exception, prior analysis is preserved."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="initial"),
                "sig123",
            )
        )

        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="disagree",
        )
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="ok",
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers disagree
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Call 3: orchestrator revision RAISES an exception
            if call_count == 3:
                raise RuntimeError("CLI process crashed")
            # Calls 4-5: round 2 peers agree with preserved CODE ISSUE
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="some console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=2,
            )

        assert len(results) == 1
        # Classification preserved from initial orchestrator (not lost to exception)
        assert results[0].analysis.classification == "CODE ISSUE"
        assert results[0].analysis.details == "initial"
        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        assert debate.rounds_used == 2

    @pytest.mark.asyncio
    async def test_revision_preserves_structured_fields_on_same_classification(
        self,
    ) -> None:
        """When revision keeps the same classification but drops structured fields,
        non-empty fields from the prior analysis are preserved (merged forward)."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        # Revision returns same classification but drops code_fix and artifacts_evidence
        revision_response = json.dumps(
            {
                "classification": "CODE ISSUE",
                "affected_tests": ["com.example.TestClass.testMethod"],
                "details": "Revised analysis with peer feedback incorporated",
            }
        )

        # Peers disagree in round 1 (different classification triggers revision)
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="This looks like a product bug not a code issue",
        )
        # After revision, peers agree with the (unchanged) CODE ISSUE classification
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(
                    classification="CODE ISSUE",
                    details="Initial detailed analysis",
                    artifacts_evidence="ERROR: assertion failed at line 42",
                    code_fix={
                        "file": "src/TestClass.java",
                        "line": "42",
                        "change": "fix the assertion",
                    },
                ),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers disagree
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Call 3: revision returns same classification but drops structured fields
            if call_count == 3:
                return AIResult(success=True, text=revision_response)
            # Calls 4-5: round 2 peers agree
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        assert len(results) == 1
        analysis = results[0].analysis
        assert analysis.classification == "CODE ISSUE"
        # Revised details should be used (newer)
        assert analysis.details == "Revised analysis with peer feedback incorporated"
        # Structured fields from initial analysis should be preserved
        assert analysis.artifacts_evidence == "ERROR: assertion failed at line 42"
        # Structured fields from initial analysis should be preserved exactly
        assert analysis.code_fix is not None and analysis.code_fix is not False
        assert analysis.code_fix.file == "src/TestClass.java"
        assert analysis.code_fix.line == "42"
        assert analysis.code_fix.change == "fix the assertion"

    @pytest.mark.asyncio
    async def test_revision_preserves_details_when_revision_omits_them(
        self,
    ) -> None:
        """When revision keeps the same classification but omits details,
        the prior analysis details are preserved (not replaced with empty string)."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        # Revision returns same classification but drops ALL fields including details
        revision_response = json.dumps(
            {
                "classification": "CODE ISSUE",
            }
        )

        # Peers disagree in round 1
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="This looks like a product bug",
        )
        # After revision, peers agree
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
        )

        initial_details = "Very thorough initial analysis with important context"
        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(
                    classification="CODE ISSUE",
                    details=initial_details,
                    artifacts_evidence="ERROR: assertion failed at line 42",
                    code_fix={
                        "file": "src/TestClass.java",
                        "line": "42",
                        "change": "fix the assertion",
                    },
                ),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            if call_count == 3:
                return AIResult(success=True, text=revision_response)
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        assert len(results) == 1
        analysis = results[0].analysis
        assert analysis.classification == "CODE ISSUE"
        # Details should be preserved from initial analysis since revision omitted them
        assert analysis.details == initial_details
        # Other structured fields should also be preserved
        assert analysis.artifacts_evidence == "ERROR: assertion failed at line 42"
        assert analysis.code_fix is not None and analysis.code_fix is not False
        assert analysis.code_fix.file == "src/TestClass.java"
        assert analysis.code_fix.line == "42"
        assert analysis.code_fix.change == "fix the assertion"

    @pytest.mark.asyncio
    async def test_revision_merge_uses_normalized_classification(self) -> None:
        """Revision returning a differently-cased classification still triggers
        the merge-forward path (preserving structured fields from prior analysis)."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        # Revision returns same classification but with different casing
        # and drops code_fix and artifacts_evidence
        revision_response = json.dumps(
            {
                "classification": "code issue",  # lowercase vs "CODE ISSUE"
                "affected_tests": ["com.example.TestClass.testMethod"],
                "details": "Revised analysis after peer review",
            }
        )

        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="This looks like a product bug",
        )
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(
                    classification="CODE ISSUE",
                    details="Initial detailed analysis",
                    artifacts_evidence="ERROR: assertion failed at line 42",
                    code_fix={
                        "file": "src/TestClass.java",
                        "line": "42",
                        "change": "fix the assertion",
                    },
                ),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            if call_count == 3:
                return AIResult(success=True, text=revision_response)
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        assert len(results) == 1
        analysis = results[0].analysis
        # Classification is normalized to canonical uppercase form
        assert analysis.classification == "CODE ISSUE"
        # Revised details should be used (newer)
        assert analysis.details == "Revised analysis after peer review"
        # Structured fields from initial analysis should be preserved
        # because normalized classifications match ("code issue" == "CODE ISSUE")
        assert analysis.artifacts_evidence == "ERROR: assertion failed at line 42"
        assert analysis.code_fix is not None and analysis.code_fix is not False
        assert analysis.code_fix.file == "src/TestClass.java"
        assert analysis.code_fix.line == "42"
        assert analysis.code_fix.change == "fix the assertion"

    @pytest.mark.asyncio
    async def test_analyze_with_peers_max_rounds_no_consensus(self) -> None:
        """Peers never agree; exhausts max_rounds with consensus=False."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        main_response = _make_ai_json_response(classification="CODE ISSUE")
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            # Revision calls: main AI always returns same classification
            if "revision" in prompt.lower() or "revise" in prompt.lower():
                return AIResult(success=True, text=main_response)
            # Peer calls: always disagree
            return AIResult(success=True, text=peer_disagree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=2,
            )

        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is False
        assert debate.rounds_used == 2
        assert debate.max_rounds == 2

    @pytest.mark.asyncio
    async def test_analyze_with_peers_all_peers_fail(self, monkeypatch) -> None:
        """All peers return unparseable responses -> falls back to main AI, consensus=False."""
        orchestrator_json = _make_ai_json_response(classification="CODE ISSUE")
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=False, text="CLI error: connection refused")

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
        )

        assert len(results) == 1
        assert results[0].analysis.classification == "CODE ISSUE"
        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is False

    @pytest.mark.asyncio
    async def test_peer_agrees_derived_from_classification_not_self_reported(
        self,
    ) -> None:
        """agrees_with_orchestrator is derived from classification match, not the peer's self-reported agrees field."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )
        # Peer self-reports agrees=True but classification is PRODUCT BUG
        peer_response = _make_peer_json_response(
            agrees=True,
            classification="PRODUCT BUG",
            reasoning="I say I agree but my classification differs",
        )

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            return AIResult(success=True, text=peer_response)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=1,
            )

        debate = results[0].peer_debate
        assert debate is not None
        # Classification mismatch means no consensus, despite self-reported agrees=True
        assert debate.consensus_reached is False
        # Check that agrees_with_orchestrator was derived from classification match
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        for pr in peer_rounds:
            if pr.agrees_with_orchestrator is not None:
                # Classification is "PRODUCT BUG" vs orchestrator "CODE ISSUE" -> False
                assert pr.agrees_with_orchestrator is False

    @pytest.mark.asyncio
    async def test_analyze_with_peers_one_peer_fails(self) -> None:
        """One peer fails, remaining peer agrees -> consensus=True."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        call_count = 0

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # First peer fails, second agrees
            if call_count == 1:
                return AIResult(success=False, text="CLI crashed")
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        # Should have entries for both peers (one failed, one succeeded)
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        assert len(peer_rounds) == 2
        failed_peers = [r for r in peer_rounds if r.agrees_with_orchestrator is None]
        assert len(failed_peers) == 1

    @pytest.mark.asyncio
    async def test_progress_phase_updates_during_peer_debate(self) -> None:
        """Progress phase is updated before each peer round and orchestrator revision."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        main_response = _make_ai_json_response(classification="CODE ISSUE")
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
        )
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers (disagree)
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Call 3: main AI revision
            if call_count == 3:
                return AIResult(success=True, text=main_response)
            # Calls 4-5: round 2 peers (agree)
            return AIResult(success=True, text=peer_agree)

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
            patch(
                "jenkins_job_insight.peer_analysis.update_progress_phase",
                side_effect=capture_phase,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
                job_id="test-job-id",
            )

        assert "peer_review_round_1" in phases
        assert "orchestrator_revising_round_1" in phases
        assert "peer_review_round_2" in phases

    @pytest.mark.asyncio
    async def test_progress_phase_includes_group_label(self) -> None:
        """When group_label is provided, progress phases include the group info."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        main_response = _make_ai_json_response(classification="CODE ISSUE")
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
        )
        peer_agree = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        call_count = 0

        async def mock_peer_and_revision_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            if call_count == 3:
                return AIResult(success=True, text=main_response)
            return AIResult(success=True, text=peer_agree)

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_and_revision_call,
            ),
            patch(
                "jenkins_job_insight.peer_analysis.update_progress_phase",
                side_effect=capture_phase,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
                job_id="test-job-id",
                group_label="2/3",
            )

        assert "peer_review_round_1 (group 2/3)" in phases
        assert "orchestrator_revising_round_1 (group 2/3)" in phases
        assert "peer_review_round_2 (group 2/3)" in phases

    @pytest.mark.asyncio
    async def test_progress_phase_no_group_suffix_when_label_empty(self) -> None:
        """When group_label is empty, progress phases have no group suffix."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            return AIResult(success=True, text=peer_agree)

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
            patch(
                "jenkins_job_insight.peer_analysis.update_progress_phase",
                side_effect=capture_phase,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
                job_id="test-job-id",
                group_label="",
            )

        # Should be exactly "peer_review_round_1" with no group suffix
        assert "peer_review_round_1" in phases
        assert not any("group" in p for p in phases)

    @pytest.mark.asyncio
    async def test_peer_invalid_classification_excluded_from_consensus(self) -> None:
        """Peer returning an invalid classification (not CODE ISSUE or PRODUCT BUG) is excluded."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )
        # One peer returns invalid classification, other agrees
        call_count = 0

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Invalid classification
                return AIResult(
                    success=True,
                    text=_make_peer_json_response(
                        agrees=True,
                        classification="UNKNOWN TYPE",
                        reasoning="not a valid classification",
                    ),
                )
            # Valid and agrees
            return AIResult(
                success=True,
                text=_make_peer_json_response(
                    agrees=True,
                    classification="CODE ISSUE",
                ),
            )

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=1,
            )

        debate = results[0].peer_debate
        assert debate is not None
        # Consensus should be True because the invalid peer is excluded
        # and the valid peer agrees
        assert debate.consensus_reached is True
        # The invalid peer should have agrees_with_orchestrator=None
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        invalid_peers = [r for r in peer_rounds if r.agrees_with_orchestrator is None]
        assert len(invalid_peers) == 1

    @pytest.mark.asyncio
    async def test_peer_null_classification_coerced_to_empty_string(
        self, monkeypatch
    ) -> None:
        """Peer returning null classification stores empty string, not None."""
        orchestrator_json = _make_ai_json_response(classification="CODE ISSUE")
        null_classification_response = json.dumps(
            {
                "agrees": True,
                "classification": None,
                "reasoning": "some reasoning",
                "suggested_changes": "",
            }
        )
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=True, text=null_classification_response)

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
            max_rounds=1,
        )

        debate = results[0].peer_debate
        assert debate is not None
        # Null classification should be excluded from consensus
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        for pr in peer_rounds:
            # Classification should be coerced to empty string, not None
            assert pr.classification == ""
            assert pr.agrees_with_orchestrator is None

    @pytest.mark.asyncio
    async def test_peer_case_insensitive_classification_agreement(
        self, monkeypatch
    ) -> None:
        """Peer returning lowercase classification still reaches consensus."""
        orchestrator_json = _make_ai_json_response(classification="CODE ISSUE")
        peer_response = _make_peer_json_response(
            agrees=True,
            classification="code issue",  # lowercase
        )
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=True, text=peer_response)

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
            max_rounds=1,
        )

        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        # Check that normalized classification is stored
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        for pr in peer_rounds:
            if pr.agrees_with_orchestrator is not None:
                assert pr.classification == "CODE ISSUE"

    @pytest.mark.asyncio
    async def test_no_progress_phase_when_job_id_empty(self) -> None:
        """When job_id is empty, update_progress_phase should not be called."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        peer_response = _make_peer_json_response(
            agrees=True, classification="CODE ISSUE"
        )

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            return AIResult(success=True, text=peer_response)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
            patch(
                "jenkins_job_insight.peer_analysis.update_progress_phase",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
                job_id="",
            )

        mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_suggested_changes_preserved_in_peer_round_details(
        self, monkeypatch
    ) -> None:
        """Peer's suggested_changes are preserved in the PeerRound details field."""
        orchestrator_json = _make_ai_json_response(classification="CODE ISSUE")
        peer_response = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="The test assertion is wrong",
            suggested_changes="Fix the assertion on line 42",
        )
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=True, text=peer_response)

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
            max_rounds=1,
        )

        debate = results[0].peer_debate
        assert debate is not None
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        for pr in peer_rounds:
            assert "The test assertion is wrong" in pr.details
            assert "Fix the assertion on line 42" in pr.details

    @pytest.mark.asyncio
    async def test_empty_suggested_changes_not_appended(self, monkeypatch) -> None:
        """When suggested_changes is empty, details contains only reasoning."""
        orchestrator_json = _make_ai_json_response(classification="CODE ISSUE")
        peer_response = _make_peer_json_response(
            agrees=True,
            classification="CODE ISSUE",
            reasoning="The test assertion is wrong",
            suggested_changes="",
        )
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=True, text=peer_response)

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
            max_rounds=1,
        )

        debate = results[0].peer_debate
        assert debate is not None
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        for pr in peer_rounds:
            assert pr.details == "The test assertion is wrong"
            assert "Suggested changes" not in pr.details

    @pytest.mark.asyncio
    async def test_invalid_orchestrator_classification_normalized(
        self, monkeypatch
    ) -> None:
        """Orchestrator returning an invalid classification gets normalized to empty string."""
        # Main AI returns a malformed classification
        orchestrator_json = _make_ai_json_response(classification="CODEISSUE")
        peer_response = _make_peer_json_response(
            agrees=True, classification="CODE ISSUE"
        )
        call_count = 0

        async def cli_side_effect(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=orchestrator_json)
            return AIResult(success=True, text=peer_response)

        results = await _run_peer_analysis(
            monkeypatch,
            cli_side_effect=cli_side_effect,
            peer_configs=_peer_configs(),
            max_rounds=1,
        )

        analysis = results[0].analysis
        # Invalid "CODEISSUE" should NOT leak through as the final classification
        assert analysis.classification != "CODEISSUE"

    def test_build_peer_review_prompt_includes_other_peers(self) -> None:
        """When other_peer_responses is provided, prompt includes their responses."""
        from jenkins_job_insight.peer_analysis import _build_peer_review_prompt

        from jenkins_job_insight.peer_analysis import PeerResponseSummary

        other_responses: list[PeerResponseSummary] = [
            PeerResponseSummary(
                ai_provider="gemini",
                ai_model="gemini-2.5-pro",
                classification="PRODUCT BUG",
                reasoning="This is clearly a product defect",
            ),
            PeerResponseSummary(
                ai_provider="openai",
                ai_model="gpt-4",
                classification="CODE ISSUE",
                reasoning="The test itself is wrong",
            ),
        ]
        prompt = _build_peer_review_prompt(
            failure_summary="Test failed",
            orchestrator_analysis="Classification: CODE ISSUE",
            custom_prompt="",
            resources_section="",
            other_peer_responses=other_responses,
        )
        assert "OTHER PEER RESPONSES FROM PREVIOUS ROUND" in prompt
        assert "gemini/gemini-2.5-pro" in prompt
        assert "PRODUCT BUG" in prompt
        assert "This is clearly a product defect" in prompt
        assert "openai/gpt-4" in prompt
        assert "CODE ISSUE" in prompt
        assert "The test itself is wrong" in prompt
        assert "Consider their perspectives" in prompt

    def test_build_peer_review_prompt_no_other_peers_round_1(self) -> None:
        """When other_peer_responses is None (round 1), no other peer section appears."""
        from jenkins_job_insight.peer_analysis import _build_peer_review_prompt

        prompt = _build_peer_review_prompt(
            failure_summary="Test failed",
            orchestrator_analysis="Classification: CODE ISSUE",
            custom_prompt="",
            resources_section="",
            other_peer_responses=None,
        )
        assert "OTHER PEER RESPONSES" not in prompt

    @pytest.mark.asyncio
    async def test_peers_see_each_other_in_round_2(self) -> None:
        """In round 2, each peer's prompt includes the other peer's round 1 response."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        main_response_r2 = _make_ai_json_response(
            classification="CODE ISSUE",
            details="Revised analysis",
        )

        # Round 1: both peers disagree with PRODUCT BUG
        peer1_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="Peer1 round1 reasoning",
        )
        peer2_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="Peer2 round1 reasoning",
        )
        # Round 2: both peers agree
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        captured_prompts: list[tuple[str, str]] = []  # (ai_provider, prompt)
        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-2: round 1 peers
            if call_count == 1:
                captured_prompts.append((ai_provider, prompt))
                return AIResult(success=True, text=peer1_r1)
            if call_count == 2:
                captured_prompts.append((ai_provider, prompt))
                return AIResult(success=True, text=peer2_r1)
            # Call 3: revision
            if call_count == 3:
                return AIResult(success=True, text=main_response_r2)
            # Calls 4-5: round 2 peers
            captured_prompts.append((ai_provider, prompt))
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        # Round 1 prompts (indices 0, 1) should NOT have other peer responses
        for _, prompt in captured_prompts[:2]:
            assert "OTHER PEER RESPONSES" not in prompt

        # Round 2 prompts (indices 2, 3) SHOULD have other peer responses
        assert len(captured_prompts) >= 4
        for _, prompt in captured_prompts[2:4]:
            assert "OTHER PEER RESPONSES FROM PREVIOUS ROUND" in prompt

    @pytest.mark.asyncio
    async def test_peer_excludes_own_response(self) -> None:
        """Each peer in round 2 sees the other peer's response but NOT its own."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        main_response_r2 = _make_ai_json_response(
            classification="CODE ISSUE",
            details="Revised analysis",
        )

        # Round 1: peers disagree with distinct reasoning
        peer1_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="UNIQUE_PEER1_REASONING_XYZ",
        )
        peer2_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="UNIQUE_PEER2_REASONING_ABC",
        )
        # Round 2: agree
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        captured_round2: list[tuple[str, str, str]] = []  # (provider, model, prompt)
        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResult(success=True, text=peer1_r1)
            if call_count == 2:
                return AIResult(success=True, text=peer2_r1)
            if call_count == 3:
                return AIResult(success=True, text=main_response_r2)
            # Round 2 peers
            captured_round2.append((ai_provider, ai_model, prompt))
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        # We should have 2 round-2 peer calls
        assert len(captured_round2) == 2

        # Peer 1 (gemini) should see peer 2's reasoning but NOT its own
        peer1_call = [c for c in captured_round2 if c[0] == "gemini"]
        assert len(peer1_call) == 1
        peer1_prompt = peer1_call[0][2]
        assert "UNIQUE_PEER2_REASONING_ABC" in peer1_prompt
        assert "UNIQUE_PEER1_REASONING_XYZ" not in peer1_prompt

        # Peer 2 (claude) should see peer 1's reasoning but NOT its own
        peer2_call = [c for c in captured_round2 if c[0] == "claude"]
        assert len(peer2_call) == 1
        peer2_prompt = peer2_call[0][2]
        assert "UNIQUE_PEER1_REASONING_XYZ" in peer2_prompt
        assert "UNIQUE_PEER2_REASONING_ABC" not in peer2_prompt

    @pytest.mark.asyncio
    async def test_failed_peer_not_carried_to_next_round(self) -> None:
        """Failed peer entries from round 1 must NOT appear in round 2 prompts."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        main_response_r2 = _make_ai_json_response(
            classification="CODE ISSUE",
            details="Revised analysis",
        )

        # Round 1: peer1 (gemini) succeeds with disagreement, peer2 (claude) fails
        peer1_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="VALID_PEER1_REASONING",
        )
        cli_error_output = "TRANSPORT_FAILURE_ERROR_XYZ"

        # Round 2: both agree
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        captured_round2: list[tuple[str, str, str]] = []  # (provider, model, prompt)
        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Round 1: peer1 succeeds, peer2 CLI fails
            if call_count == 1:
                return AIResult(success=True, text=peer1_r1)
            if call_count == 2:
                return AIResult(success=False, text=cli_error_output)
            # Revision call
            if call_count == 3:
                return AIResult(success=True, text=main_response_r2)
            # Round 2 peers
            captured_round2.append((ai_provider, ai_model, prompt))
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=3,
            )

        # Should have 2 round-2 peer calls
        assert len(captured_round2) == 2

        # No round 2 prompt should contain the failed peer's error output
        for _provider, _model, prompt in captured_round2:
            assert cli_error_output not in prompt

        # The successful peer1's reasoning SHOULD appear (for the other peer)
        peer2_call = [c for c in captured_round2 if c[0] == "claude"]
        assert len(peer2_call) == 1
        assert "VALID_PEER1_REASONING" in peer2_call[0][2]

    @pytest.mark.asyncio
    async def test_invalid_revision_classification_keeps_prior(self) -> None:
        """Revision returning an invalid classification preserves the prior valid analysis."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        # Main AI returns valid classification
        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(
                    classification="CODE ISSUE",
                    details="Valid initial analysis",
                ),
                "sig123",
            )
        )

        # Peers disagree in round 1
        peer_disagree = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="This is a product bug",
        )
        # Revision returns malformed classification
        revision_response = json.dumps(
            {
                "classification": "maybe product bug",
                "details": "Revised with bad classification",
            }
        )
        # Round 2: peers agree with orchestrator (which should still be
        # "CODE ISSUE" since the invalid revision was rejected)
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Round 1 peer calls (2 peers)
            if call_count <= 2:
                return AIResult(success=True, text=peer_disagree)
            # Revision call after round 1
            if call_count == 3:
                return AIResult(success=True, text=revision_response)
            # Round 2 peer calls
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=_peer_configs(),
                max_rounds=2,
            )

        analysis = results[0].analysis
        # "maybe product bug" should NOT leak through as the final classification
        assert analysis.classification != "maybe product bug"
        # The prior valid classification should have been preserved
        assert analysis.classification == "CODE ISSUE"

    @pytest.mark.asyncio
    async def test_duplicate_peer_configs_all_called(self) -> None:
        """Duplicate (provider, model) peers must all be called, not deduplicated."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        peer_call_count = 0

        async def mock_peer_call(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal peer_call_count
            peer_call_count += 1
            return AIResult(success=True, text=peer_agree)

        # 3 identical peer configs -- all should be called
        duplicate_peers = [
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
        ]

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_peer_call,
            ),
        ):
            results = await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=duplicate_peers,
                max_rounds=1,
            )

        # All 3 peers should have been called
        assert peer_call_count == 3
        debate = results[0].peer_debate
        assert debate is not None
        assert debate.consensus_reached is True
        # Should have 3 peer rounds + 1 orchestrator round
        peer_rounds = [r for r in debate.rounds if r.role == "peer"]
        assert len(peer_rounds) == 3
        # All 3 peer configs should be in ai_configs
        assert len(debate.ai_configs) == 4  # 1 main + 3 peers

    @pytest.mark.asyncio
    async def test_cross_peer_visibility_with_failed_peer(self) -> None:
        """When a peer fails in round 1, round 2 must still correctly map
        each surviving peer's response to the right peer index so that
        self-exclusion works properly.

        With 3 peers where peer 1 fails:
        - Peer 0 should see peer 2's response (not its own)
        - Peer 1 (which failed) should see both peer 0 and peer 2's responses
        - Peer 2 should see peer 0's response (not its own)
        """
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        main_response_r2 = _make_ai_json_response(
            classification="CODE ISSUE",
            details="Revised analysis",
        )

        # Round 1: peer0 succeeds, peer1 fails, peer2 succeeds
        peer0_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="UNIQUE_PEER0_REASONING_AAA",
        )
        peer2_r1 = _make_peer_json_response(
            agrees=False,
            classification="PRODUCT BUG",
            reasoning="UNIQUE_PEER2_REASONING_CCC",
        )
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        three_peers = [
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-pro"),
            AiConfigEntry(ai_provider="cursor", ai_model="cursor-fast"),
            AiConfigEntry(ai_provider="claude", ai_model="sonnet"),
        ]

        captured_round2: list[tuple[str, str, str]] = []  # (provider, model, prompt)
        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Round 1: peer0 succeeds, peer1 fails, peer2 succeeds
            if call_count == 1:
                return AIResult(success=True, text=peer0_r1)
            if call_count == 2:
                return AIResult(success=False, text="CLI_FAILURE_OUTPUT")
            if call_count == 3:
                return AIResult(success=True, text=peer2_r1)
            # Call 4: revision
            if call_count == 4:
                return AIResult(success=True, text=main_response_r2)
            # Calls 5-7: round 2 peers
            captured_round2.append((ai_provider, ai_model, prompt))
            return AIResult(success=True, text=peer_agree)

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=three_peers,
                max_rounds=3,
            )

        assert len(captured_round2) == 3

        # Peer 0 (gemini): should see peer 2's reasoning, NOT its own
        peer0_call = [c for c in captured_round2 if c[0] == "gemini"]
        assert len(peer0_call) == 1
        assert "UNIQUE_PEER2_REASONING_CCC" in peer0_call[0][2]
        assert "UNIQUE_PEER0_REASONING_AAA" not in peer0_call[0][2]

        # Peer 1 (cursor, which failed in R1): should see BOTH peers' responses
        peer1_call = [c for c in captured_round2 if c[0] == "cursor"]
        assert len(peer1_call) == 1
        assert "UNIQUE_PEER0_REASONING_AAA" in peer1_call[0][2]
        assert "UNIQUE_PEER2_REASONING_CCC" in peer1_call[0][2]

        # Peer 2 (claude): should see peer 0's reasoning, NOT its own
        peer2_call = [c for c in captured_round2 if c[0] == "claude"]
        assert len(peer2_call) == 1
        assert "UNIQUE_PEER0_REASONING_AAA" in peer2_call[0][2]
        assert "UNIQUE_PEER2_REASONING_CCC" not in peer2_call[0][2]

        # No round 2 prompt should contain the failed peer's error output
        for _, _, prompt in captured_round2:
            assert "CLI_FAILURE_OUTPUT" not in prompt

    @pytest.mark.asyncio
    async def test_duplicate_peers_index_based_self_exclusion(self) -> None:
        """In round 2, duplicate-model peers exclude only their own response by index,
        not all responses from the same provider+model."""
        from unittest.mock import AsyncMock

        from jenkins_job_insight.models import AnalysisDetail
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        mock_orchestrator = AsyncMock(
            return_value=(
                AnalysisDetail(classification="CODE ISSUE", details="Test is broken"),
                "sig123",
            )
        )

        main_response_r2 = _make_ai_json_response(
            classification="CODE ISSUE",
            details="Revised analysis",
        )

        # Round 1: 3 identical-model peers disagree with distinct reasoning
        peer_r1_responses = [
            _make_peer_json_response(
                agrees=False,
                classification="PRODUCT BUG",
                reasoning="UNIQUE_PEER_0_REASONING",
            ),
            _make_peer_json_response(
                agrees=False,
                classification="PRODUCT BUG",
                reasoning="UNIQUE_PEER_1_REASONING",
            ),
            _make_peer_json_response(
                agrees=False,
                classification="PRODUCT BUG",
                reasoning="UNIQUE_PEER_2_REASONING",
            ),
        ]
        peer_agree = _make_peer_json_response(agrees=True, classification="CODE ISSUE")

        captured_round2: list[tuple[int, str]] = []  # (call_index, prompt)
        call_count = 0

        async def mock_calls(
            prompt,
            *,
            cwd=None,
            ai_provider="",
            ai_model="",
            ai_cli_timeout=None,
            cli_flags=None,
            max_retries=3,
        ):
            nonlocal call_count
            call_count += 1
            # Calls 1-3: round 1 peers
            if call_count <= 3:
                return AIResult(success=True, text=peer_r1_responses[call_count - 1])
            # Call 4: revision
            if call_count == 4:
                return AIResult(success=True, text=main_response_r2)
            # Calls 5-7: round 2 peers
            captured_round2.append((call_count - 5, prompt))
            return AIResult(success=True, text=peer_agree)

        duplicate_peers = [
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
            AiConfigEntry(ai_provider="gemini", ai_model="gemini-2.5-pro"),
        ]

        with (
            patch(
                "jenkins_job_insight.peer_analysis._run_single_ai_analysis",
                mock_orchestrator,
            ),
            patch(
                "jenkins_job_insight.peer_analysis._call_ai_cli_with_retry",
                side_effect=mock_calls,
            ),
        ):
            await analyze_failure_group_with_peers(
                failures=[_make_failure()],
                console_context="console output",
                repo_path=None,
                main_ai_provider="claude",
                main_ai_model="claude-sonnet-4-20250514",
                peer_ai_configs=duplicate_peers,
                max_rounds=3,
            )

        assert len(captured_round2) == 3

        # Each peer at index i should see the OTHER two peers' reasoning
        # but NOT its own (by index, not by provider+model)
        for idx, prompt in captured_round2:
            # Should see the other 2 peers' reasoning
            for other_idx in range(3):
                if other_idx != idx:
                    assert f"UNIQUE_PEER_{other_idx}_REASONING" in prompt, (
                        f"Peer {idx} should see UNIQUE_PEER_{other_idx}_REASONING"
                    )
            # Should NOT see its own reasoning
            assert f"UNIQUE_PEER_{idx}_REASONING" not in prompt, (
                f"Peer {idx} should NOT see its own UNIQUE_PEER_{idx}_REASONING"
            )
