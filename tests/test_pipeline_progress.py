"""Pipeline stage boundaries persist without duplicate phase entries."""

from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.models import (
    AnalysisDetail,
    BaseTestEntry,
    ChildJobAnalysis,
    FailedTest,
    FailureAnalysis,
    ProductBugReport,
    UnifiedAnalyzeRequest,
)
from rootcoz.sources.base import CISourceResult, WorkspaceSetupResult


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_failed, expected", [(False, 2), (True, 3)])
async def test_direct_and_child_failures_persist_scoped_group_count(
    temp_db_path: Path, tmp_path: Path, direct_failed: bool, expected: int
) -> None:
    from rootcoz.main import _process_ci_source_analysis

    failed = FailureAnalysis(
        test_name="bad",
        error="boom",
        error_signature="same",
        analysis=AnalysisDetail(
            details="Analysis failed; check server logs for details"
        ),
    )
    good = FailureAnalysis(
        test_name="good",
        error="boom",
        error_signature="same",
        analysis=AnalysisDetail(details="diagnosed"),
    )
    children = [
        ChildJobAnalysis(
            job_name=name,
            build_number=number,
            all_groups_failed=True,
            failures=[
                failed.model_copy(update={"test_name": "a"}),
                failed.model_copy(update={"test_name": "b"}),
            ],
        )
        for name, number in (("leaf", 1), ("leaf", 2))
    ]
    source_result = CISourceResult(
        failures=[FailedTest(test_name="direct", error_message="boom")],
        child_job_infos=[("leaf", 1), ("leaf", 2)],
    )
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
            "rootcoz.main.run_orchestrated_analysis",
            new_callable=AsyncMock,
            return_value=([good, failed] if direct_failed else [good], []),
        ),
        patch("rootcoz.main._auto_review_matching_failures", new_callable=AsyncMock),
        patch("rootcoz.main._auto_assign_metadata", new_callable=AsyncMock),
        patch(
            "rootcoz.main.storage.make_classifications_visible", new_callable=AsyncMock
        ),
    ):
        source.return_value.raw_xml = None
        source.return_value.requires_pre_fetch.return_value = False
        source.return_value.prepare_workspace = AsyncMock(return_value=[])
        source.return_value.fetch = AsyncMock(return_value=source_result)
        source.return_value.analyze_children = AsyncMock(return_value=(children, []))
        source.return_value.persist_fetch_metadata = AsyncMock()
        await storage.init_db()
        await storage.save_result("direct-child", status="pending", result={})
        await _process_ci_source_analysis(
            job_id="direct-child",
            body=UnifiedAnalyzeRequest(type="raw", failures=source_result.failures),
            merged=Settings(),
            display_name="parent",
            ai_provider="claude",
            ai_model="model",
            peer_ai_configs=None,
            tests_repo_url="",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )
        row = await storage.get_result("direct-child")
    assert row["status"] == "completed", row["result"]
    assert row["result"]["failed_analysis_groups"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jira_enabled, keywords, partial, dedup, all_failed",
    [
        (False, True, False, False, False),
        (True, True, False, False, False),
        (True, False, False, False, False),
        (False, True, True, False, False),
        (False, True, True, True, False),
        (False, True, False, False, True),
    ],
)
async def test_pipeline_jira_and_saving_stages(
    temp_db_path: Path,
    tmp_path: Path,
    jira_enabled: bool,
    keywords: bool,
    partial: bool,
    dedup: bool,
    all_failed: bool,
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
    source_result = CISourceResult(
        failures=body.failures,
        passed_tests=[BaseTestEntry(test_name="passed", status="passed")],
        console_context="context",
    )
    failed = FailureAnalysis(
        test_name="bad",
        error="err",
        analysis=AnalysisDetail(
            details="Analysis failed; check server logs for details"
        ),
    )

    if dedup:
        source_result.failures = [
            FailedTest(test_name=name, error_message=error)
            for name, error in (
                ("good1", "good"),
                ("good2", "good"),
                ("bad1", "bad"),
                ("bad2", "bad"),
            )
        ]
        analysis.error_signature = "good-sig"
        failed.error_signature = "bad-sig"
        failed_duplicate = failed.model_copy(update={"test_name": "bad2"})
        success_duplicate = analysis.model_copy(update={"test_name": "good2"})

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
            return_value=(
                [analysis, success_duplicate, failed, failed_duplicate]
                if dedup
                else [analysis, failed]
                if partial
                else [failed]
                if all_failed
                else [analysis],
                [],
            ),
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
        assert row["status"] == ("failed" if all_failed else "completed")
        assert row["result"]["status"] == row["status"]
        assert row["result"]["passed_count"] == 1
        if partial or all_failed:
            assert len(row["result"]["failures"]) == (
                4 if dedup else 1 if all_failed else 2
            )
            assert row["result"]["failed_analysis_groups"] == 1
            assert "1 group(s) failed" in row["result"]["summary"]
            assert (
                f"{2 if dedup else 0 if all_failed else 1} analyzed successfully"
                in row["result"]["summary"]
            )
            if dedup:
                assert (
                    "Analyzed 4 test failures (2 unique errors)"
                    in row["result"]["summary"]
                )
        phases = [entry["phase"] for entry in row["result"]["progress_log"]]
        assert phases == ["fetching", "analyzing"] + (
            ["enriching_jira"] if jira_enabled and keywords else []
        ) + ["saving"]
        assert jira_mock.await_count == int(jira_enabled and keywords)
