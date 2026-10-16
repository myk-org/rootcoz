"""Tests for pi-sidecar session transcript access (#328/#329)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from rootcoz import transcripts as transcripts_module
from rootcoz.transcripts import (
    valid_session_id,
    write_transcript_env_bridge,
)


@pytest.fixture
def transcripts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the transcripts module at a temp directory."""
    directory = tmp_path / "transcripts"
    directory.mkdir()
    monkeypatch.setattr(transcripts_module, "TRANSCRIPTS_DIR", directory)
    return directory


def _write_transcript(directory: Path, session_id: str, lines: list[str]) -> Path:
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


class TestSessionIdValidation:
    """The UUID gate is the path-traversal defense — it must be airtight."""

    def test_valid_uuid_accepted(self):
        assert valid_session_id(str(uuid.uuid4())) is True

    def test_uppercase_uuid_rejected(self):
        # Sidecar ids are lowercase hex (randomUUID output)
        assert valid_session_id(str(uuid.uuid4()).upper()) is False

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "..",
            "../../etc/passwd",
            "foo/bar",
            "foo\\bar",
            "..jsonl",
            "not-a-uuid",
            "00000000-0000-0000-0000-00000000000",
            "00000000-0000-0000-0000-0000000000000.jsonl",
            "00000000-0000-0000-0000-000000000000\n",
            "00000000-0000-0000-0000-000000000000 ",
        ],
    )
    def test_malformed_rejected(self, bad: str):
        assert valid_session_id(bad) is False

    def test_transcript_path_rejects_traversal(self):
        from rootcoz.transcripts import transcript_path

        assert transcript_path("../../etc/passwd") is None
        assert transcript_path("..") is None
        assert transcript_path("") is None


class TestListTranscripts:
    def test_lists_valid_files_with_turns(self, transcripts_dir: Path):
        session = "00000000-0000-0000-0000-000000000001"
        _write_transcript(transcripts_dir, session, ["a", "b", "c"])
        other = "00000000-0000-0000-0000-000000000002"
        _write_transcript(transcripts_dir, other, ["x"])

        infos = transcripts_module.list_transcripts()
        by_id = {info.session_id: info for info in infos}
        assert by_id[session].turns == 3
        assert by_id[other].turns == 1
        assert by_id[session].size_bytes > 0

    def test_skips_foreign_and_malformed_files(self, transcripts_dir: Path):
        _write_transcript(
            transcripts_dir, "00000000-0000-0000-0000-000000000001", ["a"]
        )
        (transcripts_dir / "not-a-uuid.jsonl").write_text("x\n")
        (transcripts_dir / "README.md").write_text("x")
        (transcripts_dir / "sub").mkdir()

        infos = transcripts_module.list_transcripts()
        assert [info.session_id for info in infos] == [
            "00000000-0000-0000-0000-000000000001"
        ]

    def test_missing_dir_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            transcripts_module, "TRANSCRIPTS_DIR", tmp_path / "does-not-exist"
        )
        assert transcripts_module.list_transcripts() == []


class TestEnvBridge:
    def test_writes_enabled_state(self, transcripts_dir: Path):
        write_transcript_env_bridge(True)
        bridge = transcripts_dir.parent / "env"
        content = bridge.read_text()
        assert "RECORD_TRANSCRIPTS=true" in content
        assert f"PI_SIDECAR_TRANSCRIPTS_DIR={transcripts_dir}" in content

    def test_writes_disabled_state(self, transcripts_dir: Path):
        write_transcript_env_bridge(False)
        bridge = transcripts_dir.parent / "env"
        assert "RECORD_TRANSCRIPTS=false" in bridge.read_text()

    def test_unwritable_path_is_best_effort(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            transcripts_module, "TRANSCRIPTS_DIR", Path("/proc/definitely/not/writable")
        )
        # Must not raise
        write_transcript_env_bridge(True)


