"""The test-name filter on the job list (#546 B): modes, literal text, scope."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Admin key + a temp DB, with the settings cache cleared around the test."""
    import os

    from rootcoz.config import clear_settings_cache

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ADMIN_KEY": _ADMIN_KEY,
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "SECURE_COOKIES": "false",
        "DB_PATH": str(temp_db_path),
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        clear_settings_cache()
        try:
            yield env
        finally:
            clear_settings_cache()


@pytest.fixture
def api_client(mock_settings, temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from rootcoz.main import app

        with TestClient(
            app, headers={"Authorization": f"Bearer {_ADMIN_KEY}"}
        ) as client:
            yield client


def _seed(temp_db_path: Path, jobs: list[tuple[str, list[dict]]]) -> None:
    """Insert jobs plus their test entries, in one event loop."""

    async def _run():
        with patch.object(storage, "DB_PATH", temp_db_path):
            for job_id, entries in jobs:
                await storage.save_result(
                    job_id,
                    f"https://jenkins.example.com/job/{job_id}/1",
                    "completed",
                    {"job_name": job_id, "build_number": 1, "failures": []},
                )
                await storage.save_test_entries(job_id, entries)

    asyncio.run(_run())


def _jobs(client, **params) -> list[str]:
    resp = client.get("/api/dashboard/filtered", params=params)
    assert resp.status_code == 200, resp.text
    return [job["job_name"] for job in resp.json()["jobs"]]


@pytest.fixture
def four_modes(api_client, temp_db_path):
    """One job per position of the same term, so each mode differs observably."""
    _seed(
        temp_db_path,
        [
            ("mid-job", [{"test_name": "prefix_needle_suffix", "status": "failed"}]),
            ("exact-job", [{"test_name": "needle", "status": "failed"}]),
            ("starts-job", [{"test_name": "needle_tail", "status": "failed"}]),
            ("ends-job", [{"test_name": "head_needle", "status": "failed"}]),
            ("other-job", [{"test_name": "checkout", "status": "failed"}]),
        ],
    )
    return api_client


class TestTestNameModes:
    def test_contains_matches_every_position(self, four_modes) -> None:
        assert set(_jobs(four_modes, test_name="needle")) == {
            "mid-job",
            "exact-job",
            "starts-job",
            "ends-job",
        }

    def test_exact_does_not_match_a_superstring(self, four_modes) -> None:
        assert _jobs(four_modes, test_name="needle", test_name_mode="exact") == [
            "exact-job"
        ]

    def test_starts_with_excludes_the_leading_position(self, four_modes) -> None:
        assert set(
            _jobs(four_modes, test_name="needle", test_name_mode="starts_with")
        ) == {"exact-job", "starts-job"}

    def test_ends_with_excludes_the_trailing_position(self, four_modes) -> None:
        assert set(
            _jobs(four_modes, test_name="needle", test_name_mode="ends_with")
        ) == {"exact-job", "ends-job"}

    def test_case_insensitive(self, four_modes) -> None:
        assert _jobs(four_modes, test_name="NEEDLE", test_name_mode="exact") == [
            "exact-job"
        ]

    def test_unknown_mode_is_400(self, four_modes) -> None:
        resp = four_modes.get(
            "/api/dashboard/filtered",
            params={"test_name": "needle", "test_name_mode": "wildcard"},
        )
        assert resp.status_code == 400
        assert "test_name_mode" in resp.text

    def test_over_long_name_is_422(self, four_modes) -> None:
        resp = four_modes.get(
            "/api/dashboard/filtered", params={"test_name": "x" * 1001}
        )
        assert resp.status_code == 422


class TestLiteralText:
    @pytest.fixture
    def literal(self, api_client, temp_db_path):
        _seed(
            temp_db_path,
            [
                ("percent-job", [{"test_name": "100% done", "status": "failed"}]),
                ("underscore-job", [{"test_name": "a_b", "status": "failed"}]),
                ("plain-job", [{"test_name": "100x done", "status": "failed"}]),
                ("star-job", [{"test_name": "star*cases", "status": "failed"}]),
            ],
        )
        return api_client

    def test_percent_is_not_a_wildcard(self, literal) -> None:
        assert _jobs(literal, test_name="100%", test_name_mode="contains") == [
            "percent-job"
        ]

    def test_underscore_is_not_a_wildcard(self, literal) -> None:
        assert _jobs(literal, test_name="a_b", test_name_mode="exact") == [
            "underscore-job"
        ]

    def test_asterisk_is_literal(self, literal) -> None:
        assert _jobs(literal, test_name="star*", test_name_mode="starts_with") == [
            "star-job"
        ]
        # 'star' alone still finds it — '*' is part of the stored name, not a
        # pattern the user has to type.
        assert _jobs(literal, test_name="star", test_name_mode="starts_with") == [
            "star-job"
        ]


class TestScopeAndCombinations:
    def test_passed_entry_cannot_match(self, api_client, temp_db_path) -> None:
        """The EXISTS is scoped to status='failed' by construction."""
        _seed(
            temp_db_path,
            [
                ("only-passed", [{"test_name": "green_test", "status": "passed"}]),
                ("failed-other", [{"test_name": "red_test", "status": "failed"}]),
            ],
        )
        assert _jobs(api_client, test_name="green_test") == []

    def test_empty_test_name_returns_everything(self, api_client, temp_db_path) -> None:
        _seed(
            temp_db_path,
            [("a-job", [{"test_name": "x", "status": "failed"}]), ("b-job", [])],
        )
        unfiltered = api_client.get("/api/dashboard/filtered").json()["total"]
        empty = api_client.get(
            "/api/dashboard/filtered", params={"test_name": ""}
        ).json()["total"]
        assert empty == unfiltered == 2

    def test_ands_with_another_filter(self, api_client, temp_db_path) -> None:
        _seed(
            temp_db_path,
            [
                ("match-job", [{"test_name": "login", "status": "failed"}]),
                ("nometa-job", [{"test_name": "login", "status": "failed"}]),
            ],
        )
        api_client.put("/api/jobs/match-job/metadata", json={"team": "alpha"})
        resp = api_client.get(
            "/api/dashboard/filtered", params={"team": "alpha", "test_name": "login"}
        )
        assert resp.status_code == 200, resp.text
        assert [job["job_name"] for job in resp.json()["jobs"]] == ["match-job"]
