"""Tests for storage token usage functions."""

import asyncio
from unittest.mock import patch

import pytest

from rootcoz import storage
from rootcoz.token_tracking import (
    attach_failure_usage,
    build_token_usage_summary,
    child_usage_scope,
    failure_group_usage,
    reanalysis_usage_scope,
    record_ai_usage,
)


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path for token usage tests."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def _storage(temp_db_path, _init_db):
    """Patch DB_PATH for all storage calls in the test."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        yield


@pytest.mark.asyncio
async def test_group_usage_correlates_concurrent_primary_calls_only(_storage) -> None:
    from types import SimpleNamespace

    from rootcoz.models import AnalysisResult, FailureAnalysis

    async def call(signature: str, tokens: int) -> None:
        with failure_group_usage("group-job", signature):
            await asyncio.sleep(0)
            usage = SimpleNamespace(
                provider="claude",
                model="test",
                input_tokens=tokens,
                output_tokens=1,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=tokens / 100,
                duration_ms=10,
            )
            await record_ai_usage(
                "group-job",
                SimpleNamespace(success=True, usage=usage, text="ok"),
                "primary",
                prompt_chars=tokens,
            )
            await storage.record_token_usage(
                "group-job",
                "claude",
                "test",
                "agent_routing",
                input_tokens=300,
                cost_usd=3,
            )

    await asyncio.gather(call("a", 10), call("b", 20))
    records = await storage.get_token_usage_for_job("group-job")
    assert sorted(
        (r["call_type"], r["error_signature"]) for r in records if r["prompt_chars"]
    ) == [("primary", "a"), ("primary", "b")]
    assert all(
        r["error_signature"] == "" for r in records if r["call_type"] == "agent_routing"
    )

    def failure(name: str, signature: str) -> dict:
        return FailureAnalysis(
            test_name=name, error="oops", analysis="ok", error_signature=signature
        ).model_dump(mode="json")

    result = AnalysisResult(
        job_id="group-job", status="completed", summary="ok"
    ).model_dump(mode="json")
    result["failures"] = [
        failure("one", "a"),
        failure("two", "a"),
        failure("three", "b"),
        failure("old", "c"),
    ]
    await storage.save_result("group-job", status="completed", result=result)
    saved = (await storage.get_result("group-job"))["result"]
    assert [
        f["token_usage"]["total_calls"] if f["token_usage"] else None
        for f in saved["failures"]
    ] == [1, 1, 1, None]
    assert saved["token_usage"]["total_calls"] == len(records)
    assert saved["token_usage"]["total_cost_usd"] == pytest.approx(6.3)
    assert saved["failures"][0]["token_usage"]["total_cost_usd"] == pytest.approx(0.1)
    assert saved["failures"][2]["token_usage"]["total_cost_usd"] == pytest.approx(0.2)
    assert saved["failures"][0]["token_usage"]["total_calls"] == 1


@pytest.mark.asyncio
async def test_child_failures_share_group_usage_and_stale_values_are_cleared(
    _storage,
) -> None:
    await storage.record_token_usage(
        "child",
        "claude",
        "test",
        "primary",
        input_tokens=7,
        cost_usd=0.07,
        error_signature="same",
    )
    child = {
        "failures": [
            {"error_signature": "same", "token_usage": {"total_calls": 999}},
            {"error_signature": "other", "token_usage": {"total_calls": 999}},
        ],
        "failed_children": [{"failures": [{"error_signature": "same"}]}],
    }
    await storage.save_result(
        "child", status="completed", result={"child_job_analyses": [child]}
    )
    failures = (await storage.get_result("child"))["result"]["child_job_analyses"][0]
    assert failures["failures"][0]["token_usage"]["total_calls"] == 1
    assert failures["failures"][1]["token_usage"] is None
    assert (
        failures["failed_children"][0]["failures"][0]["token_usage"]["total_cost_usd"]
        == 0.07
    )


@pytest.mark.asyncio
async def test_legacy_primary_usage_is_not_attributed(_storage) -> None:
    from rootcoz.models import AnalysisResult, FailureAnalysis

    result = AnalysisResult(
        job_id="legacy",
        status="completed",
        summary="ok",
        failures=[
            FailureAnalysis(
                test_name="old", error="error", analysis="ok", error_signature="a"
            )
        ],
    ).model_dump(mode="json")
    await storage.record_token_usage(
        "legacy",
        "claude",
        "test",
        "primary",
        cost_usd=2,
        input_tokens=10,
    )
    await storage.save_result("legacy", status="completed", result=result)
    saved = (await storage.get_result("legacy"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    assert saved["token_usage"]["total_cost_usd"] == 2


@pytest.mark.asyncio
async def test_child_usage_is_scoped_by_child_build_and_signature(_storage) -> None:
    from types import SimpleNamespace

    usage = SimpleNamespace(
        provider="claude",
        model="test",
        input_tokens=5,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.05,
        duration_ms=1,
    )

    async def record(name: str, build: int) -> None:
        with child_usage_scope(name, build), failure_group_usage("pipeline", "same"):
            await asyncio.sleep(0)
            await record_ai_usage(
                "pipeline",
                SimpleNamespace(success=True, usage=usage, text="ok"),
                "primary",
            )

    await asyncio.gather(record("alpha", 1), record("beta", 1), record("alpha", 2))
    records = await storage.get_token_usage_for_job("pipeline")
    assert {(r["child_job_name"], r["child_build_number"]) for r in records} == {
        ("alpha", 1),
        ("beta", 1),
        ("alpha", 2),
    }
    result = {
        "failures": [{"error_signature": "same"}],
        "child_job_analyses": [
            {
                "job_name": name,
                "build_number": build,
                "failures": [{"error_signature": "same"}, {"error_signature": "same"}],
            }
            for name, build in [("alpha", 1), ("beta", 1), ("alpha", 2)]
        ],
    }
    await storage.save_result("pipeline", status="completed", result=result)
    saved = (await storage.get_result("pipeline"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    for child in saved["child_job_analyses"]:
        assert [f["token_usage"]["total_calls"] for f in child["failures"]] == [1, 1]
    assert saved["token_usage"]["total_calls"] == 3


def test_shared_signature_usage_does_not_mix_children() -> None:
    records = [
        {
            "call_type": "primary",
            "error_signature": "same",
            "child_job_name": "a",
            "child_build_number": 1,
            "ai_provider": "test",
            "ai_model": "test",
            "input_tokens": 2,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 2,
            "cost_usd": None,
            "duration_ms": None,
        },
    ]
    result = {
        "child_job_analyses": [
            {
                "job_name": "a",
                "build_number": 1,
                "failures": [{"error_signature": "same"}],
            },
            {
                "job_name": "b",
                "build_number": 1,
                "failures": [{"error_signature": "same"}],
            },
        ]
    }
    attach_failure_usage(result, records)
    assert (
        result["child_job_analyses"][0]["failures"][0]["token_usage"]["total_calls"]
        == 1
    )
    assert result["child_job_analyses"][1]["failures"][0]["token_usage"] is None


@pytest.mark.asyncio
async def test_reanalysis_replaces_only_target_card_usage_and_keeps_job_totals(
    _storage,
) -> None:
    from types import SimpleNamespace

    usage = SimpleNamespace(
        provider="claude",
        model="test",
        input_tokens=20,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.2,
        duration_ms=1,
    )
    await storage.record_token_usage(
        "pipeline",
        "claude",
        "test",
        "primary",
        input_tokens=10,
        cost_usd=0.1,
        error_signature="same",
        child_job_name="child",
        child_build_number=1,
    )
    result = {
        "failures": [{"id": "parent-id", "error_signature": "same"}],
        "child_job_analyses": [
            {
                "job_name": "child",
                "build_number": 1,
                "failures": [
                    {
                        "id": "changed",
                        "error_signature": "same",
                        "previous_analyses": [{"analysis": "old"}],
                    },
                    {"id": "untouched", "error_signature": "same"},
                ],
            }
        ],
    }
    with (
        child_usage_scope("child", 1),
        reanalysis_usage_scope("changed") as old_attempt,
        failure_group_usage("pipeline", "same"),
    ):
        await record_ai_usage(
            "pipeline", SimpleNamespace(success=True, usage=usage, text="ok"), "primary"
        )
    # A subsequent successful re-analysis replaces the earlier attempt rather than accumulating it.
    usage.input_tokens = 30
    with (
        child_usage_scope("child", 1),
        reanalysis_usage_scope("changed") as current_attempt,
        failure_group_usage("pipeline", "same"),
    ):
        await record_ai_usage(
            "pipeline", SimpleNamespace(success=True, usage=usage, text="ok"), "primary"
        )
    assert old_attempt != current_attempt
    records = await storage.get_token_usage_for_job("pipeline")
    assert [
        (r["call_type"], r["child_job_name"], r["child_build_number"]) for r in records
    ] == [
        ("primary", "child", 1),
        ("reanalysis", "child", 1),
        ("reanalysis", "child", 1),
    ]
    result["child_job_analyses"][0]["failures"][0]["usage_attempt"] = current_attempt
    await storage.save_result("pipeline", status="completed", result=result)
    saved = (await storage.get_result("pipeline"))["result"]
    child_failures = saved["child_job_analyses"][0]["failures"]
    assert saved["failures"][0]["token_usage"] is None
    assert child_failures[0]["token_usage"]["total_input_tokens"] == 30
    assert child_failures[0]["token_usage"]["total_calls"] == 1
    assert child_failures[1]["token_usage"]["total_input_tokens"] == 10
    assert saved["token_usage"]["total_input_tokens"] == 60
    assert saved["token_usage"]["total_calls"] == 3


@pytest.mark.asyncio
async def test_legacy_reanalysis_history_never_attributes_mixed_primary_usage(
    _storage,
) -> None:
    for child_name, tokens in (("", 10), ("runner", 20)):
        await storage.record_token_usage(
            "legacy-mixed",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
            child_job_name=child_name,
            child_build_number=7 if child_name else 0,
        )
    # Older re-analyses wrote primary rows without a failure ID, attempt, or child scope.
    await storage.record_token_usage(
        "legacy-mixed",
        "claude",
        "test",
        "primary",
        input_tokens=30,
        error_signature="same",
    )
    result = {
        "failures": [
            {
                "id": "top",
                "error_signature": "same",
                "token_usage": {"total_input_tokens": 999},
            },
            {"id": "sibling", "error_signature": "same"},
        ],
        "child_job_analyses": [
            {
                "job_name": "runner",
                "build_number": 7,
                "failures": [
                    {
                        "id": "old",
                        "error_signature": "same",
                        "previous_analyses": [
                            {
                                "analysis": "old",
                                "token_usage": {"total_input_tokens": 20},
                            }
                        ],
                    },
                    {"id": "child-sibling", "error_signature": "same"},
                ],
            }
        ],
    }
    await storage.save_result("legacy-mixed", status="completed", result=result)
    saved = (await storage.get_result("legacy-mixed"))["result"]
    assert [f["token_usage"] for f in saved["failures"]] == [None, None]
    assert [
        f["token_usage"]["total_input_tokens"]
        for f in saved["child_job_analyses"][0]["failures"]
    ] == [20, 20]
    assert (
        saved["child_job_analyses"][0]["failures"][0]["previous_analyses"][0][
            "token_usage"
        ]["total_input_tokens"]
        == 20
    )
    assert saved["token_usage"]["total_input_tokens"] == 60
    assert saved["token_usage"]["total_calls"] == 3


@pytest.mark.asyncio
async def test_failed_legacy_retry_without_history_withholds_card_but_keeps_job_total(
    _storage,
) -> None:
    for tokens in (10, 20):
        await storage.record_token_usage(
            "failed-legacy",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
        )
    await storage.save_result(
        "failed-legacy",
        status="completed",
        result={"failures": [{"id": "original", "error_signature": "same"}]},
    )
    saved = (await storage.get_result("failed-legacy"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    assert saved["token_usage"]["total_input_tokens"] == 30
    assert saved["token_usage"]["total_calls"] == 2


@pytest.mark.asyncio
async def test_top_history_does_not_hide_child_original_with_same_signature(
    _storage,
) -> None:
    for child_name, tokens in (("", 10), ("runner", 17)):
        await storage.record_token_usage(
            "scoped-history",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
            child_job_name=child_name,
            child_build_number=7 if child_name else 0,
        )
    await storage.save_result(
        "scoped-history",
        status="completed",
        result={
            "failures": [
                {
                    "error_signature": "same",
                    "previous_analyses": [{"analysis": "old"}],
                }
            ],
            "child_job_analyses": [
                {
                    "job_name": "runner",
                    "build_number": 7,
                    "failures": [{"error_signature": "same"}],
                }
            ],
        },
    )
    saved = (await storage.get_result("scoped-history"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    assert (
        saved["child_job_analyses"][0]["failures"][0]["token_usage"][
            "total_input_tokens"
        ]
        == 17
    )
    assert saved["token_usage"]["total_input_tokens"] == 27


@pytest.mark.asyncio
async def test_known_archived_snapshot_survives_ambiguous_current_card(
    _storage,
) -> None:
    for tokens in (10, 20):
        await storage.record_token_usage(
            "archived-known",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
        )
    await storage.save_result(
        "archived-known",
        status="completed",
        result={
            "failures": [
                {
                    "error_signature": "same",
                    "previous_analyses": [
                        {"analysis": "old", "token_usage": {"total_input_tokens": 4}}
                    ],
                }
            ]
        },
    )
    saved = (await storage.get_result("archived-known"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    assert (
        saved["failures"][0]["previous_analyses"][0]["token_usage"][
            "total_input_tokens"
        ]
        == 4
    )
    assert (await storage.get_result("archived-known"))["result"]["failures"][0][
        "previous_analyses"
    ][0]["token_usage"]["total_input_tokens"] == 4


@pytest.mark.asyncio
async def test_legacy_archived_snapshot_is_not_rewritten_on_read(
    _storage,
) -> None:
    for tokens in (10, 20):
        await storage.record_token_usage(
            "archived-mixed",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
        )
    await storage.save_result(
        "archived-mixed",
        status="completed",
        result={
            "failures": [
                {
                    "id": "existing-id",
                    "error_signature": "same",
                    "usage_attempt": "modern",
                    "previous_analyses": [
                        {"analysis": "old", "token_usage": {"total_input_tokens": 30}}
                    ],
                }
            ]
        },
    )
    # A previously persisted legacy sum is not proof of an exact per-attempt
    # amount. Reads must not overwrite a stored snapshot without provenance.
    async with storage._connect_db() as db:
        row = await (
            await db.execute(
                "SELECT result_json FROM results WHERE job_id = ?", ("archived-mixed",)
            )
        ).fetchone()
    before = row["result_json"]
    saved = (await storage.get_result("archived-mixed"))["result"]
    assert (
        saved["failures"][0]["previous_analyses"][0]["token_usage"][
            "total_input_tokens"
        ]
        == 30
    )
    async with storage._connect_db() as db:
        row = await (
            await db.execute(
                "SELECT result_json FROM results WHERE job_id = ?", ("archived-mixed",)
            )
        ).fetchone()
    assert row["result_json"] == before


@pytest.mark.asyncio
async def test_child_legacy_history_can_contaminate_unscoped_top_level_only(
    _storage,
) -> None:
    for child_name, tokens in (("", 20), ("runner", 17)):
        await storage.record_token_usage(
            "child-history",
            "claude",
            "test",
            "primary",
            input_tokens=tokens,
            error_signature="same",
            child_job_name=child_name,
            child_build_number=7 if child_name else 0,
        )
    await storage.save_result(
        "child-history",
        status="completed",
        result={
            "failures": [{"error_signature": "same"}],
            "child_job_analyses": [
                {
                    "job_name": "runner",
                    "build_number": 7,
                    "failures": [
                        {
                            "error_signature": "same",
                            "previous_analyses": [{"analysis": "older"}],
                        }
                    ],
                }
            ],
        },
    )
    saved = (await storage.get_result("child-history"))["result"]
    assert saved["failures"][0]["token_usage"] is None
    assert (
        saved["child_job_analyses"][0]["failures"][0]["token_usage"][
            "total_input_tokens"
        ]
        == 17
    )
    assert saved["token_usage"]["total_input_tokens"] == 37


@pytest.mark.asyncio
async def test_failed_reanalysis_keeps_prior_card_usage(_storage) -> None:
    await storage.record_token_usage(
        "job",
        "claude",
        "test",
        "primary",
        input_tokens=10,
        error_signature="signature",
    )
    with reanalysis_usage_scope("failure-id") as attempt:
        await storage.record_token_usage(
            "job",
            "claude",
            "test",
            "reanalysis",
            input_tokens=30,
            error_signature="signature",
            failure_id="failure-id",
            usage_attempt=attempt,
        )
    await storage.save_result(
        "job",
        status="completed",
        result={"failures": [{"id": "failure-id", "error_signature": "signature"}]},
    )
    saved = (await storage.get_result("job"))["result"]
    assert saved["failures"][0]["token_usage"]["total_input_tokens"] == 10
    assert saved["token_usage"]["total_input_tokens"] == 40


class TestRecordTokenUsage:
    @pytest.mark.asyncio
    async def test_inserts_record_and_returns_uuid(self, _storage) -> None:
        """record_token_usage inserts a record and returns a UUID."""
        record_id = await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            cost_usd=0.05,
            duration_ms=1200,
            prompt_chars=500,
            response_chars=200,
        )
        assert isinstance(record_id, str)
        assert len(record_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_stored_fields_are_correct(self, _storage) -> None:
        """All fields are stored correctly."""
        await storage.record_token_usage(
            job_id="job-2",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
            cache_read_tokens=15,
            cache_write_tokens=3,
            cost_usd=0.03,
            duration_ms=800,
            prompt_chars=1000,
            response_chars=400,
        )
        records = await storage.get_token_usage_for_job("job-2")
        assert len(records) == 1
        rec = records[0]
        assert rec["job_id"] == "job-2"
        assert rec["ai_provider"] == "gemini"
        assert rec["ai_model"] == "2.5-pro"
        assert rec["call_type"] == "peer_review"
        assert rec["input_tokens"] == 200
        assert rec["output_tokens"] == 80
        assert rec["cache_read_tokens"] == 15
        assert rec["cache_write_tokens"] == 3
        assert rec["total_tokens"] == 280
        assert rec["cost_usd"] == pytest.approx(0.03)
        assert rec["duration_ms"] == 800
        assert rec["prompt_chars"] == 1000
        assert rec["response_chars"] == 400

    @pytest.mark.asyncio
    async def test_total_tokens_computed(self, _storage) -> None:
        """total_tokens = input_tokens + output_tokens."""
        await storage.record_token_usage(
            job_id="job-3",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=300,
            output_tokens=150,
        )
        records = await storage.get_token_usage_for_job("job-3")
        assert records[0]["total_tokens"] == 450


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([], None),
        (["user"], "user"),
        (["server"], "server"),
        (["user", "server"], "mixed"),
        (["user", "server", "unknown"], "mixed"),
        (["user", "unknown"], "unknown"),
        (["server", "unknown"], "unknown"),
        (["unknown"], "unknown"),
        ([""], "unknown"),
        (["unexpected"], "unknown"),
    ],
)
async def test_job_totals_credential_source(_storage, sources, expected) -> None:
    for source in sources:
        await storage.record_token_usage(
            "credential-job", "gemini", "test", "analysis", credential_source=source
        )
    totals = await storage.get_job_token_usage_totals("credential-job")
    if expected is None:
        assert totals is None
    else:
        assert totals is not None
        assert totals["credential_source"] == expected
        detailed = await build_token_usage_summary("credential-job")
        assert detailed is not None
        assert detailed.credential_source == expected


class TestGetTokenUsageForJob:
    @pytest.mark.asyncio
    async def test_returns_records_for_job(self, _storage) -> None:
        """Returns all records for a specific job."""
        await storage.record_token_usage(
            job_id="job-a",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
        )
        await storage.record_token_usage(
            job_id="job-a",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
        )
        records = await storage.get_token_usage_for_job("job-a")
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_nonexistent_job(self, _storage) -> None:
        """Returns empty list for non-existent job."""
        records = await storage.get_token_usage_for_job("nonexistent-job")
        assert records == []

    @pytest.mark.asyncio
    async def test_does_not_return_other_jobs(self, _storage) -> None:
        """Records from other jobs are not included."""
        await storage.record_token_usage(
            job_id="job-x",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
        )
        await storage.record_token_usage(
            job_id="job-y",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=200,
            output_tokens=80,
        )
        records = await storage.get_token_usage_for_job("job-x")
        assert len(records) == 1
        assert records[0]["job_id"] == "job-x"


@pytest.mark.asyncio
async def test_detailed_usage_keeps_snapshot_during_concurrent_insert(_storage) -> None:
    await storage.record_token_usage(
        job_id="snapshot",
        ai_provider="gemini",
        ai_model="test",
        call_type="analysis",
        input_tokens=3,
        output_tokens=1,
    )
    original = storage._get_job_token_usage_totals

    async def insert_after_totals(db, job_id):
        totals = await original(db, job_id)
        await storage.record_token_usage(
            job_id="snapshot",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=10,
            output_tokens=2,
        )
        return totals

    with patch.object(storage, "_get_job_token_usage_totals", insert_after_totals):
        summary = await build_token_usage_summary("snapshot")
    assert summary is not None
    assert summary.total_calls == len(summary.calls) == 1
    assert summary.total_tokens == sum(call.total_tokens for call in summary.calls) == 4
    assert len(await storage.get_token_usage_for_job("snapshot")) == 2


class TestGetTokenUsageSummary:
    @pytest.mark.asyncio
    async def _insert_test_records(self) -> None:
        """Helper to insert test records for summary tests."""
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.05,
            duration_ms=1000,
        )
        await storage.record_token_usage(
            job_id="job-2",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
            cost_usd=0.03,
            duration_ms=800,
        )
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="jira_filter",
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.01,
            duration_ms=300,
        )

    @pytest.mark.asyncio
    async def test_totals_correct(self, _storage) -> None:
        """Totals are correctly aggregated."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary()
        assert summary["total_calls"] == 3
        assert summary["total_input_tokens"] == 350
        assert summary["total_output_tokens"] == 150
        assert summary["total_cost_usd"] == pytest.approx(0.09)

    @pytest.mark.asyncio
    async def test_unknown_only_cost_is_unavailable_not_zero(self, _storage) -> None:
        """Spend on a model with unknown price is unavailable, never a real $0."""
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="gemini",
            ai_model="key-discovered",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=None,
            duration_ms=1000,
        )
        summary = await storage.get_token_usage_summary()
        assert summary["total_cost_usd"] is None
        assert summary["priced_calls"] == 0
        assert summary["total_calls"] == 1
        # token columns are NOT NULL and still total normally
        assert summary["total_input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_mixed_priced_and_unknown_cost_is_unavailable(self, _storage) -> None:
        """A partial sum over unknown rows is a floor, so it is not reported as the total."""
        await self._insert_test_records()
        await storage.record_token_usage(
            job_id="job-3",
            ai_provider="gemini",
            ai_model="key-discovered",
            call_type="analysis",
            input_tokens=10,
            output_tokens=5,
            cost_usd=None,
            duration_ms=100,
        )
        summary = await storage.get_token_usage_summary()
        assert summary["total_cost_usd"] is None
        assert summary["priced_calls"] == 3
        assert summary["total_calls"] == 4

    @pytest.mark.asyncio
    async def test_breakdown_marks_unpriced_groups_and_sorts_them_last(
        self, _storage
    ) -> None:
        """Group rows expose priced/total calls and never price an unknown group at zero."""
        await self._insert_test_records()
        await storage.record_token_usage(
            job_id="job-3",
            ai_provider="gemini",
            ai_model="key-discovered",
            call_type="analysis",
            input_tokens=10,
            cost_usd=None,
            duration_ms=100,
        )
        summary = await storage.get_token_usage_summary(group_by="model")
        rows = {row["group_key"]: row for row in summary["breakdown"]}
        unknown = rows["gemini / key-discovered"]
        assert unknown["cost_usd"] is None
        assert unknown["priced_calls"] == 0
        assert unknown["call_count"] == 1
        # complete groups rank ahead of unknown ones regardless of token volume
        assert list(rows).index("gemini / key-discovered") == len(rows) - 1

    @pytest.mark.asyncio
    async def test_dashboard_period_reports_unknown_cost_as_unavailable(
        self, _storage
    ) -> None:
        """Period cards must not present unknown spend as a complete total."""
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="gemini",
            ai_model="key-discovered",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=None,
            duration_ms=1000,
        )
        result = await storage.get_token_usage_dashboard_summary()
        assert result["today"]["cost_usd"] is None
        assert result["today"]["priced_calls"] == 0
        assert result["today"]["calls"] == 1
        top_model = next(
            m for m in result["top_models"] if m["model"] == "gemini / key-discovered"
        )
        assert top_model["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_partial_prompt_cost_is_flagged(self, _storage) -> None:
        """A prompt whose cost covers only some turns is flagged as a lower bound."""
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="gemini",
            ai_model="priced-model",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.02,
            duration_ms=1000,
            cost_partial=True,
        )
        totals = await storage.get_job_token_usage_totals("job-1")
        assert totals is not None
        assert totals["cost_partial"] == 1
        assert totals["total_cost_usd"] == pytest.approx(0.02)
        assert totals["priced_calls"] == 1

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, _storage) -> None:
        """Empty database returns zero totals."""
        summary = await storage.get_token_usage_summary()
        assert summary["total_calls"] == 0
        assert summary["total_input_tokens"] == 0
        assert summary["total_cost_usd"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_provider(self, _storage) -> None:
        """Filter by ai_provider works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(ai_provider="claude")
        assert summary["total_calls"] == 2
        assert summary["total_input_tokens"] == 150

    @pytest.mark.asyncio
    async def test_filter_by_call_type(self, _storage) -> None:
        """Filter by call_type works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(call_type="analysis")
        assert summary["total_calls"] == 1
        assert summary["total_input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_filter_by_model(self, _storage) -> None:
        """Filter by ai_model works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(ai_model="2.5-pro")
        assert summary["total_calls"] == 1
        assert summary["total_input_tokens"] == 200

    @pytest.mark.asyncio
    async def test_group_by_provider(self, _storage) -> None:
        """group_by=provider produces breakdown by provider."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="provider")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 2
        keys = {row["group_key"] for row in breakdown}
        assert "claude" in keys
        assert "gemini" in keys

    @pytest.mark.asyncio
    async def test_group_by_call_type(self, _storage) -> None:
        """group_by=call_type produces breakdown by call_type."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="call_type")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 3
        keys = {row["group_key"] for row in breakdown}
        assert "analysis" in keys
        assert "peer_review" in keys
        assert "jira_filter" in keys

    @pytest.mark.asyncio
    async def test_group_by_job(self, _storage) -> None:
        """group_by=job produces breakdown by job_id."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="job")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 2
        keys = {row["group_key"] for row in breakdown}
        assert "job-1" in keys
        assert "job-2" in keys

    @pytest.mark.asyncio
    async def test_no_breakdown_without_group_by(self, _storage) -> None:
        """No breakdown returned when group_by is not specified."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary()
        assert summary["breakdown"] == []


class TestGetTokenUsageDashboardSummary:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, _storage) -> None:
        """Dashboard summary returns today, this_week, this_month, top_models, top_jobs."""
        result = await storage.get_token_usage_dashboard_summary()
        assert "today" in result
        assert "this_week" in result
        assert "this_month" in result
        assert "top_models" in result
        assert "top_jobs" in result

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, _storage) -> None:
        """Empty database returns zero values."""
        result = await storage.get_token_usage_dashboard_summary()
        assert result["today"]["calls"] == 0
        assert result["this_week"]["calls"] == 0
        assert result["this_month"]["calls"] == 0
        assert result["top_models"] == []
        assert result["top_jobs"] == []

    @pytest.mark.asyncio
    async def test_recent_records_appear_in_periods(self, _storage) -> None:
        """Records inserted now appear in today, this_week, and this_month."""
        await storage.record_token_usage(
            job_id="job-dash",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.05,
        )
        result = await storage.get_token_usage_dashboard_summary()
        assert result["today"]["calls"] == 1
        assert result["this_week"]["calls"] == 1
        assert result["this_month"]["calls"] == 1
        assert len(result["top_models"]) == 1
        assert result["top_models"][0]["model"] == "claude / opus-4"
        assert len(result["top_jobs"]) == 1
        assert result["top_jobs"][0]["job_id"] == "job-dash"
