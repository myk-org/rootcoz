"""Regressions for Jenkins child analysis failure and usage boundaries."""

from unittest.mock import AsyncMock, patch

import pytest
from pi_sidecar_client import AIResult, AITokenUsage

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.models import AnalysisDetail, ChildJobAnalysis, FailedTest, FailureAnalysis
from rootcoz.sources.base import CISourceResult
from rootcoz.sources.jenkins_source import (
    ChildJobResult,
    _analyze_child_job_inner,
    _normalize_child_results,
)


@pytest.mark.asyncio
async def test_console_only_usage_attaches_to_each_child_build(temp_db_path) -> None:
    from rootcoz.token_tracking import _child_context

    async def ai_response(_prompt, **_kwargs):
        build = _child_context.get()
        return AIResult(
            success=True,
            text='{"details": "diagnosed"}',
            usage=AITokenUsage(input_tokens=7 if build == ("leaf", 7) else 11),
        )

    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch("rootcoz.engine.core.call_ai_once", side_effect=ai_response),
        patch(
            "rootcoz.engine.core.install_http_tools_mcp_best_effort_async",
            new_callable=AsyncMock,
        ),
    ):
        await storage.init_db()
        children = []
        for build in (7, 8):
            child = await _analyze_child_job_inner(
                source=None,
                source_result=CISourceResult(
                    failures=[], console_context="identical console failure"
                ),
                job_name="leaf",
                build_number=build,
                jenkins_url=f"https://example.test/job/leaf/{build}",
                settings=Settings(),
                depth=0,
                max_depth=3,
                repo_path=None,
                ai_provider="test",
                ai_model="test",
                ai_call_timeout=None,
                custom_prompt="",
                server_url="",
                job_id="parent",
                peer_ai_configs=None,
                peer_analysis_max_rounds=1,
                additional_repos=None,
                max_concurrent_ai_calls=1,
                auth_header="",
                ingest_only=False,
            )
            children.append(child.analysis.model_dump(mode="json"))

        assert (
            children[0]["failures"][0]["error_signature"]
            == children[1]["failures"][0]["error_signature"]
        )
        await storage.save_result(
            "parent", status="completed", result={"child_job_analyses": children}
        )
        saved = (await storage.get_result("parent"))["result"]
        records = await storage.get_token_usage_for_job("parent")

    assert _child_context.get() is None
    assert {(r["child_build_number"], r["input_tokens"]) for r in records} == {
        (7, 7),
        (8, 11),
    }
    assert [
        child["failures"][0]["token_usage"]["total_input_tokens"]
        for child in saved["child_job_analyses"]
    ] == [7, 11]


@pytest.mark.asyncio
async def test_child_group_analysis_sets_usage_context_and_marks_all_failed() -> None:
    from rootcoz.token_tracking import _child_context

    failure = FailedTest(test_name="test_a", error_message="boom")
    seen = []

    async def failed_groups(*_args, **_kwargs):
        seen.append(_child_context.get())
        return (
            [
                FailureAnalysis(
                    test_name="test_a",
                    error="boom",
                    analysis=AnalysisDetail(details="Analysis failed"),
                )
            ],
            1,
            1,
        )

    with patch(
        "rootcoz.sources.jenkins_source._analyze_grouped_failures",
        side_effect=failed_groups,
    ):
        result = await _analyze_child_job_inner(
            source=None,
            source_result=CISourceResult(failures=[failure]),
            job_name="leaf",
            build_number=7,
            jenkins_url="https://example.test/job/leaf/7",
            settings=Settings(),
            depth=0,
            max_depth=3,
            repo_path=None,
            ai_provider="test",
            ai_model="test",
            ai_call_timeout=None,
            custom_prompt="",
            server_url="",
            job_id="parent",
            peer_ai_configs=None,
            peer_analysis_max_rounds=1,
            additional_repos=None,
            max_concurrent_ai_calls=1,
            auth_header="",
            ingest_only=False,
        )
    assert seen == [("leaf", 7)]
    assert result.analysis.note == "All 1 analysis group(s) failed"
    assert result.analysis.all_groups_failed is True
    assert _child_context.get() is None


