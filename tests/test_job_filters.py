"""Unit and HTTP parity tests for canonical backend job_filters (#659)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from rootcoz import job_filters, storage
from rootcoz.tag_facets import TagFacet

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}


# ─── Unit tests: allowlist, parsing, legacy aliases, preset validation ───────


class TestFilterParsingAndAliases:
    def test_canonical_allowlist_and_preset_keys(self) -> None:
        expected_canonical = {
            "search",
            "team",
            "tier",
            "version",
            "label",
            "exclude_label",
            "status",
            "review_status",
            "analysis_state",
            "submitted_by",
            "date_from",
            "date_to",
            "test_name",
            "test_name_mode",
        }
        assert job_filters.CANONICAL_JOB_FILTER_KEYS == expected_canonical
        assert job_filters.FILTER_PRESET_KEYS["dashboard"] == expected_canonical
        assert job_filters.FILTER_PRESET_KEYS["reports"] == expected_canonical | {
            "from",
            "to",
        }

    def test_parse_filter_list_handles_repeatable_csv_and_mixed(self) -> None:
        assert job_filters.parse_filter_list(None) == ()
        assert job_filters.parse_filter_list("") == ()
        assert job_filters.parse_filter_list(["", "  "]) == ()
        assert job_filters.parse_filter_list("alpha, beta , alpha") == ("alpha", "beta")
        assert job_filters.parse_filter_list(["alpha", "beta"]) == ("alpha", "beta")
        assert job_filters.parse_filter_list(["alpha,beta", "beta, gamma"]) == (
            "alpha",
            "beta",
            "gamma",
        )

    def test_parse_filter_list_rejects_oversized_entry(self) -> None:
        with pytest.raises(ValueError, match="Filter value too long"):
            job_filters.parse_filter_list("x" * (job_filters.MAX_CSV_FILTER_LENGTH + 1))

    def test_query_parser_normalizes_legacy_aliases_canonical_wins(self) -> None:
        # Legacy aliases work when canonical keys are omitted
        legacy = job_filters.parse_job_filters_query(
            team="storage,core",
            tags="aws,bare-metal",
            exclude_tags="nightly,flaky",
            legacy_from="2025-01-01",
            legacy_to="2025-12-31",
            review_status="all",
            analysis_state="all",
        )
        assert legacy.team == ("storage", "core")
        assert legacy.labels == ("aws", "bare-metal")
        assert legacy.exclude_labels == ("nightly", "flaky")
        assert legacy.date_from == "2025-01-01"
        assert legacy.date_to == "2025-12-31"
        assert legacy.review_status == ""
        assert legacy.analysis_state == ""

        # Canonical keys take precedence when both canonical and legacy are supplied
        both = job_filters.parse_job_filters_query(
            label=["io2-csi"],
            tags=["aws"],
            exclude_label=["smoke"],
            exclude_tags=["nightly"],
            date_from="2026-01-01",
            legacy_from="2025-01-01",
            date_to="2026-06-01",
            legacy_to="2025-12-31",
        )
        assert both.labels == ("io2-csi",)
        assert both.exclude_labels == ("smoke",)
        assert both.date_from == "2026-01-01"
        assert both.date_to == "2026-06-01"

    def test_submitted_by_preserves_commas_and_enforces_caps(self) -> None:
        # Usernames may contain commas — never split on commas
        parsed = job_filters.parse_job_filters_query(
            submitted_by=["Doe, Jane", "  alice  ", ""]
        )
        assert parsed.submitters == frozenset({"Doe, Jane", "alice"})

        with pytest.raises(ValueError, match="Too many submitted_by values"):
            job_filters.normalize_submitters([f"u{i}" for i in range(51)])

        with pytest.raises(ValueError, match="submitted_by value too long"):
            job_filters.normalize_submitters(["x" * 65])

    def test_invalid_enum_values_raise_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid review_status"):
            job_filters.parse_job_filters_query(review_status="bogus")

        with pytest.raises(ValueError, match="Invalid analysis_state"):
            job_filters.parse_job_filters_query(analysis_state="bogus")

        with pytest.raises(ValueError, match="Invalid test_name_mode"):
            job_filters.parse_job_filters_query(test_name_mode="regex")


class TestFilterPresetValidationAndParsing:
    def test_validate_preset_rejects_unknown_keys_and_bad_types(self) -> None:
        job_filters.validate_filter_preset_filters(
            "dashboard", {"search": "ocp", "team": ["mtv"], "label": ["aws"]}
        )
        job_filters.validate_filter_preset_filters(
            "reports", {"from": "2026-01-01", "to": "2026-02-01"}
        )

        with pytest.raises(ValueError, match="page must be one of"):
            job_filters.validate_filter_preset_filters("unknown", {"team": ["mtv"]})

        with pytest.raises(ValueError, match="Unknown filter key"):
            job_filters.validate_filter_preset_filters("dashboard", {"from": "2026"})

        with pytest.raises(ValueError, match="must be a string"):
            job_filters.validate_filter_preset_filters(
                "dashboard", {"search": ["a", "b"]}
            )

        with pytest.raises(ValueError, match="Invalid test_name_mode"):
            job_filters.validate_filter_preset_filters(
                "reports", {"test_name_mode": "glob"}
            )

        with pytest.raises(ValueError, match="submitted_by value too long"):
            job_filters.validate_filter_preset_filters(
                "dashboard", {"submitted_by": ["x" * 65]}
            )

    def test_parse_filter_preset_normalizes_legacy_keys(self) -> None:
        parsed = job_filters.parse_filter_preset(
            "reports",
            {
                "team": ["storage"],
                "from": "2026-01-01",
                "to": "2026-01-31",
                "review_status": "unreviewed",
                "submitted_by": ["alice"],
            },
        )
        assert parsed.team == ("storage",)
        assert parsed.date_from == "2026-01-01"
        assert parsed.date_to == "2026-01-31"
        assert parsed.review_status == "not_reviewed"
        assert parsed.submitters == frozenset({"alice"})


# ─── Unit tests: shared SQL predicate compilation ───────────────────────────


class TestSQLPredicateCompilation:
    @staticmethod
    def _sample_facets() -> list[TagFacet]:
        return [
            TagFacet(
                id="platform",
                name="Platform",
                options=[
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Bare metal", "tags": ["bare-metal"]},
                ],
            ),
            TagFacet(
                id="storage",
                name="Storage",
                options=[
                    {"label": "OCS RBD", "tags": ["ocs-rbd"]},
                    {"label": "io2", "tags": ["io2-csi"]},
                ],
            ),
        ]

    def test_facet_grouped_label_sql_or_within_and_across(self) -> None:
        facets = self._sample_facets()
        flt = job_filters.JobFilters.from_values(
            labels=["aws", "bare-metal", "ocs-rbd", "scheduled", "nightly"]
        )

        # Dashboard mode: ungrouped labels combine with AND (one IN subquery each)
        dash = job_filters.compile_job_filter_sql(
            flt, tag_facets=facets, ungrouped_label_mode="and"
        )
        assert len(dash.conditions) == 4
        assert dash.params == ["aws", "bare-metal", "ocs-rbd", "scheduled", "nightly"]

        # Reports mode: ungrouped labels combine with OR (one shared IN subquery)
        rep = job_filters.compile_job_filter_sql(
            flt, tag_facets=facets, ungrouped_label_mode="or"
        )
        assert len(rep.conditions) == 3
        assert rep.params == ["aws", "bare-metal", "ocs-rbd", "scheduled", "nightly"]

    def test_exclude_labels_preserves_null_job_names(self) -> None:
        flt = job_filters.JobFilters.from_values(exclude_labels=["nightly", "flaky"])
        compiled = job_filters.compile_job_filter_sql(flt, job_name_col="r.job_name")
        assert len(compiled.conditions) == 1
        assert "r.job_name IS NULL OR r.job_name NOT IN" in compiled.conditions[0]
        assert compiled.params == ["nightly", "flaky"]

    def test_status_predicate_handles_virtual_timeout_and_real_statuses(self) -> None:
        flt = job_filters.JobFilters.from_values(status=["completed", "timeout"])
        compiled = job_filters.compile_job_filter_sql(flt, results_alias="r")
        assert len(compiled.conditions) == 1
        clause = compiled.conditions[0]
        assert "r.status IN (?)" in clause
        assert "lower(r.error) LIKE '%timed out%'" in clause
        assert compiled.params == ["completed"]

    def test_optional_and_left_or_inner_results_join_modes(self) -> None:
        empty = job_filters.JobFilters()
        opt_empty = job_filters.compile_job_filter_sql(
            empty,
            job_name_col="tc.job_name",
            job_id_col="tc.job_id",
            results_alias="r_filter",
            results_join_col="tc.job_id",
            results_join_mode="optional_inner",
        )
        assert opt_empty.results_join == ""

        # Search triggers the optional results join on overrides
        with_search = job_filters.JobFilters.from_values(search="ocp")
        opt_search = job_filters.compile_job_filter_sql(
            with_search,
            job_name_col="tc.job_name",
            job_id_col="tc.job_id",
            results_alias="r_filter",
            results_join_col="tc.job_id",
            results_join_mode="optional_inner",
        )
        assert "JOIN results r_filter ON r_filter.job_id = tc.job_id" in (
            opt_search.results_join
        )
        assert opt_search.params == ["%ocp%", "%ocp%"]

        # Issues query uses LEFT JOIN when unfiltered, inner JOIN when filtered
        issues_empty = job_filters.compile_job_filter_sql(
            empty,
            job_name_col="r.job_name",
            job_id_col="c.job_id",
            results_alias="r",
            results_join_col="c.job_id",
            results_join_mode="left_or_inner",
        )
        assert "LEFT JOIN results r ON r.job_id = c.job_id" in issues_empty.results_join

        issues_filtered = job_filters.compile_job_filter_sql(
            job_filters.JobFilters.from_values(team=["mtv"]),
            job_name_col="r.job_name",
            job_id_col="c.job_id",
            results_alias="r",
            results_join_col="c.job_id",
            results_join_mode="left_or_inner",
        )
        assert issues_filtered.results_join.strip().startswith("JOIN results r")
        assert "JOIN job_metadata jm ON jm.job_name = r.job_name" in (
            issues_filtered.meta_join
        )

    def test_like_escape_in_test_name_and_submitter_predicates(self) -> None:
        clause, params = job_filters.build_failed_test_name_predicate(
            "exact", r"100%_a\b", job_column="tc.job_id"
        )
        assert "te.job_id = tc.job_id" in clause
        assert params == [r"100\%\_a\\b"]

        sub_clause, sub_params = job_filters.build_submitted_by_predicate(
            frozenset({"a_b%c"}), column="r.submitted_by"
        )
        assert "r.submitted_by" in sub_clause
        assert sub_params == [r"%a\_b\%c%"]


# ─── HTTP + DB Parity Tests across Dashboard, Reports, and Presets ──────────


@pytest.fixture
def _init_db(temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def seeded_client(_init_db, temp_db_path: Path):
    """Seed realistic jobs, metadata, facets, overrides, reviews, and issues."""
    from rootcoz.config import clear_db_settings_cache, clear_settings_cache

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ADMIN_KEY": _ADMIN_KEY,
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "SECURE_COOKIES": "false",
        "DB_PATH": str(temp_db_path),
        "REQUIRE_APPROVAL": "false",
    }

    async def _seed() -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            rows = [
                (
                    "job-aws-ocs",
                    "mtv-aws-ocs",
                    "mtv",
                    "tier1",
                    "4.17",
                    ["aws", "ocs-rbd", "scheduled"],
                    "alice",
                    "analyzed",
                    "2026-03-10 12:00:00",
                    True,
                    "test_migrate_aws",
                ),
                (
                    "job-aws-io2",
                    "mtv-aws-io2",
                    "mtv",
                    "tier2",
                    "4.17",
                    ["aws", "io2-csi"],
                    "bob",
                    "analyzed",
                    "2026-03-10 15:30:00",
                    False,
                    "test_migrate_io2",
                ),
                (
                    "job-bm-ocs",
                    "cnv-bm-ocs",
                    "cnv",
                    "tier1",
                    "4.16",
                    ["bare-metal", "ocs-rbd", "nightly"],
                    "alice",
                    "submitted",
                    "2026-03-15 09:00:00",
                    True,
                    "test_network_bm",
                ),
            ]
            for (
                job_id,
                job_name,
                team,
                tier,
                version,
                labels,
                submitter,
                state,
                created_at,
                reviewed,
                test_name,
            ) in rows:
                await storage.save_result(
                    job_id,
                    f"https://jenkins.example.com/job/{job_name}/1/",
                    "completed",
                    {
                        "job_name": job_name,
                        "build_number": 1,
                        "failures": [],
                        "request_params": {"submitted_by": submitter},
                        "analysis_state": state,
                    },
                )
                await storage.set_job_metadata(
                    job_name,
                    team=team,
                    tier=tier,
                    version=version,
                    labels=labels,
                )
                await storage.save_test_entries(
                    job_id, [{"test_name": test_name, "status": "failed"}]
                )
                async with storage._connect_db() as db:
                    await db.execute(
                        "UPDATE results SET created_at = ? WHERE job_id = ?",
                        (created_at, job_id),
                    )
                    await db.execute(
                        "INSERT INTO failure_history "
                        "(job_id, job_name, build_number, test_name, "
                        "error_message, error_signature, classification, "
                        "child_job_name, child_build_number) "
                        "VALUES (?, ?, 1, ?, 'err', 'sig', 'FLAKY', ?, 1)",
                        (job_id, job_name, test_name, job_name),
                    )
                    if reviewed:
                        await db.execute(
                            "INSERT INTO failure_reviews "
                            "(job_id, test_name, reviewed, username, updated_at) "
                            "VALUES (?, ?, 1, 'reviewer', ?)",
                            (job_id, test_name, created_at),
                        )
                        await db.execute(
                            "INSERT INTO test_classifications "
                            "(job_id, job_name, test_name, child_build_number, "
                            "classification, pattern, original_classification, "
                            "original_pattern, created_by, created_at, visible) "
                            "VALUES (?, ?, ?, 1, 'PRODUCT BUG', '', 'FLAKY', '', "
                            "'reviewer', ?, 1)",
                            (job_id, job_name, test_name, created_at),
                        )
                    await db.commit()
                await storage.add_comment(
                    job_id,
                    test_name,
                    f"GitHub Issue: [bug {job_name}](https://github.com/org/repo/issues/1)",
                    username=submitter,
                )
                async with storage._connect_db() as db:
                    await db.execute(
                        "UPDATE comments SET created_at = ? WHERE job_id = ?",
                        (created_at, job_id),
                    )
                    await db.commit()

    asyncio.run(_seed())

    with patch.dict(os.environ, env, clear=True):
        clear_settings_cache()
        clear_db_settings_cache()
        from rootcoz.main import app

        with TestClient(app, headers=_ADMIN_HEADERS) as client:
            facets_cfg = [
                {
                    "id": "platform",
                    "name": "Platform",
                    "options": [
                        {"label": "AWS", "tags": ["aws"]},
                        {"label": "Bare metal", "tags": ["bare-metal"]},
                    ],
                },
                {
                    "id": "storage",
                    "name": "Storage",
                    "options": [
                        {"label": "OCS RBD", "tags": ["ocs-rbd"]},
                        {"label": "io2", "tags": ["io2-csi"]},
                    ],
                },
            ]
            resp = client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(facets_cfg)}},
            )
            assert resp.status_code == 200, resp.text
            yield client
    clear_settings_cache()
    clear_db_settings_cache()


class TestDashboardAndReportsParity:
    def test_dashboard_accepts_canonical_and_legacy_shapes(
        self, seeded_client: TestClient
    ) -> None:
        # Canonical repeatable shape
        canonical = seeded_client.get(
            "/api/dashboard/filtered",
            params=[
                ("team", "mtv"),
                ("team", "cnv"),
                ("label", "aws"),
                ("label", "bare-metal"),
                ("label", "ocs-rbd"),
                ("date_from", "2026-03-10"),
                ("date_to", "2026-03-15"),
                ("review_status", "all"),
            ],
        )
        # Legacy CSV + from/to + tags shape
        legacy = seeded_client.get(
            "/api/dashboard/filtered",
            params={
                "team": "mtv,cnv",
                "tags": "aws,bare-metal,ocs-rbd",
                "from": "2026-03-10",
                "to": "2026-03-15",
                "review_status": "",
            },
        )
        assert canonical.status_code == 200, canonical.text
        assert legacy.status_code == 200, legacy.text
        assert {j["job_id"] for j in canonical.json()["jobs"]} == {
            "job-aws-ocs",
            "job-bm-ocs",
        }
        assert canonical.json() == legacy.json()

    def test_dashboard_same_day_date_matches_sqlite_timestamp(
        self, seeded_client: TestClient
    ) -> None:
        resp = seeded_client.get(
            "/api/dashboard/filtered",
            params={"date_from": "2026-03-10", "date_to": "2026-03-10"},
        )
        assert resp.status_code == 200, resp.text
        assert {j["job_id"] for j in resp.json()["jobs"]} == {
            "job-aws-ocs",
            "job-aws-io2",
        }

    def test_reports_accept_both_canonical_and_legacy_dialects(
        self, seeded_client: TestClient
    ) -> None:
        endpoints = (
            "/api/reports/totals",
            "/api/reports/classification-overrides",
            "/api/reports/issues-created",
        )
        for path in endpoints:
            canonical = seeded_client.get(
                path,
                params=[
                    ("team", "mtv"),
                    ("team", "cnv"),
                    ("label", "aws"),
                    ("label", "bare-metal"),
                    ("label", "ocs-rbd"),
                    ("exclude_label", "nightly"),
                    ("date_from", "2026-03-01"),
                    ("date_to", "2026-03-31"),
                    ("review_status", "all"),
                    ("analysis_state", "all"),
                ],
            )
            legacy = seeded_client.get(
                path,
                params={
                    "team": "mtv,cnv",
                    "tags": "aws,bare-metal,ocs-rbd",
                    "exclude_tags": "nightly",
                    "from": "2026-03-01",
                    "to": "2026-03-31",
                },
            )
            assert canonical.status_code == 200, (path, canonical.text)
            assert legacy.status_code == 200, (path, legacy.text)
            assert canonical.json() == legacy.json(), path

        # Verify the narrowed result set matches only job-aws-ocs across all three
        totals = seeded_client.get(
            "/api/reports/totals",
            params=[
                ("label", "aws"),
                ("label", "bare-metal"),
                ("exclude_label", "nightly"),
            ],
        ).json()
        assert {j["job_id"] for j in totals["jobs"]} == {"job-aws-ocs", "job-aws-io2"}

    def test_reports_support_search_filter(self, seeded_client: TestClient) -> None:
        totals = seeded_client.get("/api/reports/totals", params={"search": "cnv-bm"})
        assert totals.status_code == 200
        assert [j["job_id"] for j in totals.json()["jobs"]] == ["job-bm-ocs"]

        overrides = seeded_client.get(
            "/api/reports/classification-overrides", params={"search": "cnv-bm"}
        )
        assert overrides.status_code == 200
        assert [d["job_id"] for d in overrides.json()["details"]] == ["job-bm-ocs"]
        assert overrides.json()["total_reviewed"] == 1

        issues = seeded_client.get(
            "/api/reports/issues-created", params={"search": "mtv-aws-io2"}
        )
        assert issues.status_code == 200
        assert [i["job_id"] for i in issues.json()["issues"]] == ["job-aws-io2"]

    def test_dashboard_and_reports_facet_parity(
        self, seeded_client: TestClient
    ) -> None:
        # (AWS OR Bare metal) AND OCS RBD -> job-aws-ocs, job-bm-ocs
        dash = seeded_client.get(
            "/api/dashboard/filtered",
            params=[("label", "aws"), ("label", "bare-metal"), ("label", "ocs-rbd")],
        )
        rep = seeded_client.get(
            "/api/reports/totals",
            params=[("label", "aws"), ("label", "bare-metal"), ("label", "ocs-rbd")],
        )
        assert dash.status_code == 200 and rep.status_code == 200
        assert (
            {j["job_id"] for j in dash.json()["jobs"]}
            == {j["job_id"] for j in rep.json()["jobs"]}
            == {"job-aws-ocs", "job-bm-ocs"}
        )

    def test_dashboard_metadata_attached_only_from_matching_jobs(
        self, seeded_client: TestClient
    ) -> None:
        resp = seeded_client.get(
            "/api/dashboard/filtered",
            params={"team": "mtv", "tier": "tier1"},
        )
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-aws-ocs"
        assert jobs[0]["metadata"]["team"] == "mtv"
        assert jobs[0]["metadata"]["tier"] == "tier1"
        assert set(jobs[0]["metadata"]["labels"]) == {"aws", "ocs-rbd", "scheduled"}
