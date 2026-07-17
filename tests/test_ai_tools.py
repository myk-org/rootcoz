"""Tests for AI session tool restrictions (issue #74).

Verifies that all AI sessions receive explicit tool lists (with subagent,
without bash) and that the chat session rebuild path also enforces tool
restrictions.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pi_sidecar_client import AIResult

from rootcoz.ai_client import ANALYSIS_BUILTIN_TOOLS, CHAT_BUILTIN_TOOLS


class TestToolConstants:
    """Verify tool list constants have correct values."""

    def test_chat_tools_include_subagent(self):
        assert "subagent" in CHAT_BUILTIN_TOOLS

    def test_analysis_tools_include_subagent(self):
        assert "subagent" in ANALYSIS_BUILTIN_TOOLS

    def test_chat_tools_no_bash(self):
        assert "bash" not in CHAT_BUILTIN_TOOLS

    def test_analysis_tools_no_bash(self):
        assert "bash" not in ANALYSIS_BUILTIN_TOOLS

    def test_chat_tools_filesystem_browsing(self):
        for tool in ("read", "ls", "find", "grep"):
            assert tool in CHAT_BUILTIN_TOOLS

    def test_analysis_tools_filesystem_browsing(self):
        for tool in ("read", "ls", "find", "grep"):
            assert tool in ANALYSIS_BUILTIN_TOOLS


class TestChatSessionTools:
    """Verify _create_chat_session passes tools correctly."""

    @pytest.mark.asyncio
    async def test_create_chat_session_passes_tools(self):
        from rootcoz.engine.chat import _create_chat_session

        mock_client = MagicMock()
        mock_client.create_session = AsyncMock(return_value="sess-123")

        with patch("rootcoz.engine.chat.get_sidecar_client", return_value=mock_client):
            session_id = await _create_chat_session(
                system_prompt="test",
                ai_provider="gemini",
                ai_model="pro",
                restrict_tools=True,
            )

        assert session_id == "sess-123"
        passed_kwargs = mock_client.create_session.call_args.kwargs
        assert passed_kwargs.get("tools") == list(CHAT_BUILTIN_TOOLS)

    @pytest.mark.asyncio
    async def test_create_chat_session_no_restrict(self):
        from rootcoz.engine.chat import _create_chat_session

        mock_client = MagicMock()
        mock_client.create_session = AsyncMock(return_value="sess-456")

        with patch("rootcoz.engine.chat.get_sidecar_client", return_value=mock_client):
            await _create_chat_session(
                system_prompt="test",
                ai_provider="gemini",
                ai_model="pro",
                restrict_tools=False,
            )

        passed_kwargs = mock_client.create_session.call_args.kwargs
        assert "tools" not in passed_kwargs


class TestChatImplTools:
    """Verify _chat_with_ai_impl passes tools on new session and retry."""

    @pytest.mark.asyncio
    async def test_new_session_passes_tools(self):
        from rootcoz.engine.chat import _chat_with_ai_impl

        mock_call_ai = AsyncMock(
            return_value=AIResult(success=True, text="response", session_id="new-sess")
        )

        with patch("rootcoz.engine.chat.call_ai", mock_call_ai):
            success, text, sid = await _chat_with_ai_impl(
                message="hello",
                history=[],
                ai_provider="gemini",
                ai_model="pro",
                build_prompt_fn=lambda: "system prompt",
                session_id=None,
                restrict_tools=True,
            )

        assert success
        assert mock_call_ai.call_args[1].get("tools") == list(CHAT_BUILTIN_TOOLS)

    @pytest.mark.asyncio
    async def test_existing_session_no_tools(self):
        from rootcoz.engine.chat import _chat_with_ai_impl

        mock_call_ai = AsyncMock(
            return_value=AIResult(success=True, text="response", session_id="existing")
        )

        with patch("rootcoz.engine.chat.call_ai", mock_call_ai):
            await _chat_with_ai_impl(
                message="hello",
                history=[],
                ai_provider="gemini",
                ai_model="pro",
                build_prompt_fn=lambda: "system prompt",
                session_id="existing-sess",
                restrict_tools=True,
            )

        assert "tools" not in mock_call_ai.call_args[1]

    @pytest.mark.asyncio
    async def test_session_lost_retry_passes_tools(self):
        from rootcoz.engine.chat import _chat_with_ai_impl

        mock_call_ai = AsyncMock(
            side_effect=[
                AIResult(success=False, text="Session not found"),
                AIResult(success=True, text="retry response", session_id="new-sess"),
            ]
        )

        with patch("rootcoz.engine.chat.call_ai", mock_call_ai):
            success, text, sid = await _chat_with_ai_impl(
                message="hello",
                history=[],
                ai_provider="gemini",
                ai_model="pro",
                build_prompt_fn=lambda: "system prompt",
                session_id="old-sess",
                restrict_tools=True,
            )

        assert success
        retry_kwargs = mock_call_ai.call_args_list[1][1]
        assert retry_kwargs.get("tools") == list(CHAT_BUILTIN_TOOLS)
        assert retry_kwargs.get("session_id") is None


class TestAnalysisTools:
    """Verify call_ai_once in core.py passes ANALYSIS_BUILTIN_TOOLS."""

    @pytest.mark.asyncio
    async def test_analyze_passes_tools(self, monkeypatch, tmp_path):
        from rootcoz.engine.core import run_single_ai_analysis

        captured_kwargs = {}

        async def mock_ai_once(prompt, **kwargs):
            captured_kwargs.update(kwargs)
            return AIResult(
                success=True,
                text=json.dumps(
                    {
                        "classification": "CODE ISSUE",
                        "details": "test",
                    }
                ),
            )

        monkeypatch.setattr("rootcoz.engine.core.call_ai_once", mock_ai_once)
        monkeypatch.setattr("rootcoz.engine.core.update_progress_phase", AsyncMock())

        from rootcoz.models import FailedTest

        failures = [
            FailedTest(
                test_name="test_example",
                error_message="AssertionError",
                stack_trace="trace",
            )
        ]

        await run_single_ai_analysis(
            failures=failures,
            console_context="some console output",
            repo_path=tmp_path,
            ai_provider="gemini",
            ai_model="pro",
            ai_call_timeout=None,
            custom_prompt="",
            artifacts_context="",
            server_url="",
            job_id="test-job",
        )

        assert captured_kwargs.get("tools") == list(ANALYSIS_BUILTIN_TOOLS)


class TestPeerAnalysisTools:
    """Verify peer analysis passes ANALYSIS_BUILTIN_TOOLS."""

    @pytest.mark.asyncio
    async def test_peer_calls_pass_tools(self, monkeypatch, tmp_path):
        from rootcoz.models import AiConfigEntry, FailedTest
        from rootcoz.peer_analysis import analyze_failure_group_with_peers

        captured_call_ai_kwargs = []

        async def mock_call_ai(prompt, **kwargs):
            captured_call_ai_kwargs.append(kwargs)
            return AIResult(
                success=True,
                text=json.dumps(
                    {
                        "classification": "CODE ISSUE",
                        "details": "peer analysis",
                        "timeline": [],
                        "jira_search_keywords": [],
                    }
                ),
                session_id="peer-sess",
            )

        async def mock_call_ai_once(prompt, **kwargs):
            return AIResult(
                success=True,
                text=json.dumps(
                    {
                        "classification": "CODE ISSUE",
                        "details": "revision",
                        "timeline": [],
                        "jira_search_keywords": [],
                    }
                ),
            )

        monkeypatch.setattr("rootcoz.peer_analysis.call_ai", mock_call_ai)
        monkeypatch.setattr("rootcoz.peer_analysis.call_ai_once", mock_call_ai_once)
        monkeypatch.setattr("rootcoz.engine.core.call_ai_once", mock_call_ai_once)
        monkeypatch.setattr("rootcoz.engine.core.update_progress_phase", AsyncMock())

        failures = [
            FailedTest(
                test_name="test_peer",
                error_message="Error",
                stack_trace="trace",
            )
        ]
        peer_configs = [AiConfigEntry(ai_provider="gemini", ai_model="pro")]

        await analyze_failure_group_with_peers(
            failures=failures,
            console_context="some console output",
            repo_path=tmp_path,
            main_ai_provider="claude",
            main_ai_model="sonnet",
            peer_ai_configs=peer_configs,
            ai_call_timeout=None,
            custom_prompt="",
            artifacts_context="",
            server_url="",
            job_id="peer-job",
        )

        assert any(
            kw.get("tools") == list(ANALYSIS_BUILTIN_TOOLS)
            for kw in captured_call_ai_kwargs
        ), f"No call_ai call had tools={list(ANALYSIS_BUILTIN_TOOLS)}"


class TestResourcesAgentDiscovery:
    """Verify build_resources_section advertises .rootcoz/agents/."""

    def test_agents_advertised_with_frontmatter(self, tmp_path):
        """Agent names come from frontmatter name: field."""
        from rootcoz.engine.core import build_resources_section

        repo = tmp_path / "my-repo"
        agents_dir = repo / ".rootcoz" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "analyzer.md").write_text(
            "---\nname: my-analyzer\ndescription: Analyzes things\n---\nInstructions"
        )
        (agents_dir / "helper.md").write_text(
            "---\nname: my-helper\ndescription: Helps out\n---\nInstructions"
        )

        result = build_resources_section(
            tmp_path,
            additional_repos={"my-repo": repo},
        )

        assert "MANDATORY" in result
        assert "my-analyzer" in result
        assert "my-helper" in result
        assert "agentScope" in result

    def test_agents_fallback_to_filename(self, tmp_path):
        """Without frontmatter, falls back to filename stem."""
        from rootcoz.engine.core import build_resources_section

        repo = tmp_path / "my-repo"
        agents_dir = repo / ".rootcoz" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-analyzer.md").write_text("# Agent without frontmatter")

        result = build_resources_section(
            tmp_path,
            additional_repos={"my-repo": repo},
        )

        assert "my-analyzer" in result

    def test_no_agents_dir(self, tmp_path):
        from rootcoz.engine.core import build_resources_section

        repo = tmp_path / "my-repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        result = build_resources_section(
            tmp_path,
            additional_repos={"my-repo": repo},
        )

        assert "MANDATORY" not in result

    def test_empty_agents_dir(self, tmp_path):
        from rootcoz.engine.core import build_resources_section

        repo = tmp_path / "my-repo"
        agents_dir = repo / ".rootcoz" / "agents"
        agents_dir.mkdir(parents=True)

        result = build_resources_section(
            tmp_path,
            additional_repos={"my-repo": repo},
        )

        assert "MANDATORY" not in result


class TestSidecarArgvFix:
    """Verify sidecar server.ts clears process.argv[1]."""

    def test_server_ts_clears_argv(self):
        server_ts = Path("sidecar-helper/src/server.ts").read_text()
        assert 'process.argv[1] = ""' in server_ts