@pytest.mark.asyncio
async def test_pipeline_with_only_failed_child_groups_marks_parent_failed(
    temp_db_path,
) -> None:
    from rootcoz import storage
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import UnifiedAnalyzeRequest

    failed = ChildJobAnalysis(
        job_name="leaf",
        build_number=7,
        all_groups_failed=True,
        note="All 1 analysis group(s) failed",
        failures=[
            FailureAnalysis(
                test_name="test_a",
                error="boom",
                analysis=AnalysisDetail(details="Analysis failed"),
            )
        ],
    )
    source_result = CISourceResult(
        failures=[],
        child_job_infos=[("leaf", 7)],
        identity={"job_name": "parent", "build_number": 1},
    )
    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.requires_pre_fetch",
            return_value=False,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
            new_callable=AsyncMock,
            return_value=source_result,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.analyze_children",
            new_callable=AsyncMock,
            return_value=([failed], []),
        ),
        patch(
            "rootcoz.main._preflight_sidecar_check",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "rootcoz.main._validate_catalog_pair",
            new_callable=AsyncMock,
            return_value=("claude", "test"),
        ),
    ):
        await storage.init_db()
        await storage.save_result("parent", status="pending", result={})
        await _process_ci_source_analysis(
            job_id="parent",
            body=UnifiedAnalyzeRequest(
                type="jenkins", job_name="parent", build_number=1
            ),
            merged=Settings(
                jenkins_url="https://example.test",
                jenkins_user="user",
                jenkins_password="fake",  # pragma: allowlist secret
            ),
            display_name="parent",
            ai_provider="claude",
            ai_model="test",
            peer_ai_configs=None,
            tests_repo_url="",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )
        row = await storage.get_result("parent")
    assert row["status"] == "failed", row["result"]
    assert "child_job_analyses" in row["result"], row["result"]
    assert row["result"]["child_job_analyses"][0]["all_groups_failed"] is True
    assert row["result"]["child_job_analyses"][0]["failures"]


@pytest.mark.asyncio
async def test_crashed_child_and_successful_sibling_complete_with_warning(
    temp_db_path,
) -> None:
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import UnifiedAnalyzeRequest

    children = _normalize_child_results(
        [("crashed", 2), ("ok", 3)],
        [
            RuntimeError("secret-api-key-value"),
            ChildJobResult(
                analysis=ChildJobAnalysis(
                    job_name="ok",
                    build_number=3,
                    failures=[
                        FailureAnalysis(
                            test_name="test_ok",
                            error="boom",
                            analysis=AnalysisDetail(details="diagnosed"),
                        )
                    ],
                )
            ),
        ],
    )
    source_result = CISourceResult(
        failures=[],
        child_job_infos=[("crashed", 2), ("ok", 3)],
        identity={"job_name": "parent", "build_number": 1},
    )
    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.requires_pre_fetch",
            return_value=False,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
            new_callable=AsyncMock,
            return_value=source_result,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.analyze_children",
            new_callable=AsyncMock,
            return_value=([child.analysis for child in children], []),
        ),
        patch(
            "rootcoz.main._preflight_sidecar_check",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "rootcoz.main._validate_catalog_pair",
            new_callable=AsyncMock,
            return_value=("claude", "test"),
        ),
    ):
        await storage.init_db()
        await storage.save_result("parent", status="pending", result={})
        await _process_ci_source_analysis(
            job_id="parent",
            body=UnifiedAnalyzeRequest(
                type="jenkins", job_name="parent", build_number=1
            ),
            merged=Settings(
                jenkins_url="https://example.test",
                jenkins_user="user",
                jenkins_password="fake",  # pragma: allowlist secret
            ),
            display_name="parent",
            ai_provider="claude",
            ai_model="test",
            peer_ai_configs=None,
            tests_repo_url="",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )
        row = await storage.get_result("parent")
    assert row["status"] == "completed", row["result"]
    assert row["result"]["failed_analysis_groups"] == 1
    assert row["result"]["child_job_analyses"][0]["all_groups_failed"] is True
    assert (
        row["result"]["child_job_analyses"][1]["failures"][0]["analysis"]["details"]
        == "diagnosed"
    )
    assert "secret-api-key-value" not in str(row["result"])


