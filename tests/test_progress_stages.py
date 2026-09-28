"""Live and refreshable progress milestones for the analysis pipeline."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pi_sidecar_client import AIResult

from rootcoz import storage
from rootcoz.engine.core import run_orchestrated_analysis, safe_update_progress
from rootcoz.models import FailedTest


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_agents", [False, True])
@pytest.mark.parametrize("group_count", [1, 2])
async def test_orchestration_phase_transitions(
    tmp_path: Path, custom_agents: bool, group_count: int
) -> None:
    groups = {
        f"sig_{i}": [FailedTest(test_name=f"test_{i}", error_message="err")]
        for i in range(group_count)
    }
    stages: list[str] = []

    async def ai_call(*_args, **_kwargs):
        return AIResult(
            success=True,
            text=json.dumps({"classification": "INFRASTRUCTURE", "details": "d"}),
        )

    async def progress(_job: str, phase: str):
        stages.append(phase)

    with (
        patch(
            "rootcoz.engine.core.discover_custom_agents",
            return_value=["specialist"] if custom_agents else [],
        ),
        patch("rootcoz.engine.core._call_ai_with_retry", side_effect=ai_call),
        patch("rootcoz.engine.core.call_ai_once", side_effect=ai_call),
        patch("rootcoz.engine.core.safe_update_progress", side_effect=progress),
    ):
        await run_orchestrated_analysis(
            groups=groups,
            console_context="",
            repo_path=tmp_path,
            ai_provider="test",
            ai_model="model",
            job_id="job",
            max_concurrent_ai_calls=1,
        )

    expected = (
        (["agent_routing"] if custom_agents else [])
        + [
            f"analyzing_failures (group {i + 1}/{group_count})"
            for i in range(group_count)
        ]
        + (["cross_failure"] if group_count > 1 else [])
    )
    assert stages == expected
    assert len(stages) == len(set(stages))


@pytest.mark.asyncio
async def test_fallback_per_group_progress(tmp_path: Path) -> None:
    from rootcoz.main import _run_per_group_analysis

    groups = {
        f"sig_{i}": [FailedTest(test_name=f"test_{i}", error_message="err")]
        for i in range(2)
    }
    phases: list[str] = []

    async def progress(_job: str, phase: str):
        phases.append(phase)

    with (
        patch(
            "rootcoz.main.analyze_failure_group",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("rootcoz.main.safe_update_progress", side_effect=progress),
    ):
        await _run_per_group_analysis(
            groups=groups,
            console_context="",
            repo_path=tmp_path,
            ai_provider="test",
            ai_model="model",
            ai_call_timeout=None,
            custom_prompt="",
            artifacts_context="",
            server_url="",
            job_id="job",
            additional_repos=None,
            max_concurrent_ai_calls=1,
            auth_header="",
        )
    assert phases == [
        "analyzing_failures (group 1/2)",
        "analyzing_failures (group 2/2)",
    ]


@pytest.mark.asyncio
async def test_progress_stage_sse_and_refresh(temp_db_path: Path) -> None:
    from rootcoz.main import _job_status_listeners, _make_sse_stream

    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        await storage.save_result("stage-job", "", "running", {})
        request = AsyncMock()
        request.is_disconnected.return_value = False
        stream = _make_sse_stream(
            request,
            set(),
            "status-changed",
            per_key_listeners=_job_status_listeners,
            listener_key="stage-job",
        ).body_iterator
        event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        try:
            await safe_update_progress("stage-job", "saving")
            assert (
                await asyncio.wait_for(event, 1)
                == "event: status-changed\ndata: refresh\n\n"
            )
            result = await storage.get_result("stage-job")
            assert result["result"]["progress_phase"] == "saving"
            assert [entry["phase"] for entry in result["result"]["progress_log"]] == [
                "saving"
            ]
        finally:
            await stream.aclose()
