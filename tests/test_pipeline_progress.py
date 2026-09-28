"""Pipeline stage boundaries persist without duplicate phase entries."""

from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.models import (
    AnalysisDetail,
    FailedTest,
    FailureAnalysis,
    ProductBugReport,
    UnifiedAnalyzeRequest,
)
from rootcoz.sources.base import CISourceResult, WorkspaceSetupResult


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jira_enabled, keywords", [(False, True), (True, True), (True, False)]
)
async def test_pipeline_jira_and_saving_stages(
    temp_db_path: Path, tmp_path: Path, jira_enabled: bool, keywords: bool
) -> None:
    from rootcoz.main import _process_ci_source_analysis

    body = UnifiedAnalyzeRequest(
        type="raw",
        failures=[FailedTest(test_name="test", error_message="err")],
        ai_provider="claude",
        ai_model="model",
    )
    analysis = FailureAnalysis(
        test_name="test",
        error="err",
        analysis=AnalysisDetail(
            details="done",
            product_bug_report=ProductBugReport(
                jira_search_keywords=["bug"] if keywords else []
            ),
        ),
    )
    source_result = CISourceResult(failures=body.failures, console_context="context")

    async def jira(*_args, **_kwargs):
        row = await storage.get_result("progress-pipeline")
        assert row["result"]["progress_phase"] == "enriching_jira"

    async def saving(*_args, **_kwargs):
        row = await storage.get_result("progress-pipeline")
        assert row["result"]["progress_phase"] == "saving"

    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch("rootcoz.main.create_source_from_request") as source,
        patch(
            "rootcoz.main.setup_analysis_workspace",
            new_callable=AsyncMock,
            return_value=(WorkspaceSetupResult(tmp_path), ""),
        ),
        patch(
            "rootcoz.main._preflight_sidecar_check",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "rootcoz.main._validate_catalog_pair",
            new_callable=AsyncMock,
            return_value=("claude", "model"),
        ),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "rootcoz.main.run_orchestrated_analysis",
            new_callable=AsyncMock,
            return_value=([analysis], []),
        ),
        patch("rootcoz.main._resolve_enable_jira", return_value=jira_enabled),
        patch.object(
            Settings,
            "jira_enabled",
            new_callable=PropertyMock,
            return_value=jira_enabled,
        ),
        patch("rootcoz.main.enrich_with_jira_matches", side_effect=jira) as jira_mock,
        patch("rootcoz.main.populate_failure_history", side_effect=saving),
        patch("rootcoz.main._auto_review_matching_failures", new_callable=AsyncMock),
        patch("rootcoz.main._auto_assign_metadata", new_callable=AsyncMock),
        patch(
            "rootcoz.main.storage.make_classifications_visible", new_callable=AsyncMock
        ),
    ):
        source.return_value.raw_xml = None
        source.return_value.prepare_workspace = AsyncMock(return_value=[])
        source.return_value.requires_pre_fetch.return_value = False
        source.return_value.fetch = AsyncMock(return_value=source_result)
        source.return_value.persist_fetch_metadata = AsyncMock()
        await storage.init_db()
        await storage.save_result("progress-pipeline", "", "pending", {})
        await _process_ci_source_analysis(
            job_id="progress-pipeline",
            body=body,
            merged=Settings(),
            display_name="test",
            ai_provider="claude",
            ai_model="model",
            peer_ai_configs=None,
            tests_repo_url="",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )
        row = await storage.get_result("progress-pipeline")
        assert row["status"] == "completed", row["result"].get("error")
        phases = [entry["phase"] for entry in row["result"]["progress_log"]]
        assert phases == ["fetching", "analyzing"] + (
            ["enriching_jira"] if jira_enabled and keywords else []
        ) + ["saving"]
        assert jira_mock.await_count == int(jira_enabled and keywords)
