"""Tests for admin-defined tag facets (#658)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from rootcoz import storage
from rootcoz.config import (
    Settings,
    clear_db_settings_cache,
    clear_settings_cache,
)
from rootcoz.tag_facets import (
    MAX_FACET_NAME_LENGTH,
    MAX_FACET_OPTIONS,
    MAX_OPTION_TAGS,
    MAX_TAG_FACETS,
    TagFacet,
    diff_tag_facets,
    parse_tag_facets,
    partition_labels_by_facets,
)

_ADMIN_KEY = "test-admin-key-tag-facets-16ch"  # pragma: allowlist secret


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_tag_facets.db"


@pytest.fixture
def facet_client(temp_db_path: Path):
    """TestClient backed by an isolated SQLite DB with clean settings cache."""
    env = {
        "ADMIN_KEY": _ADMIN_KEY,
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-tag-facets",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
        "REQUIRE_APPROVAL": "false",
    }
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(storage, "DB_PATH", temp_db_path),
    ):
        clear_db_settings_cache()
        clear_settings_cache()
        from rootcoz.main import app

        with TestClient(
            app, headers={"Authorization": f"Bearer {_ADMIN_KEY}"}
        ) as client:
            yield client
        clear_db_settings_cache()
        clear_settings_cache()


async def _seed_run(
    job_id: str,
    job_name: str,
    *,
    labels: list[str],
    status: str = "completed",
    result_json: dict | None = None,
) -> None:
    """Create a job_metadata row plus a results row for filtering/count tests."""
    await storage.set_job_metadata(job_name, labels=labels)
    payload = result_json or {
        "job_name": job_name,
        "build_number": 1,
        "summary": "1 failure",
        "failures": [
            {
                "test_name": f"test_{job_id}",
                "error": "AssertionError: boom",
                "classification": "PRODUCT BUG",
                "jira_matches": [],
                "github_issues": [],
            }
        ],
    }
    await storage.save_result(
        job_id=job_id,
        build_url=f"https://ci.example.com/job/{job_name}/1/",
        status=status,
        result=payload,
    )


# ---------------------------------------------------------------------------
# Unit tests: schema validation, partitioning, diffing, startup safety
# ---------------------------------------------------------------------------


class TestTagFacetSchemaValidation:
    def test_valid_facets_parse_and_deduplicate_tags(self) -> None:
        raw = json.dumps(
            [
                {
                    "id": "platform",
                    "name": " Platform ",
                    "options": [
                        {"label": " AWS ", "tags": ["aws", "aws"]},
                        {"label": "Bare metal", "tags": ["bare-metal"]},
                    ],
                }
            ]
        )
        facets = parse_tag_facets(raw, strict=True)
        assert len(facets) == 1
        assert facets[0].id == "platform"
        assert facets[0].name == "Platform"
        assert facets[0].options[0].label == "AWS"
        assert facets[0].options[0].tags == ["aws"]

    @pytest.mark.parametrize(
        "bad_id",
        ["", "Platform", "has_underscore", "has space", "a" * 33],
    )
    def test_invalid_slug_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValueError, match="facet id"):
            parse_tag_facets(
                [
                    {
                        "id": bad_id,
                        "name": "Ok",
                        "options": [{"label": "A", "tags": ["a"]}],
                    }
                ],
                strict=True,
            )

    def test_duplicate_facet_id_rejected(self) -> None:
        item = {
            "id": "platform",
            "name": "P",
            "options": [{"label": "A", "tags": ["a"]}],
        }
        with pytest.raises(ValueError, match="duplicate facet id"):
            parse_tag_facets([item, item], strict=True)

    def test_tag_shared_across_two_facets_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot belong to multiple facets"):
            parse_tag_facets(
                [
                    {
                        "id": "platform",
                        "name": "P",
                        "options": [{"label": "AWS", "tags": ["aws"]}],
                    },
                    {
                        "id": "cloud",
                        "name": "C",
                        "options": [{"label": "AWS2", "tags": ["aws"]}],
                    },
                ],
                strict=True,
            )

    def test_name_length_and_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="facet name"):
            parse_tag_facets(
                [
                    {
                        "id": "p",
                        "name": "   ",
                        "options": [{"label": "A", "tags": ["a"]}],
                    }
                ],
                strict=True,
            )
        with pytest.raises(ValueError, match="facet name"):
            parse_tag_facets(
                [
                    {
                        "id": "p",
                        "name": "x" * (MAX_FACET_NAME_LENGTH + 1),
                        "options": [{"label": "A", "tags": ["a"]}],
                    }
                ],
                strict=True,
            )

    def test_limits_enforced(self) -> None:
        too_many_facets = [
            {
                "id": f"f-{i}",
                "name": f"Facet {i}",
                "options": [{"label": "A", "tags": ["a"]}],
            }
            for i in range(MAX_TAG_FACETS + 1)
        ]
        with pytest.raises(ValueError, match=f"at most {MAX_TAG_FACETS}"):
            parse_tag_facets(too_many_facets, strict=True)

        too_many_options = [
            {"label": f"Opt {i}", "tags": ["a"]} for i in range(MAX_FACET_OPTIONS + 1)
        ]
        with pytest.raises(ValueError, match=f"at most {MAX_FACET_OPTIONS}"):
            parse_tag_facets(
                [{"id": "p", "name": "P", "options": too_many_options}], strict=True
            )

        too_many_tags = [f"tag-{i}" for i in range(MAX_OPTION_TAGS + 1)]
        with pytest.raises(ValueError, match=f"at most {MAX_OPTION_TAGS}"):
            parse_tag_facets(
                [
                    {
                        "id": "p",
                        "name": "P",
                        "options": [{"label": "Opt", "tags": too_many_tags}],
                    }
                ],
                strict=True,
            )

    def test_duplicate_option_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate option label"):
            parse_tag_facets(
                [
                    {
                        "id": "p",
                        "name": "P",
                        "options": [
                            {"label": "AWS", "tags": ["aws"]},
                            {"label": "aws", "tags": ["aws-2"]},
                        ],
                    }
                ],
                strict=True,
            )

    def test_startup_safe_env_default_does_not_require_db_or_crash(self) -> None:
        env_facets = json.dumps(
            [
                {
                    "id": "platform",
                    "name": "Platform",
                    "options": [{"label": "AWS", "tags": ["aws"]}],
                }
            ]
        )
        with patch.dict(os.environ, {"TAG_FACETS": env_facets}, clear=True):
            s = Settings()
            assert len(s.parsed_tag_facets) == 1
            assert s.parsed_tag_facets[0].id == "platform"

        # Malformed env JSON degrades cleanly instead of crashing startup
        with patch.dict(os.environ, {"TAG_FACETS": "{not-valid-json"}, clear=True):
            s = Settings()
            assert s.parsed_tag_facets == []


class TestPartitionAndDiff:
    def test_partition_or_within_facet_and_across_facets(self) -> None:
        facets = [
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
                    {"label": "OCS", "tags": ["ocs-rbd"]},
                    {"label": "io2", "tags": ["io2-csi"]},
                ],
            ),
        ]
        # Same facet -> single OR group
        assert partition_labels_by_facets(["aws", "bare-metal"], facets) == [
            ["aws", "bare-metal"]
        ]
        # Across facets -> separate AND groups
        assert partition_labels_by_facets(["aws", "io2-csi"], facets) == [
            ["aws"],
            ["io2-csi"],
        ]
        # Combined facets + non-facet tag
        assert partition_labels_by_facets(
            ["aws", "bare-metal", "ocs-rbd", "io2-csi", "scheduled"], facets
        ) == [["aws", "bare-metal"], ["ocs-rbd", "io2-csi"], ["scheduled"]]
        # No facets -> each label is its own AND group
        assert partition_labels_by_facets(["nightly", "smoke"], []) == [
            ["nightly"],
            ["smoke"],
        ]

    def test_diff_tag_facets_create_update_reorder_delete(self) -> None:
        f_plat = TagFacet(
            id="platform", name="Platform", options=[{"label": "AWS", "tags": ["aws"]}]
        )
        f_stor = TagFacet(
            id="storage",
            name="Storage",
            options=[{"label": "io2", "tags": ["io2-csi"]}],
        )
        assert diff_tag_facets([], [f_plat]) == [
            {
                "op": "create",
                "facet_id": "platform",
                "changed_fields": ["name", "options"],
            }
        ]
        f_plat_renamed = TagFacet(
            id="platform",
            name="Cloud Platform",
            options=[{"label": "AWS", "tags": ["aws"]}],
        )
        assert diff_tag_facets([f_plat, f_stor], [f_stor, f_plat_renamed]) == [
            {"op": "update", "facet_id": "storage", "changed_fields": ["order"]},
            {
                "op": "update",
                "facet_id": "platform",
                "changed_fields": ["name", "order"],
            },
        ]
        # Deleting the first facet does not mark the remaining facet as reordered
        assert diff_tag_facets([f_plat, f_stor], [f_stor]) == [
            {"op": "delete", "facet_id": "platform", "changed_fields": []}
        ]


# ---------------------------------------------------------------------------
# API + Storage integration: validation, counts, missing tags, Dashboard & Reports, Audit
# ---------------------------------------------------------------------------


class TestTagFacetsAPI:
    def test_put_rejects_unknown_tags_with_422(self, facet_client: TestClient) -> None:
        asyncio.run(_seed_run("r1", "job-aws", labels=["aws"]))
        payload = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Ghost", "tags": ["ghost-tag", "another-missing"]},
                ],
            }
        ]
        resp = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": json.dumps(payload)}},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "ghost-tag" in detail
        assert "another-missing" in detail

    def test_get_filter_facets_counts_multi_tag_dedup_and_missing_tags(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(_seed_run("r1", "job-aws-ocs", labels=["aws", "ocs-rbd"]))
        asyncio.run(_seed_run("r2", "job-aws-io2", labels=["aws", "io2-csi"]))
        asyncio.run(_seed_run("r3", "job-bm-io2", labels=["bare-metal", "io2-csi"]))
        # Job with both aws and gcp -> multi-tag option should count r4 ONCE, not twice
        asyncio.run(_seed_run("r4", "job-multi", labels=["aws", "gcp", "old-tag"]))

        facets_cfg = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Cloud", "tags": ["aws", "gcp"]},
                    {"label": "Bare metal", "tags": ["bare-metal"]},
                    {"label": "Legacy", "tags": ["old-tag"]},
                ],
            }
        ]
        save = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": facets_cfg}},
        )
        assert save.status_code == 200

        # Non-admin viewer can read GET /api/filters/facets
        reg = facet_client.post(
            "/api/auth/register",
            json={"username": "viewer1"},
        )
        assert reg.status_code in (200, 201)
        viewer_key = reg.json()["api_key"]
        viewer_resp = facet_client.get(
            "/api/filters/facets",
            headers={"Authorization": f"Bearer {viewer_key}"},
        )
        assert viewer_resp.status_code == 200
        facets = viewer_resp.json()["facets"]
        assert len(facets) == 1
        by_label = {o["label"]: o for o in facets[0]["options"]}
        assert by_label["AWS"]["count"] == 3
        assert by_label["Cloud"]["count"] == 3  # r1, r2, r4 (deduped!)
        assert by_label["Bare metal"]["count"] == 1
        assert by_label["Legacy"] == {
            "label": "Legacy",
            "tags": ["old-tag"],
            "count": 1,
            "missing_tags": [],
        }

        # Now drop "old-tag" from job metadata -> count drops to 0 and missing_tags flags it,
        # while the stored setting is NOT silently rewritten.
        asyncio.run(storage.set_job_metadata("job-multi", labels=["aws", "gcp"]))
        after = facet_client.get("/api/filters/facets").json()["facets"]
        legacy = {o["label"]: o for o in after[0]["options"]}["Legacy"]
        assert legacy["count"] == 0
        assert legacy["missing_tags"] == ["old-tag"]

    def test_or_within_facet_and_across_facets_on_dashboard_and_reports(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(
            _seed_run(
                "r-aws-ocs", "job-aws-ocs", labels=["aws", "ocs-rbd", "scheduled"]
            )
        )
        asyncio.run(_seed_run("r-aws-io2", "job-aws-io2", labels=["aws", "io2-csi"]))
        asyncio.run(
            _seed_run("r-bm-ocs", "job-bm-ocs", labels=["bare-metal", "ocs-rbd"])
        )
        asyncio.run(
            _seed_run("r-vsphere-io2", "job-vs-io2", labels=["vsphere", "io2-csi"])
        )

        facets_cfg = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Bare metal", "tags": ["bare-metal"]},
                    {"label": "vSphere", "tags": ["vsphere"]},
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
        resp = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": json.dumps(facets_cfg)}},
        )
        assert resp.status_code == 200

        # 1) OR within one facet (Platform = AWS OR Bare metal) -> 3 runs
        dash_or = facet_client.get(
            "/api/dashboard/filtered",
            params=[("label", "aws"), ("label", "bare-metal")],
        )
        assert dash_or.status_code == 200
        assert {j["job_id"] for j in dash_or.json()["jobs"]} == {
            "r-aws-ocs",
            "r-aws-io2",
            "r-bm-ocs",
        }

        rep_or = facet_client.get(
            "/api/reports/totals",
            params={"tags": "aws,bare-metal"},
        )
        assert rep_or.status_code == 200
        assert rep_or.json()["total_jobs"] == 3

        # 2) AND across two facets (Platform = AWS AND Storage = io2) -> 1 run
        dash_and = facet_client.get(
            "/api/dashboard/filtered",
            params=[("label", "aws"), ("label", "io2-csi")],
        )
        assert dash_and.status_code == 200
        assert [j["job_id"] for j in dash_and.json()["jobs"]] == ["r-aws-io2"]

        rep_and = facet_client.get(
            "/api/reports/totals",
            params={"tags": "aws,io2-csi"},
        )
        assert rep_and.status_code == 200
        assert rep_and.json()["total_jobs"] == 1
        assert rep_and.json()["jobs"][0]["job_id"] == "r-aws-io2"

        # 3) Combined (AWS OR Bare metal) AND (OCS RBD) -> 2 runs
        dash_combo = facet_client.get(
            "/api/dashboard/filtered",
            params=[("label", "aws"), ("label", "bare-metal"), ("label", "ocs-rbd")],
        )
        assert {j["job_id"] for j in dash_combo.json()["jobs"]} == {
            "r-aws-ocs",
            "r-bm-ocs",
        }
        rep_combo = facet_client.get(
            "/api/reports/totals",
            params={"tags": "aws,bare-metal,ocs-rbd"},
        )
        assert rep_combo.json()["total_jobs"] == 2

        # 4) Facet + non-facet plain tag: (AWS OR Bare metal) AND scheduled -> 1 run
        dash_plain = facet_client.get(
            "/api/dashboard/filtered",
            params=[("label", "aws"), ("label", "bare-metal"), ("label", "scheduled")],
        )
        assert [j["job_id"] for j in dash_plain.json()["jobs"]] == ["r-aws-ocs"]
        rep_plain = facet_client.get(
            "/api/reports/totals",
            params={"tags": "aws,bare-metal,scheduled"},
        )
        assert rep_plain.json()["total_jobs"] == 1

        # Classification overrides and issues-created share _build_tags_filter
        for endpoint in (
            "/api/reports/classification-overrides",
            "/api/reports/issues-created",
        ):
            r_ok = facet_client.get(endpoint, params={"tags": "aws,io2-csi"})
            assert r_ok.status_code == 200

    def test_audit_rows_on_create_update_delete_and_reset(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(_seed_run("r1", "job-1", labels=["aws", "bare-metal", "io2-csi"]))

        # Create two facets
        initial = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [{"label": "AWS", "tags": ["aws"]}],
            },
            {
                "id": "storage",
                "name": "Storage",
                "options": [{"label": "io2", "tags": ["io2-csi"]}],
            },
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(initial)}},
            ).status_code
            == 200
        )

        # Update platform options only (storage untouched)
        updated = [
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
                "options": [{"label": "io2", "tags": ["io2-csi"]}],
            },
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(updated)}},
            ).status_code
            == 200
        )

        # Delete storage facet via PUT
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps([updated[0]])}},
            ).status_code
            == 200
        )

        # Reset tag_facets -> deletes remaining platform facet
        assert facet_client.delete("/api/admin/settings/tag_facets").status_code == 200

        events = facet_client.get("/api/admin/audit", params={"limit": 50}).json()[
            "events"
        ]
        facet_events = [e for e in events if e["action"].startswith("tag_facet.")]
        # Events are returned newest-first; reverse for chronological assertions
        facet_events.reverse()
        summary = [(e["action"], e["target_id"], e["details"]) for e in facet_events]
        assert summary == [
            (
                "tag_facet.create",
                "platform",
                {"facet_id": "platform", "changed_fields": ["name", "options"]},
            ),
            (
                "tag_facet.create",
                "storage",
                {"facet_id": "storage", "changed_fields": ["name", "options"]},
            ),
            (
                "tag_facet.update",
                "platform",
                {"facet_id": "platform", "changed_fields": ["options"]},
            ),
            (
                "tag_facet.delete",
                "storage",
                {"facet_id": "storage", "changed_fields": []},
            ),
            (
                "tag_facet.delete",
                "platform",
                {"facet_id": "platform", "changed_fields": []},
            ),
        ]
        # Never store the facet options/tags blob in audit details
        for event in facet_events:
            assert "aws" not in json.dumps(event["details"])

    def test_reports_preserves_legacy_or_for_ungrouped_tags_when_no_facets_configured(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(_seed_run("r-aws", "job-aws", labels=["aws"]))
        asyncio.run(_seed_run("r-vs", "job-vs", labels=["vsphere"]))
        # Default tag_facets=[] -> legacy Reports multi-tag bookmark matches EITHER tag (OR)
        rep = facet_client.get("/api/reports/totals", params={"tags": "aws,vsphere"})
        assert rep.status_code == 200
        assert rep.json()["total_jobs"] == 2

    def test_vanished_tag_does_not_block_editing_other_facets_or_import(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(_seed_run("r1", "job-1", labels=["aws", "old-tag", "io2-csi"]))
        initial = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Legacy", "tags": ["old-tag"]},
                ],
            },
            {
                "id": "storage",
                "name": "Storage",
                "options": [{"label": "io2", "tags": ["io2-csi"]}],
            },
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(initial)}},
            ).status_code
            == 200
        )
        # Now old-tag disappears from job metadata
        asyncio.run(storage.set_job_metadata("job-1", labels=["aws", "io2-csi"]))

        # Renaming storage (without touching Legacy) still succeeds
        renamed = [
            initial[0],
            {**initial[1], "name": "Storage class"},
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(renamed)}},
            ).status_code
            == 200
        )

        # Renaming the option or facet ID that already holds the vanished tag succeeds
        renamed_opt = [
            {
                "id": "platform-v2",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Legacy (deprecated)", "tags": ["old-tag"]},
                ],
            },
            {**initial[1], "name": "Storage class"},
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(renamed_opt)}},
            ).status_code
            == 200
        )

        # Duplicating the vanished tag into an additional option is rejected with 422
        duplicated_vanished = [
            {
                "id": "platform-v2",
                "name": "Platform",
                "options": [
                    {"label": "AWS", "tags": ["aws"]},
                    {"label": "Legacy (deprecated)", "tags": ["old-tag"]},
                    {"label": "Legacy copy", "tags": ["old-tag"]},
                ],
            },
            {**initial[1], "name": "Storage class"},
        ]
        reused_resp = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": json.dumps(duplicated_vanished)}},
        )
        assert reused_resp.status_code == 422
        assert "old-tag" in reused_resp.json()["detail"]

        # Adding a brand-new unknown tag is still rejected with 422
        with_brand_new_unknown = [
            initial[0],
            {
                "id": "storage",
                "name": "Storage class",
                "options": [{"label": "Bad", "tags": ["brand-new-unknown"]}],
            },
        ]
        bad = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": json.dumps(with_brand_new_unknown)}},
        )
        assert bad.status_code == 422
        assert "brand-new-unknown" in bad.json()["detail"]

        # Backup import onto a fresh DB allows missing tags while validating schema
        imp = facet_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "tag_facets": json.dumps(
                        [
                            {
                                "id": "imported",
                                "name": "Imported",
                                "options": [
                                    {"label": "Future", "tags": ["not-seen-yet"]}
                                ],
                            }
                        ]
                    )
                },
            },
        )
        assert imp.status_code == 200

    def test_persist_admin_setting_validates_env_tag_facets(
        self, facet_client: TestClient
    ) -> None:
        # Malformed env TAG_FACETS cannot be persisted to DB
        with patch.dict(os.environ, {"TAG_FACETS": "{invalid-json"}):
            resp = facet_client.post("/api/admin/settings/tag_facets/persist")
        assert resp.status_code == 422

        # Valid env TAG_FACETS referencing live tag persists cleanly
        asyncio.run(_seed_run("r1", "job-1", labels=["aws"]))
        valid_env = json.dumps(
            [
                {
                    "id": "platform",
                    "name": "Platform",
                    "options": [{"label": "AWS", "tags": ["aws"]}],
                }
            ]
        )
        with patch.dict(os.environ, {"TAG_FACETS": valid_env}):
            resp = facet_client.post("/api/admin/settings/tag_facets/persist")
        assert resp.status_code == 200

        # Env TAG_FACETS with no DB row and an unknown tag cannot bypass 422 on persist or PUT
        assert facet_client.delete("/api/admin/settings/tag_facets").status_code == 200
        asyncio.run(storage.set_job_metadata("job-1", labels=["other"]))
        with patch.dict(os.environ, {"TAG_FACETS": valid_env}):
            clear_settings_cache()
            resp_env_unknown = facet_client.post(
                "/api/admin/settings/tag_facets/persist"
            )
            assert resp_env_unknown.status_code == 422
            assert "aws" in resp_env_unknown.json()["detail"]
            resp_put_env_unknown = facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": valid_env}},
            )
            assert resp_put_env_unknown.status_code == 422

    def test_reordering_overlapping_vanished_options_succeeds(
        self, facet_client: TestClient
    ) -> None:
        asyncio.run(_seed_run("r1", "job-1", labels=["d1", "d2"]))
        initial = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "Both", "tags": ["d1", "d2"]},
                    {"label": "First", "tags": ["d1"]},
                ],
            }
        ]
        assert (
            facet_client.put(
                "/api/admin/settings",
                json={"settings": {"tag_facets": json.dumps(initial)}},
            ).status_code
            == 200
        )
        # Both tags vanish -> reordering First before Both must still succeed via bipartite matching
        asyncio.run(storage.set_job_metadata("job-1", labels=["other"]))
        reordered = [
            {
                "id": "platform",
                "name": "Platform",
                "options": [
                    {"label": "First", "tags": ["d1"]},
                    {"label": "Both", "tags": ["d1", "d2"]},
                ],
            }
        ]
        resp = facet_client.put(
            "/api/admin/settings",
            json={"settings": {"tag_facets": json.dumps(reordered)}},
        )
        assert resp.status_code == 200