@pytest.mark.asyncio
async def test_failed_child_group_does_not_expose_provider_exception() -> None:
    from rootcoz.sources.jenkins_source import _analyze_grouped_failures

    with (
        patch(
            "rootcoz.sources.jenkins_source.analyze_failure_group",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret-api-key-value"),
        ),
        patch("rootcoz.sources.jenkins_source.logger.error") as log_error,
    ):
        failures, total, failed = await _analyze_grouped_failures(
            [FailedTest(test_name="test_a", error_message="boom")],
            console_context="",
            repo_path=None,
            artifacts_context="",
        )
    assert (total, failed) == (1, 1)
    assert "secret-api-key-value" not in failures[0].analysis.details
    assert "secret-api-key-value" not in str(log_error.call_args)
    assert "pi_sidecar_client" in str(log_error.call_args)


@pytest.mark.asyncio
async def test_child_only_completion_persists_nested_failed_group_count(
    temp_db_path,
) -> None:
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import UnifiedAnalyzeRequest

    def failure(name: str, signature: str, details: str) -> FailureAnalysis:
        return FailureAnalysis(
            test_name=name,
            error="boom",
            error_signature=signature,
            analysis=AnalysisDetail(details=details),
        )

    failed_details = "Analysis failed; check server logs for details"
    children = [
        ChildJobAnalysis(
            job_name="pipeline",
            build_number=1,
            failed_children=[
                ChildJobAnalysis(
                    job_name="leaf",
                    build_number=2,
                    all_groups_failed=True,
                    failures=[
                        failure("a", "same", failed_details),
                        failure("b", "same", failed_details),
                    ],
                ),
                ChildJobAnalysis(
                    job_name="leaf",
                    build_number=3,
                    all_groups_failed=True,
                    failures=[failure("c", "same", failed_details)],
                ),
            ],
        ),
        ChildJobAnalysis(
            job_name="console",
            build_number=5,
            all_groups_failed=True,
            note="Child console analysis failed; check server logs for details",
        ),
        ChildJobAnalysis(
            job_name="ok",
            build_number=4,
            failures=[failure("good", "same", "diagnosed")],
        ),
    ]
    source_result = CISourceResult(
        failures=[],
        child_job_infos=[("pipeline", 1), ("console", 5), ("ok", 4)],
        identity={"job_name": "parent", "build_number": 1},
    )
    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.requires_pre_fetch",
            return_value=False,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
            new_callable=AsyncMock,
            return_value=source_result,
        ),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.analyze_children",
            new_callable=AsyncMock,
            return_value=(children, []),
        ),
        patch(
            "rootcoz.main._preflight_sidecar_check",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "rootcoz.main._validate_catalog_pair",
            new_callable=AsyncMock,
            return_value=("claude", "test"),
        ),
    ):
        await storage.init_db()
        await storage.save_result("parent", status="pending", result={})
        await _process_ci_source_analysis(
            job_id="parent",
            body=UnifiedAnalyzeRequest(
                type="jenkins", job_name="parent", build_number=1
            ),
            merged=Settings(
                jenkins_url="https://example.test",
                jenkins_user="user",
                jenkins_password="fake",  # pragma: allowlist secret
            ),
            display_name="parent",
            ai_provider="claude",
            ai_model="test",
            peer_ai_configs=None,
            tests_repo_url="",
            tests_repo_ref="",
            resolved_tests_repo_token="",
            additional_repos_list=[],
            base_url="",
        )
        row = await storage.get_result("parent")
    assert row["status"] == "completed", row["result"]
    assert row["result"]["failed_analysis_groups"] == 3


def test_nested_all_failed_children_are_not_counted_as_success() -> None:
    from rootcoz.sources.jenkins_source import child_has_successful_analysis

    failed = ChildJobAnalysis(
        job_name="leaf",
        build_number=1,
        all_groups_failed=True,
        failures=[
            FailureAnalysis(
                test_name="test_a",
                error="boom",
                analysis=AnalysisDetail(details="Analysis failed"),
            )
        ],
    )
    nested = ChildJobAnalysis(
        job_name="pipeline", build_number=1, failed_children=[failed]
    )
    assert not child_has_successful_analysis(nested)
    assert child_has_successful_analysis(
        ChildJobAnalysis(
            job_name="ok",
            build_number=1,
            failures=[
                FailureAnalysis(
                    test_name="test_b",
                    error="boom",
                    analysis=AnalysisDetail(details="ok"),
                )
            ],
        )
    )