class TestTranscriptEndpoints:
    """Admin-only API: gating, listing with context join, download, traversal."""

    @pytest.fixture
    def client(self, temp_db_path: Path) -> Generator:
        from tests.conftest import make_app_client

        yield from make_app_client(temp_db_path)

    @pytest.fixture
    def admin_headers(self, client) -> dict[str, str]:
        from tests.conftest import admin_login

        cookies = admin_login(client)
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    @pytest.fixture
    def viewer_headers(self, client) -> dict[str, str]:
        client.post(
            "/api/auth/register",
            json={"username": "viewer-user"},
        )
        resp = client.post(
            "/api/auth/login",
            json={"username": "viewer-user", "api_key": ""},
        )
        assert resp.status_code in (200, 400)
        # Register returns an API key; use it directly.
        reg = client.post("/api/auth/register", json={"username": "viewer2"})
        if reg.status_code == 200:
            key = reg.json().get("api_key", "")
            return {"Authorization": f"Bearer {key}"}
        return {}

    def test_non_admin_gets_403(self, client, viewer_headers):
        resp = client.get("/api/admin/transcripts", headers=viewer_headers)
        assert resp.status_code == 403

    def test_unauthenticated_gets_401(self, client):
        resp = client.get("/api/admin/transcripts")
        assert resp.status_code in (401, 403)

    def test_list_with_context_join(
        self, client, admin_headers, transcripts_dir, monkeypatch
    ):
        from rootcoz import storage

        session = "00000000-0000-0000-0000-00000000000a"
        _write_transcript(transcripts_dir, session, ["turn1", "turn2"])
        chat_session = "00000000-0000-0000-0000-00000000000b"
        _write_transcript(transcripts_dir, chat_session, ["msg"])

        # NOTE: the endpoint reads TRANSCRIPTS_DIR through the module
        monkeypatch.setattr("rootcoz.main.sidecar_transcripts", transcripts_module)

        async def seed():
            await storage.record_token_usage(
                job_id="job-1",
                ai_provider="claude",
                ai_model="opus-4-6",
                call_type="primary",
                input_tokens=10,
                session_id=session,
            )
            await storage.add_chat_message(
                "job-1", "user", "hello", "someuser", session_id=chat_session
            )

        import asyncio

        asyncio.run(seed())

        resp = client.get("/api/admin/transcripts", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        by_id = {item["session_id"]: item for item in data["items"]}
        assert by_id[session]["turns"] == 2
        assert by_id[session]["context"]["job_id"] == "job-1"
        assert by_id[session]["context"]["call_type"] == "primary"
        assert by_id[chat_session]["context"]["chat_username"] == "someuser"

    def test_job_filter(self, client, admin_headers, transcripts_dir):
        import asyncio

        from rootcoz import storage

        session_a = "00000000-0000-0000-0000-00000000000a"
        session_b = "00000000-0000-0000-0000-00000000000b"
        _write_transcript(transcripts_dir, session_a, ["a"])
        _write_transcript(transcripts_dir, session_b, ["b"])

        async def seed():
            await storage.record_token_usage(
                job_id="job-1",
                ai_provider="claude",
                ai_model="m",
                call_type="primary",
                session_id=session_a,
            )
            await storage.record_token_usage(
                job_id="job-2",
                ai_provider="claude",
                ai_model="m",
                call_type="primary",
                session_id=session_b,
            )

        asyncio.run(seed())

        resp = client.get(
            "/api/admin/transcripts", params={"job_id": "job-1"}, headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [item["session_id"] for item in data["items"]] == [session_a]

    def test_download_streams_full_content(
        self, client, admin_headers, transcripts_dir
    ):
        session = "00000000-0000-0000-0000-00000000000c"
        content = '{"turn":1}\n{"turn":2}\n'
        _write_transcript(
            transcripts_dir,
            session,
            [content.strip().splitlines()[0], content.strip().splitlines()[1]],
        )

        resp = client.get(f"/api/admin/transcripts/{session}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.text == content
        assert resp.headers["content-disposition"] == (
            f'attachment; filename="{session}.jsonl"'
        )
        assert resp.headers["cache-control"] == "no-store"

    def test_download_404_for_missing(self, client, admin_headers, transcripts_dir):
        session = "00000000-0000-0000-0000-00000000000d"
        resp = client.get(f"/api/admin/transcripts/{session}", headers=admin_headers)
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "bad",
        ["..", "../../etc/passwd", "%2e%2e%2f", "not-a-uuid", ""],
    )
    def test_download_rejects_malformed_ids(self, client, admin_headers, bad):
        # Path-normalized ids ("..", "../..") may be rewritten by the
        # HTTP layer before reaching the handler — the security property
        # is that NO malformed id ever yields 200 or touches the filesystem.
        resp = client.get(f"/api/admin/transcripts/{bad}", headers=admin_headers)
        assert resp.status_code in (400, 404), resp.status_code

    def test_download_rejects_non_uuid_at_handler(self, client, admin_headers):
        # A plain non-UUID reaches the handler and must get a clean 400 from
        # the validated UUID gate (no filesystem access). Traversal-shaped
        # ids are additionally path-normalized by the HTTP layer (404) and
        # rejected by the same gate at unit level (transcript_path tests).
        resp = client.get("/api/admin/transcripts/not-a-uuid", headers=admin_headers)
        assert resp.status_code == 400
        assert "Invalid session id" in resp.json()["detail"]

    def test_setting_registered_restart_required(self):
        from rootcoz.main import _RESTART_REQUIRED_SETTINGS, _SETTINGS_CATEGORIES

        assert "record_transcripts" in _RESTART_REQUIRED_SETTINGS
        assert "record_transcripts" in _SETTINGS_CATEGORIES["AI"]

    def test_setting_defaults_off(self, mock_env_vars):
        from rootcoz.config import Settings

        assert Settings().record_transcripts is False


class TestSessionIdRecording:
    """#328: usage rows carry the sidecar session id for transcript joins."""

    @pytest.mark.asyncio
    async def test_record_ai_usage_stores_session_id(self):
        from unittest.mock import AsyncMock, patch

        from pi_sidecar_client import AIResult, AITokenUsage

        from rootcoz.token_tracking import record_ai_usage

        result = AIResult(
            success=True,
            text="out",
            usage=AITokenUsage(input_tokens=1, output_tokens=1),
        )
        result.transcript_session_id = "00000000-0000-0000-0000-00000000000e"

        with patch(
            "rootcoz.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(job_id="job-1", result=result, call_type="primary")
            assert (
                mock_record.call_args.kwargs["session_id"]
                == "00000000-0000-0000-0000-00000000000e"
            )

    @pytest.mark.asyncio
    async def test_record_ai_usage_falls_back_to_live_session_id(self):
        from unittest.mock import AsyncMock, patch

        from pi_sidecar_client import AIResult, AITokenUsage

        from rootcoz.token_tracking import record_ai_usage

        result = AIResult(
            success=True,
            text="out",
            usage=AITokenUsage(input_tokens=1, output_tokens=1),
        )
        result.session_id = "00000000-0000-0000-0000-00000000000f"

        with patch(
            "rootcoz.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(job_id="job-1", result=result, call_type="chat")
            assert (
                mock_record.call_args.kwargs["session_id"]
                == "00000000-0000-0000-0000-00000000000f"
            )

    @pytest.mark.asyncio
    async def test_storage_round_trip(self, initialized_db):
        from rootcoz import storage

        session = "00000000-0000-0000-0000-000000000010"
        await storage.record_token_usage(
            job_id="job-rt",
            ai_provider="claude",
            ai_model="m",
            call_type="primary",
            session_id=session,
        )
        context = await storage.get_usage_context_for_sessions([session])
        assert context[session]["job_id"] == "job-rt"

        rows = await storage.get_token_usage_for_job("job-rt")
        assert rows[0]["session_id"] == session
