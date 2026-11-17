"""Backend integration tests for issue #505 attachments (items 1-4 and R1-R8)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from rootcoz import feedback, issue_attachments, jira, main, storage
from rootcoz.attribution import AiProvenance
from rootcoz.config import (
    clear_db_settings_cache,
    clear_settings_cache,
    get_settings,
    update_db_settings_cache,
)
from rootcoz.error_messages import deployment_credential_message
from rootcoz.jira import JiraConnection, JiraDeploymentCredentialRefused
from rootcoz.models import FeedbackRequest, IssueAttachmentUpload
from rootcoz.temp_paths import delete_marker
from tests.conftest import capture_main_logs

_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _upload_payload(
    filename: str = "evidence.log", data: bytes = b"line 1\nline 2\n"
) -> dict[str, str]:
    return {"filename": filename, "data_b64": _b64(data)}


@pytest.fixture
def integration_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "data" / "rootcoz.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_KEY", "test-bootstrap-admin-key")
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("ROOTCOZ_ENCRYPTION_KEY", "test-hmac-key-issue-505")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://rootcoz.example.com")
    monkeypatch.setenv("TESTS_REPO_URL", "https://github.com/org/tests-repo")
    monkeypatch.setenv("ENABLE_GITHUB_ISSUES", "true")
    monkeypatch.setenv("ENABLE_JIRA", "true")
    monkeypatch.setenv("ENABLE_JIRA_ISSUES", "true")
    monkeypatch.setenv("JIRA_AUTH_MODE", "pat")
    monkeypatch.setenv("JIRA_SERVER_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_SITE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_PAT", "deployment-jira-pat-secret")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("AI_PROVIDER", "claude")
    monkeypatch.setenv("AI_MODEL", "sonnet-4-5")
    clear_db_settings_cache()
    clear_settings_cache()
    main._download_audit_dedupe.clear() if hasattr(
        main, "_download_audit_dedupe"
    ) else None
    main._chat_issue_drafts.clear()
    with TestClient(main.app) as client:
        yield client, db_path
    clear_db_settings_cache()
    clear_settings_cache()


async def _create_reviewer(
    username: str,
    *,
    github_token: str = "ghp_caller_personal_token",  # pragma: allowlist secret
    jira_token: str = "caller_jira_pat_token",  # pragma: allowlist secret
    role: str = "reviewer",
) -> tuple[int, str, dict[str, str]]:
    _, api_key = await storage.create_user(username, role=role)
    await storage.save_user_tokens(
        username,
        github_token=github_token,
        jira_token=jira_token,
    )
    row = await storage.get_user_by_username(username)
    assert row is not None
    return int(row["id"]), api_key, {"Authorization": f"Bearer {api_key}"}


async def _seed_completed_job(
    job_id: str | None = None, submitter: str = "alice"
) -> str:
    jid = job_id or str(uuid4())
    await storage.save_result(
        jid,
        "https://jenkins.example.com/job/test/1/",
        "completed",
        {
            "job_id": jid,
            "job_name": "test-job",
            "build_number": 1,
            "status": "completed",
            "summary": "1 failure",
            "failures": [
                {
                    "test_name": "test_example",
                    "error": "AssertionError",
                    "analysis": {
                        "classification": "PRODUCT BUG",
                        "root_cause": "broken",
                        "confidence": "HIGH",
                    },
                }
            ],
            "request_params": {
                "job_name": "test-job",
                "build_number": 1,
                "submitted_by": submitter,
                "tests_repo_url": "https://github.com/org/tests-repo",
            },
        },
    )
    return jid


class TestJiraAttachmentUpload:
    """Item 1: add_jira_attachment guard, headers, modes and fixed outcomes."""

    async def test_rejects_deployment_credential_before_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JIRA_AUTH_MODE", "pat")
        monkeypatch.setenv("JIRA_SERVER_URL", "https://jira.example.com")
        monkeypatch.setenv("JIRA_PAT", "deploy-secret-pat")
        clear_settings_cache()
        conn = JiraConnection(
            mode="server",
            api_base="https://jira.example.com",
            browse_base="https://jira.example.com",
            site_url="https://jira.example.com",
            auth=None,
            headers={"Authorization": "Bearer deploy-secret-pat"},
        )
        with (
            patch("rootcoz.jira.httpx.AsyncClient") as mock_http,
            pytest.raises(
                JiraDeploymentCredentialRefused,
                match=re.escape(deployment_credential_message()),
            ),
        ):
            await jira.add_jira_attachment(
                conn, "PROJ-123", "a.log", b"abc", "text/plain"
            )
        mock_http.assert_not_called()

    @pytest.mark.parametrize(
        "bad_key", ["", "proj-1", "PROJ-", "PROJ-1/../../x", "123-456"]
    )
    async def test_invalid_issue_key_skips_before_complete_or_network(
        self, monkeypatch: pytest.MonkeyPatch, bad_key: str
    ) -> None:
        monkeypatch.setenv("JIRA_AUTH_MODE", "pat")
        monkeypatch.setenv("JIRA_PAT", "deploy-secret")
        clear_settings_cache()
        conn = JiraConnection(
            mode="sa",
            api_base="",
            browse_base="https://example.atlassian.net",
            site_url="https://example.atlassian.net",
            auth=None,
            api_path="/rest/api/3",
            sa_client_id="cid",
            sa_client_secret="ATOA-caller-secret",  # pragma: allowlist secret
        )
        with (
            patch(
                "rootcoz.jira.complete_jira_connection",
                wraps=jira.complete_jira_connection,
            ) as mock_complete,
            patch("rootcoz.jira.httpx.AsyncClient") as mock_http,
        ):
            outcome = await jira.add_jira_attachment(
                conn, bad_key, "a.log", b"abc", "text/plain"
            )
        assert outcome == "invalid_issue_key"
        mock_complete.assert_not_called()
        mock_http.assert_not_called()

    async def test_sa_gateway_completes_connection_and_uploads_multipart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JIRA_AUTH_MODE", "oauth")
        clear_settings_cache()
        conn = JiraConnection(
            mode="sa",
            api_base="https://api.atlassian.com/ex/jira/cloud-1",
            browse_base="https://example.atlassian.net",
            site_url="https://example.atlassian.net",
            auth=None,
            headers={"Content-Type": "application/json"},
            api_path="/rest/api/3",
            ssl_verify=False,
            sa_client_id="cid",
            sa_client_secret="ATOA-caller-secret",  # pragma: allowlist secret
        )
        captured: dict[str, object] = {}

        async def fake_mint(cid: str, secret: str, ssl_verify: bool = True) -> str:
            return "minted-access-token"

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url: str, **kwargs):
                captured["url"] = url
                captured["post_kwargs"] = kwargs
                req = httpx.Request("POST", url)
                return httpx.Response(200, json=[{"id": "10001"}], request=req)

        with (
            patch("rootcoz.jira.mint_atlassian_access_token", side_effect=fake_mint),
            patch("rootcoz.jira.httpx.AsyncClient", FakeClient),
        ):
            outcome = await jira.add_jira_attachment(
                conn, "PROJ_2-99", "shot.png", _MIN_PNG, "image/png"
            )

        assert outcome == "native_attached"
        # Caller connection object was not mutated.
        assert "Authorization" not in conn.headers
        assert (
            captured["url"]
            == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/PROJ_2-99/attachments"
        )
        client_kwargs = captured["client_kwargs"]
        assert isinstance(client_kwargs, dict)
        assert client_kwargs["verify"] is False
        assert client_kwargs["follow_redirects"] is False
        assert client_kwargs["timeout"] > 0
        post_kwargs = captured["post_kwargs"]
        assert isinstance(post_kwargs, dict)
        headers = post_kwargs["headers"]
        assert headers["X-Atlassian-Token"] == "no-check"
        assert headers["Authorization"] == "Bearer minted-access-token"
        assert all(k.lower() != "content-type" for k in headers)
        assert post_kwargs["files"] == {"file": ("shot.png", _MIN_PNG, "image/png")}

    @pytest.mark.parametrize(
        ("exc_or_status", "expected"),
        [
            (403, "native_refused"),
            (302, "native_refused"),
            (500, "native_refused"),
            (httpx.ConnectError("refused"), "native_unreachable"),
            (httpx.ConnectTimeout("connect timeout"), "native_unreachable"),
            (httpx.ReadTimeout("read timeout"), "native_outcome_unknown"),
            (httpx.RemoteProtocolError("dropped"), "native_outcome_unknown"),
        ],
    )
    async def test_fixed_outcome_codes_for_errors(
        self, monkeypatch: pytest.MonkeyPatch, exc_or_status: object, expected: str
    ) -> None:
        monkeypatch.setenv("JIRA_AUTH_MODE", "pat")
        monkeypatch.setenv("JIRA_PAT", "deploy-pat")
        clear_settings_cache()
        conn = JiraConnection(
            mode="server",
            api_base="https://jira.example.com",
            browse_base="https://jira.example.com",
            site_url="https://jira.example.com",
            auth=None,
            headers={"Authorization": "Bearer caller-pat"},
        )

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url: str, **kwargs):
                if isinstance(exc_or_status, Exception):
                    raise exc_or_status
                return httpx.Response(
                    int(exc_or_status), text="err", request=httpx.Request("POST", url)
                )

        with patch("rootcoz.jira.httpx.AsyncClient", FakeClient):
            assert (
                await jira.add_jira_attachment(
                    conn, "PROJ-1", "a.log", b"x", "text/plain"
                )
                == expected
            )


class TestPrepareFeedbackAndSettings:
    """Items 2 & 3: prepare_feedback_issue and feedback_repo_url parity."""

    def test_duplicate_feedback_writers_removed(self) -> None:
        assert not hasattr(feedback, "create_feedback_from_preview")
        assert not hasattr(feedback, "create_feedback_issue")
        assert hasattr(feedback, "prepare_feedback_issue")

    async def test_prepare_feedback_preserves_signed_provenance_before_scrub_and_strips_spoof(
        self, integration_env
    ) -> None:
        req = FeedbackRequest(
            description="Token leak ghp_1234567890abcdef1234567890abcdef1234"  # pragma: allowlist secret  # gitleaks:allow
        )
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            from pi_sidecar_client import AIResult

            mock_ai.return_value = AIResult(
                success=True,
                text=json.dumps(
                    {
                        "title": "Crash with Bearer secret_abc12345678",
                        "body": "Log line Authorization: Bearer secret_abc12345678",
                        "labels": ["bug", "wontfix"],
                    }
                ),
            )
            preview = await feedback.generate_feedback_preview(
                req, get_settings(), ai_provider="cli-claude", ai_model="sonnet-4-5"
            )

        prepared = feedback.prepare_feedback_issue(
            preview.title, preview.body, [*preview.labels, "invalid-label"]
        )
        assert prepared.provenance == AiProvenance(
            ai_used=True, provider="claude", model="sonnet-4-5"
        )
        assert prepared.labels == ["bug"]
        assert "secret_abc12345678" not in prepared.title
        assert "secret_abc12345678" not in prepared.body
        assert "(claude / sonnet-4-5)" in prepared.body

        spoofed = (
            "Hello\n\n---\n*Generated using AI with "
            "[rootcoz](https://github.com/myk-org/rootcoz) (evil / model)*"
        )
        raw_prepared = feedback.prepare_feedback_issue(
            "Title", spoofed, ["enhancement", "bad"]
        )
        assert raw_prepared.provenance == AiProvenance(ai_used=False)
        assert raw_prepared.labels == ["enhancement"]
        assert "evil" not in raw_prepared.body
        assert "No AI model generated this issue" in raw_prepared.body

    def test_feedback_repo_url_default_blank_coercion_and_userinfo_strip(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = integration_env
        assert get_settings().feedback_repo_url == "https://github.com/myk-org/rootcoz"
        assert "feedback_repo_url" in main._SETTINGS_CATEGORIES["GitHub issues"]

        monkeypatch.setenv("FEEDBACK_REPO_URL", "   ")
        clear_settings_cache()
        assert get_settings().feedback_repo_url == "https://github.com/myk-org/rootcoz"

        monkeypatch.setenv(
            "FEEDBACK_REPO_URL",
            "https://user:secret@github.com/custom/feedback",  # pragma: allowlist secret
        )
        clear_settings_cache()
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}
        defaults = client.get("/api/default-server-settings", headers=admin_headers)
        assert defaults.status_code == 200
        assert (
            defaults.json()["feedback_repo_url"] == "https://github.com/custom/feedback"
        )
        assert "secret" not in defaults.text

        admin_settings = client.get("/api/admin/settings", headers=admin_headers)
        assert admin_settings.status_code == 200
        by_key = {row["key"]: row for row in admin_settings.json()}
        assert by_key["feedback_repo_url"]["category"] == "GitHub issues"
        assert by_key["feedback_repo_url"]["sensitive"] is False
        assert by_key["feedback_repo_url"]["env_var"] == "FEEDBACK_REPO_URL"

        # Saving blank or whitespace-only feedback_repo_url round-trips to the
        # normalized default URL in Server Settings rather than displaying blank.
        for raw_blank in ("", "   "):
            put_resp = client.put(
                "/api/admin/settings",
                headers=admin_headers,
                json={"settings": {"feedback_repo_url": raw_blank}},
            )
            assert put_resp.status_code == 200
            clear_settings_cache()
            assert (
                get_settings().feedback_repo_url == "https://github.com/myk-org/rootcoz"
            )
            refreshed = {
                row["key"]: row
                for row in client.get(
                    "/api/admin/settings", headers=admin_headers
                ).json()
            }["feedback_repo_url"]
            assert refreshed["source"] == "db"
            assert refreshed["value"] == "https://github.com/myk-org/rootcoz"

    async def test_invalid_feedback_repo_url_refused_without_echoing_value(
        self, integration_env
    ) -> None:
        client, _ = integration_env
        _, _, headers = await _create_reviewer("fb-bad-repo")
        secret_bad_url = "https://ghp_supersecretvalue@evil.example.com/not-github"  # pragma: allowlist secret
        update_db_settings_cache({"feedback_repo_url": secret_bad_url})
        clear_settings_cache()

        resp = client.post(
            "/api/feedback/create",
            headers=headers,
            json={"title": "Bug", "body": "Details", "labels": ["bug"]},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "FEEDBACK_REPO_URL" in detail
        assert "ghp_supersecretvalue" not in detail
        assert "evil.example.com" not in detail


def test_public_base_url_goes_through_the_shared_url_predicate() -> None:
    """#562: internal whitespace is refused — the predicate is the one definition."""
    assert (
        main._attachment_public_base_url(
            main.Settings(public_base_url="https://rootcoz.example.com/a b")
        )
        is None
    )
    assert (
        main._attachment_public_base_url(
            main.Settings(public_base_url="https://rootcoz.example.com/sub/")
        )
        == "https://rootcoz.example.com/sub"
    )


class TestCapabilitiesAndOrchestration:
    """Item 4 + R1 + R5: capabilities contract, PUBLIC_BASE_URL, budgets, Jira fallback, rollback."""

    def test_capabilities_matches_wire_contract(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = integration_env
        expected_capabilities = {
            "max_files": 3,
            "max_file_bytes": 5242880,
            "max_total_bytes": 10485760,
            "max_run_bytes": 209715200,
            "max_owner_bytes": 524288000,
            "types": [
                "text/plain",
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            ],
            "report_github": {"available": True, "reason": "available"},
            "report_jira": {"available": True, "reason": "available"},
            "feedback": {"available": True, "reason": "available"},
            "chat_ui": {"available": False, "reason": "browser_chat_unsupported"},
            "direct_chat": {"available": True, "reason": "available"},
        }
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}
        resp = client.get("/api/capabilities", headers=admin_headers)
        assert resp.status_code == 200
        cap = resp.json()["issue_attachments"]
        assert cap == expected_capabilities

        # Removing PUBLIC_BASE_URL flips enabled trackers to public_base_url_required,
        # while disabled tracker stays tracker_disabled and chat_ui stays browser_chat_unsupported.
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setenv("ENABLE_JIRA_ISSUES", "false")
        clear_settings_cache()
        cap2 = client.get("/api/capabilities", headers=admin_headers).json()[
            "issue_attachments"
        ]
        assert cap2["report_github"] == {
            "available": False,
            "reason": "public_base_url_required",
        }
        assert cap2["feedback"] == {
            "available": False,
            "reason": "public_base_url_required",
        }
        assert cap2["direct_chat"] == {
            "available": False,
            "reason": "public_base_url_required",
        }
        assert cap2["report_jira"] == {"available": False, "reason": "tracker_disabled"}
        assert cap2["chat_ui"] == {
            "available": False,
            "reason": "browser_chat_unsupported",
        }

    async def test_public_base_url_settings_only_and_preserves_prefix(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = integration_env
        _, _, headers = await _create_reviewer("alice-baseurl")
        job_id = await _seed_completed_job()

        # Missing PUBLIC_BASE_URL refuses file-bearing create even with Host/X-Forwarded headers,
        # while text-only create still succeeds and omits attachments field.
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        clear_settings_cache()
        with patch("rootcoz.main.create_github_issue") as mock_gh:
            refused = client.post(
                f"/results/{job_id}/create-github-issue",
                headers={
                    **headers,
                    "Host": "attacker.example.com",
                    "X-Forwarded-Host": "attacker.example.com",
                    "X-Forwarded-Proto": "https",
                },
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload()],
                },
            )
            assert refused.status_code == 400
            assert "PUBLIC_BASE_URL" in refused.json()["detail"]
            mock_gh.assert_not_called()

            mock_gh.return_value = {
                "url": "https://github.com/org/tests-repo/issues/1",
                "number": 1,
                "title": "T",
            }
            text_ok = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={"test_name": "test_example", "title": "T", "body": "B"},
            )
            assert text_ok.status_code == 201
            assert "attachments" not in text_ok.json()

        # Userinfo or query in PUBLIC_BASE_URL is rejected for attachments.
        for invalid_url in (
            "https://user:pw@rootcoz.example.com",  # pragma: allowlist secret
            "https://rootcoz.example.com?foo=1",
            "ftp://rootcoz.example.com",
        ):
            monkeypatch.setenv("PUBLIC_BASE_URL", invalid_url)
            clear_settings_cache()
            bad = client.post(
                "/api/feedback/create",
                headers=headers,
                json={
                    "title": "T",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload()],
                },
            )
            assert bad.status_code == 400
            assert "PUBLIC_BASE_URL" in bad.json()["detail"]

        # Valid deployment path prefix is preserved in hosted links.
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://rootcoz.example.com/subpath/")
        clear_settings_cache()
        with patch("rootcoz.main.create_github_issue") as mock_gh:
            mock_gh.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/5",
                "number": 5,
                "title": "T",
            }
            ok = client.post(
                "/api/feedback/create",
                headers=headers,
                json={
                    "title": "T",
                    "body": "Body text",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("trace.log", b"trace-bytes")],
                },
            )
        assert ok.status_code == 201, ok.text
        posted_body = mock_gh.call_args.kwargs["body"]
        att_id = ok.json()["attachments"]["files"][0]["id"]
        assert (
            f"https://rootcoz.example.com/subpath/api/issue-attachments/{att_id}"
            in posted_body
        )
        # Reporter footer and attachments section appear BEFORE final AI attribution.
        assert posted_body.index(
            "_Reported by: alice-baseurl via rootcoz feedback_"
        ) < posted_body.index("No AI model generated this issue")
        assert posted_body.index(
            f"/api/issue-attachments/{att_id}"
        ) < posted_body.index("No AI model generated this issue")

    async def test_r1_r5_owner_isolation_across_all_four_callers_and_no_owner_zero(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, db_path = integration_env
        # R5 structural assertions: no duplicate get_user_id, no username in store/table.
        assert not hasattr(storage, "get_user_id")
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(issue_attachments)")
            }
        assert "username" not in cols

        id_a, _, headers_a = await _create_reviewer("user-a")
        id_b, _, headers_b = await _create_reviewer("user-b")
        job_id = await _seed_completed_job(submitter="user-a")

        # Seed user-a close to 500 MiB ceiling directly in DB so user-a has < 10 bytes left,
        # while user-b has 0 bytes used.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO issue_attachments "
                "(id, submission_id, job_id, owner_id, media_type, display_name, size_bytes, tracker, state) "
                "VALUES (?, ?, '', ?, 'text/plain', 'big.log', ?, 'github', 'stored')",
                (str(uuid4()), str(uuid4()), id_a, 500 * 1024 * 1024 - 4),
            )

        # User A is refused on report GitHub, report Jira, feedback, and direct chat!
        for endpoint, payload in (
            (
                f"/results/{job_id}/create-github-issue",
                {
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("a.log", b"12345678")],
                },
            ),
            (
                f"/results/{job_id}/create-jira-bug",
                {
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("a.log", b"12345678")],
                },
            ),
            (
                "/api/feedback/create",
                {
                    "title": "T",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("a.log", b"12345678")],
                },
            ),
        ):
            refused = client.post(endpoint, headers=headers_a, json=payload)
            assert refused.status_code == 400, endpoint
            assert "ceiling 500 MiB" in refused.json()["detail"]
            assert "rootcoz delete-attachment" in refused.json()["detail"]

        # User B succeeds on all four callers on the SAME run and feedback/chat!
        with (
            patch(
                "rootcoz.main.create_github_issue",
                AsyncMock(
                    return_value={
                        "url": "https://github.com/org/tests-repo/issues/2",
                        "number": 2,
                        "title": "T",
                    }
                ),
            ),
            patch(
                "rootcoz.main.create_jira_bug",
                AsyncMock(
                    return_value={
                        "url": "https://jira.example.com/browse/PROJ-2",
                        "key": "PROJ-2",
                        "title": "T",
                    }
                ),
            ),
            patch(
                "rootcoz.main.add_jira_attachment",
                AsyncMock(return_value="native_attached"),
            ),
        ):
            r_gh = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers_b,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("b1.log", b"12345678")],
                },
            )
            assert r_gh.status_code == 201, r_gh.text
            r_jira = client.post(
                f"/results/{job_id}/create-jira-bug",
                headers=headers_b,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("b2.log", b"12345678")],
                },
            )
            assert r_jira.status_code == 201, r_jira.text
            r_fb = client.post(
                "/api/feedback/create",
                headers=headers_b,
                json={
                    "title": "T",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("b3.log", b"12345678")],
                },
            )
            assert r_fb.status_code == 201, r_fb.text
            prop = client.post(
                "/api/chat-issue/propose",
                headers=headers_b,
                json={
                    "target": "github",
                    "title": "T",
                    "body": "B",
                    "job_id": job_id,
                    "attachments": [_upload_payload("b4.log", b"12345678")],
                },
            )
            assert prop.status_code == 201, prop.text
            r_chat = client.post(
                "/api/chat-issue",
                headers=headers_b,
                json={
                    "target": "github",
                    "title": prop.json()["title"],
                    "body": prop.json()["body"],
                    "job_id": job_id,
                    "confirmation_id": prop.json()["confirmation_id"],
                    "confirmation_code": prop.json()["confirmation_code"],
                    "attachments": [_upload_payload("b4.log", b"12345678")],
                },
            )
            assert r_chat.status_code == 201, r_chat.text

        with sqlite3.connect(db_path) as conn:
            zero_rows = conn.execute(
                "SELECT COUNT(*) FROM issue_attachments WHERE owner_id <= 0"
            ).fetchone()[0]
            b_rows = conn.execute(
                "SELECT COUNT(*) FROM issue_attachments WHERE owner_id = ?", (id_b,)
            ).fetchone()[0]
        assert zero_rows == 0
        assert b_rows == 4

    async def test_no_profile_bootstrap_admin_refused_with_attachments_only(
        self, integration_env
    ) -> None:
        client, _ = integration_env
        job_id = await _seed_completed_job()
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}
        with patch.object(
            storage,
            "get_user_tokens",
            AsyncMock(return_value={"github_token": "ghp_temp_token"}),
        ):
            refused = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=admin_headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload()],
                },
            )
        assert refused.status_code == 400
        assert "profile" in refused.json()["detail"].lower()

    async def test_jira_prepublished_links_and_per_file_partial_outcomes(
        self, integration_env
    ) -> None:
        client, _ = integration_env
        _, _, headers = await _create_reviewer("jira-partial")
        job_id = await _seed_completed_job()
        seen_create_bodies: list[str] = []

        async def fake_create_jira_bug(**kwargs):
            seen_create_bodies.append(kwargs["body"])
            return {
                "url": "https://jira.example.com/browse/PROJ-77",
                "key": "PROJ-77",
                "title": kwargs["title"],
            }

        outcomes = iter(["native_attached", "native_outcome_unknown", "native_refused"])

        async def fake_add_att(conn, key, filename, data, media_type):
            # Hosted links MUST already exist in the issue body before upload runs!
            assert len(seen_create_bodies) == 1
            return next(outcomes)

        with (
            patch("rootcoz.main.create_jira_bug", side_effect=fake_create_jira_bug),
            patch("rootcoz.main.add_jira_attachment", side_effect=fake_add_att),
        ):
            resp = client.post(
                f"/results/{job_id}/create-jira-bug",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "Jira bug",
                    "body": "Jira body",
                    "attachments": [
                        _upload_payload("one.log", b"111"),
                        _upload_payload("two.png", _MIN_PNG),
                        _upload_payload("three.txt", b"333"),
                    ],
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["url"] == "https://jira.example.com/browse/PROJ-77"
        summary = data["attachments"]
        assert summary["count"] == 3
        assert summary["types"] == ["image/png", "text/plain"]
        assert summary["linked"] == 3
        assert summary["native_attached"] == 1
        assert summary["failed_count"] == 2
        assert [f["delivery"] for f in summary["files"]] == [
            "native_and_hosted",
            "native_outcome_unknown",
            "fallback_only",
        ]
        assert [f["reason"] for f in summary["files"]] == [
            "native_attached",
            "native_outcome_unknown",
            "native_refused",
        ]
        for f in summary["files"]:
            assert (
                f"https://rootcoz.example.com/api/issue-attachments/{f['id']}"
                in seen_create_bodies[0]
            )

    async def test_jira_sa_mode_adf_file_links_have_exact_names_and_hrefs(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = integration_env
        monkeypatch.setenv("JIRA_AUTH_MODE", "oauth")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLOUD_ID", str(uuid4()))
        monkeypatch.setenv("JIRA_SITE_URL", "https://acme.atlassian.net")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "deploy-cid")
        monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", "ATOA-deploy-secret")
        clear_settings_cache()

        _, _, headers = await _create_reviewer("jira-sa-user", jira_token="")
        await storage.save_user_tokens(
            "jira-sa-user",
            jira_sa_token="ATOA-caller-secret",  # pragma: allowlist secret
        )
        job_id = await _seed_completed_job(submitter="jira-sa-user")
        captured_payloads: list[dict[str, object]] = []

        class FakeJiraV3Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url: str, **kwargs):
                captured_payloads.append(kwargs["json"])
                return httpx.Response(
                    201,
                    json={"key": "PROJ-505"},
                    request=httpx.Request("POST", url),
                )

        with (
            patch(
                "rootcoz.jira.mint_atlassian_access_token",
                AsyncMock(return_value="minted-sa-token"),
            ),
            patch("rootcoz.bug_creation.httpx.AsyncClient", FakeJiraV3Client),
            patch(
                "rootcoz.main.add_jira_attachment",
                AsyncMock(return_value="native_attached"),
            ),
        ):
            resp = client.post(
                f"/results/{job_id}/create-jira-bug",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "ADF links",
                    "body": "Body text",
                    "attachments": [
                        _upload_payload("my-log_file.txt", b"log-1"),
                        _upload_payload("a|b[1].txt", b"log-2"),
                    ],
                },
            )
        assert resp.status_code == 201, resp.text
        files = resp.json()["attachments"]["files"]
        assert len(captured_payloads) == 1
        adf_doc = captured_payloads[0]["fields"]["description"]  # type: ignore[index]
        link_nodes = [
            inline
            for block in adf_doc["content"]  # type: ignore[index]
            if block["type"] == "bulletList"
            for item in block["content"]
            for para in item["content"]
            for inline in para["content"]
            if any(m.get("type") == "link" for m in inline.get("marks", []))
        ]
        by_href = {
            next(m["attrs"]["href"] for m in n["marks"] if m["type"] == "link"): n[
                "text"
            ]
            for n in link_nodes
        }
        url_0 = f"https://rootcoz.example.com/api/issue-attachments/{files[0]['id']}"
        url_1 = f"https://rootcoz.example.com/api/issue-attachments/{files[1]['id']}"
        assert by_href[url_0] == "my-log_file.txt"
        assert "\\" not in by_href[url_0]
        assert by_href[url_1] == "a_b_1_.txt"

    async def test_proven_not_filed_rolls_back_exact_submission_while_ambiguous_retains(
        self, integration_env
    ) -> None:
        client, db_path = integration_env
        _, _, headers = await _create_reviewer("rollback-user")
        job_id = await _seed_completed_job()

        # First create succeeds so scope already holds a live stored attachment.
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/org/tests-repo/issues/1",
                    "number": 1,
                    "title": "T",
                }
            ),
        ):
            first = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("keep.log", b"keep")],
                },
            )
        keep_id = first.json()["attachments"]["files"][0]["id"]

        # Second create fails with proven-not-filed 503 -> rolls back only second submission!
        req = httpx.Request(
            "POST", "https://api.github.com/repos/org/tests-repo/issues"
        )
        err_503 = httpx.HTTPStatusError(
            "503", request=req, response=httpx.Response(503, request=req)
        )
        with patch("rootcoz.main.create_github_issue", AsyncMock(side_effect=err_503)):
            second = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("drop.log", b"drop")],
                },
            )
        assert second.status_code == 502
        with sqlite3.connect(db_path) as conn:
            ids = [
                r[0]
                for r in conn.execute("SELECT id FROM issue_attachments").fetchall()
            ]
        assert ids == [keep_id]
        assert (
            client.get(f"/api/issue-attachments/{keep_id}", headers=headers).status_code
            == 200
        )

        # Third create fails with ReadTimeout (ambiguous!) -> retains hosted file!
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(side_effect=httpx.ReadTimeout("timeout")),
        ):
            third = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("maybe.log", b"maybe-filed")],
                },
            )
        assert third.status_code == 502
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, display_name, state FROM issue_attachments"
            ).fetchall()
        assert len(rows) == 2
        maybe_ids = [r[0] for r in rows if r[1] == "maybe.log"]
        assert len(maybe_ids) == 1
        dl = client.get(f"/api/issue-attachments/{maybe_ids[0]}", headers=headers)
        assert dl.status_code == 200
        assert dl.content == b"maybe-filed"

    async def test_jira_upload_deadlines_bound_per_file_and_total_loop_wall_clock(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert main._JIRA_ATTACHMENT_PER_FILE_TIMEOUT_SECONDS == 30.0
        assert main._JIRA_ATTACHMENTS_TOTAL_TIMEOUT_SECONDS == 65.0

        client, _ = integration_env
        _, _, headers = await _create_reviewer("jira-deadlines")
        job_id = await _seed_completed_job()
        seen_bodies: list[str] = []

        async def fake_create_jira_bug(**kwargs):
            seen_bodies.append(kwargs["body"])
            return {
                "url": "https://jira.example.com/browse/PROJ-99",
                "key": "PROJ-99",
                "title": kwargs["title"],
            }

        # 1) Total loop deadline interrupts a multi-file stall and marks remaining
        #    files native_outcome_unknown while returning 201 cleanly.
        attempted: list[str] = []

        async def stalling_multi_file_upload(conn, key, filename, data, media_type):
            attempted.append(filename)
            if filename == "one.log":
                await asyncio.sleep(0.02)
                return "native_attached"
            await asyncio.sleep(1.0)
            return "native_attached"

        monkeypatch.setattr(main, "_JIRA_ATTACHMENT_PER_FILE_TIMEOUT_SECONDS", 0.30)
        monkeypatch.setattr(main, "_JIRA_ATTACHMENTS_TOTAL_TIMEOUT_SECONDS", 0.10)
        with (
            patch("rootcoz.main.create_jira_bug", side_effect=fake_create_jira_bug),
            patch(
                "rootcoz.main.add_jira_attachment",
                side_effect=stalling_multi_file_upload,
            ),
        ):
            resp = client.post(
                f"/results/{job_id}/create-jira-bug",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "Total deadline bug",
                    "body": "Body",
                    "attachments": [
                        _upload_payload("one.log", b"111"),
                        _upload_payload("two.png", _MIN_PNG),
                        _upload_payload("three.txt", b"333"),
                    ],
                },
            )
        assert resp.status_code == 201, resp.text
        assert attempted == ["one.log", "two.png"]
        summary = resp.json()["attachments"]
        assert summary["count"] == 3
        assert summary["native_attached"] == 1
        assert summary["linked"] == 3
        assert summary["failed_count"] == 2
        assert [f["delivery"] for f in summary["files"]] == [
            "native_and_hosted",
            "native_outcome_unknown",
            "native_outcome_unknown",
        ]
        assert [f["reason"] for f in summary["files"]] == [
            "native_attached",
            "native_outcome_unknown",
            "native_outcome_unknown",
        ]
        for f in summary["files"]:
            assert (
                f"https://rootcoz.example.com/api/issue-attachments/{f['id']}"
                in seen_bodies[0]
            )
            assert (
                client.get(
                    f"/api/issue-attachments/{f['id']}", headers=headers
                ).status_code
                == 200
            )

        # 2) Per-file deadline interrupts a single stalled file while allowing
        #    subsequent files within the total loop budget to attach natively.
        async def single_file_stall(conn, key, filename, data, media_type):
            if filename == "slow.log":
                await asyncio.sleep(1.0)
            return "native_attached"

        monkeypatch.setattr(main, "_JIRA_ATTACHMENT_PER_FILE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(main, "_JIRA_ATTACHMENTS_TOTAL_TIMEOUT_SECONDS", 0.50)
        with (
            patch("rootcoz.main.create_jira_bug", side_effect=fake_create_jira_bug),
            patch("rootcoz.main.add_jira_attachment", side_effect=single_file_stall),
        ):
            resp2 = client.post(
                f"/results/{job_id}/create-jira-bug",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "Per-file deadline bug",
                    "body": "Body",
                    "attachments": [
                        _upload_payload("slow.log", b"slow"),
                        _upload_payload("fast.txt", b"fast"),
                    ],
                },
            )
        assert resp2.status_code == 201, resp2.text
        files2 = resp2.json()["attachments"]["files"]
        assert [f["delivery"] for f in files2] == [
            "native_outcome_unknown",
            "native_and_hosted",
        ]


class TestDirectChatManifestAndModelRefusal:
    """Item 4: direct chat draft manifest binding and model tool explicit refusal."""

    async def test_direct_chat_manifest_binds_exact_ordered_bytes_without_storing_blobs(
        self, integration_env
    ) -> None:
        client, db_path = integration_env
        _, _, headers = await _create_reviewer("chat-cli-user")
        job_id = await _seed_completed_job()

        prop = client.post(
            "/api/chat-issue/propose",
            headers=headers,
            json={
                "target": "github",
                "title": "CLI issue",
                "body": "CLI body",
                "job_id": job_id,
                "attachments": [
                    _upload_payload("a.log", b"alpha"),
                    _upload_payload("b.png", _MIN_PNG),
                ],
            },
        )
        assert prop.status_code == 201, prop.text
        pdata = prop.json()
        assert len(pdata["attachment_manifest"]) == 2
        assert pdata["attachment_manifest"][0]["filename"] == "a.log"
        assert pdata["attachment_manifest"][0]["media_type"] == "text/plain"
        assert pdata["attachment_manifest"][0]["size_bytes"] == 5
        assert len(pdata["attachment_manifest"][0]["sha256"]) == 64
        assert "a.log" in pdata["preview"]

        # Proposal stores ZERO rows in DB and ZERO bytes in draft!
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM issue_attachments").fetchone()[0]
                == 0
            )
        draft = main._chat_issue_drafts[pdata["confirmation_id"]]
        assert "alpha" not in json.dumps(draft)

        # Changed bytes, changed name, changed order, or missing files all fail 400 WITHOUT spending draft!
        for mutated in (
            [_upload_payload("a.log", b"TAMPERED"), _upload_payload("b.png", _MIN_PNG)],
            [
                _upload_payload("renamed.log", b"alpha"),
                _upload_payload("b.png", _MIN_PNG),
            ],
            [_upload_payload("b.png", _MIN_PNG), _upload_payload("a.log", b"alpha")],
            [],
        ):
            with patch("rootcoz.main.create_github_issue") as mock_gh:
                bad = client.post(
                    "/api/chat-issue",
                    headers=headers,
                    json={
                        "target": "github",
                        "title": pdata["title"],
                        "body": pdata["body"],
                        "job_id": job_id,
                        "confirmation_id": pdata["confirmation_id"],
                        "confirmation_code": pdata["confirmation_code"],
                        "attachments": mutated,
                    },
                )
            assert bad.status_code == 400
            mock_gh.assert_not_called()

        # Original approved bytes still redeem the unspent draft!
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/org/tests-repo/issues/9",
                    "number": 9,
                    "title": "CLI issue",
                }
            ),
        ):
            good = client.post(
                "/api/chat-issue",
                headers=headers,
                json={
                    "target": "github",
                    "title": pdata["title"],
                    "body": pdata["body"],
                    "job_id": job_id,
                    "confirmation_id": pdata["confirmation_id"],
                    "confirmation_code": pdata["confirmation_code"],
                    "attachments": [
                        _upload_payload("a.log", b"alpha"),
                        _upload_payload("b.png", _MIN_PNG),
                    ],
                },
            )
        assert good.status_code == 201, good.text
        assert good.json()["attachments"]["count"] == 2

    async def test_model_chat_tools_explicitly_refuse_attachments_and_paths(
        self, integration_env
    ) -> None:
        from rootcoz.engine.chat import build_chat_custom_tools, build_system_prompt

        client, _ = integration_env
        _, _, headers = await _create_reviewer("chat-tool-user")
        job_id = await _seed_completed_job()

        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id=job_id,
            tracker_targets=("github", "jira"),
        )
        by_name = {t["name"]: t for t in tools}
        for name in (
            "propose_tracker_issue",
            "create_github_issue",
            "create_jira_issue",
        ):
            assert by_name[name]["parameters"]["additionalProperties"] is False
            assert "attach" in by_name[name]["description"].lower()

        prompt = build_system_prompt("job", 1, job_id, tools)
        assert "--attach" in prompt

        # Conversation-bound proposal with attachments is refused with explicit reason.
        conv_refused = client.post(
            "/api/chat-issue/propose",
            headers=headers,
            json={
                "target": "github",
                "title": "T",
                "body": "B",
                "job_id": job_id,
                "conversation": job_id,
                "attachments": [_upload_payload()],
            },
        )
        assert conv_refused.status_code == 400
        assert "--attach" in conv_refused.json()["detail"]

        # Model tool header with interpolated rejected path arg is refused instead of silently stripped.
        for tool_name, endpoint, base_payload in (
            (
                "propose_tracker_issue",
                "/api/chat-issue/propose",
                {
                    "target": "jira",
                    "title": "T",
                    "body": "B",
                    "job_id": job_id,
                    "conversation": job_id,
                },
            ),
            (
                "create_github_issue",
                "/api/chat-issue",
                {
                    "target": "github",
                    "title": "T",
                    "body": "B",
                    "confirmation_id": "missing-draft-id",
                    "confirmation_code": "BADCODE",
                    "job_id": job_id,
                },
            ),
            (
                "create_jira_issue",
                "/api/chat-issue",
                {
                    "target": "jira",
                    "title": "T",
                    "body": "B",
                    "confirmation_id": "missing-draft-id",
                    "confirmation_code": "BADCODE",
                },
            ),
        ):
            template_hdrs = by_name[tool_name]["http"]["headers"]
            for forbidden_field, sample_val in (
                ("attachments", '[{"filename":"log.txt","data_b64":"aGVsbG8="}]'),
                ("data_b64", "aGVsbG8K"),
                ("file_path", "/tmp/issue505-sentinel.log"),
                ("attachment_path", "/tmp/issue505-sentinel.log"),
                ("attachment", "log.txt"),
                ("files", "log.txt"),
                ("path", "/tmp/issue505-sentinel.log"),
                ("paths", "/tmp/issue505-sentinel.log"),
            ):
                placeholder = f"{{{forbidden_field}}}"
                assert placeholder in template_hdrs["X-RootCoz-Chat-Tool-Rejected"], (
                    f"{tool_name} missing {placeholder} in X-RootCoz-Chat-Tool-Rejected"
                )
                tool_headers = dict(template_hdrs)
                tool_headers["Authorization"] = headers["Authorization"]
                tool_headers["X-RootCoz-Chat-Tool-Rejected"] = tool_headers[
                    "X-RootCoz-Chat-Tool-Rejected"
                ].replace(placeholder, sample_val)
                refused = client.post(
                    endpoint,
                    headers=tool_headers,
                    json=base_payload,
                )
                assert refused.status_code == 400, (
                    f"{tool_name} with {forbidden_field} returned {refused.status_code}: {refused.text}"
                )
                detail = refused.json()["detail"]
                assert "local paths" in detail and "--attach" in detail

                # Even if a caller sends X-RootCoz-Chat-Tool: 1 with raw extra field in JSON body
                # (without interpolating X-RootCoz-Chat-Tool-Rejected), attachment/path refusal
                # still fires BEFORE confirmation_id validation.
                if forbidden_field != "attachments":
                    raw_headers = {
                        "Authorization": headers["Authorization"],
                        "X-RootCoz-Chat-Tool": "1",
                    }
                    raw_refused = client.post(
                        endpoint,
                        headers=raw_headers,
                        json={**base_payload, forbidden_field: sample_val},
                    )
                    assert raw_refused.status_code == 400
                    assert (
                        "local paths" in raw_refused.json()["detail"]
                        and "--attach" in raw_refused.json()["detail"]
                    )


class TestDownloadSecurityAndAuditDedupe:
    """Item 4 + R6 + R7: live authorization, O_NOFOLLOW fd streaming, disconnect close, 4096 LRU dedupe."""

    async def test_r6_r7_download_auth_headers_symlinks_disconnect_and_lru_dedupe(
        self,
        integration_env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, db_path = integration_env
        _, _, uploader_headers = await _create_reviewer("uploader")
        _, _, viewer_headers = await _create_reviewer("teammate-viewer", role="viewer")
        _, _, stranger_headers = await _create_reviewer("stranger")
        job_id = await _seed_completed_job()

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/1",
                    "number": 1,
                    "title": "T",
                }
            ),
        ):
            run_resp = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=uploader_headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("config.yaml", b"key: val\n")],
                },
            )
            fb_resp = client.post(
                "/api/feedback/create",
                headers=uploader_headers,
                json={
                    "title": "FB",
                    "body": "FB body",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("secret name.png", _MIN_PNG)],
                },
            )
        run_att_id = run_resp.json()["attachments"]["files"][0]["id"]
        fb_att_id = fb_resp.json()["attachments"]["files"][0]["id"]

        # Unknown / invalid UUID writes NO audit row and returns 404.
        assert (
            client.get(
                "/api/issue-attachments/not-a-uuid", headers=uploader_headers
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/issue-attachments/{uuid4()}", headers=uploader_headers
            ).status_code
            == 404
        )
        assert (await storage.list_audit_events(action="issue_attachment.download"))[
            "events"
        ] == []

        # Anonymous returns 401.
        assert client.get(f"/api/issue-attachments/{run_att_id}").status_code == 401

        # Viewer teammate can read run-scoped file; headers force text/plain + attachment + nosniff + no-store.
        dl_run = client.get(
            f"/api/issue-attachments/{run_att_id}", headers=viewer_headers
        )
        assert dl_run.status_code == 200
        assert dl_run.content == b"key: val\n"
        assert dl_run.headers["content-type"].startswith("text/plain")
        assert dl_run.headers["x-content-type-options"] == "nosniff"
        assert dl_run.headers["cache-control"] == "private, no-store"
        assert (
            f'attachment; filename="{run_att_id}"'
            in dl_run.headers["content-disposition"]
        )

        # Stranger CANNOT read run-less feedback attachment -> 404 + 1 denied audit row without filename!
        denied = client.get(
            f"/api/issue-attachments/{fb_att_id}", headers=stranger_headers
        )
        assert denied.status_code == 404
        assert "secret name" not in denied.text
        events = (await storage.list_audit_events(action="issue_attachment.download"))[
            "events"
        ]
        assert len(events) == 2
        denied_ev = next(e for e in events if e["outcome"] == "denied")
        assert set(denied_ev["details"]) == {
            "attachment_id",
            "media_type",
            "size_bytes",
            "tracker",
            "refused_kind",
        }
        assert "secret name" not in json.dumps(denied_ev)

        # R7: default bound is 4096; repeat read is DEBUG-only; denied->success records both; eviction & restart re-record!
        assert main._DOWNLOAD_AUDIT_DEDUPE_MAX == 4096
        with capture_main_logs(caplog, level=logging.DEBUG):
            repeat = client.get(
                f"/api/issue-attachments/{run_att_id}", headers=viewer_headers
            )
        assert repeat.status_code == 200
        assert (
            len(
                (await storage.list_audit_events(action="issue_attachment.download"))[
                    "events"
                ]
            )
            == 2
        )

        # Promote stranger to admin -> denied-to-success for same caller+attachment records a new success row!
        await storage.change_user_role("stranger", "admin")
        now_ok = client.get(
            f"/api/issue-attachments/{fb_att_id}", headers=stranger_headers
        )
        assert now_ok.status_code == 200
        assert now_ok.content == _MIN_PNG
        assert (
            len(
                (await storage.list_audit_events(action="issue_attachment.download"))[
                    "events"
                ]
            )
            == 3
        )

        # LRU eviction test with cap=1: reading fb_att_id evicts run_att_id, so run_att_id records again!
        monkeypatch.setattr(main, "_DOWNLOAD_AUDIT_DEDUPE_MAX", 1)
        main._download_audit_dedupe.clear()
        assert (
            client.get(
                f"/api/issue-attachments/{run_att_id}", headers=uploader_headers
            ).status_code
            == 200
        )
        assert (
            len(
                (await storage.list_audit_events(action="issue_attachment.download"))[
                    "events"
                ]
            )
            == 4
        )
        assert (
            client.get(
                f"/api/issue-attachments/{fb_att_id}", headers=uploader_headers
            ).status_code
            == 200
        )
        assert (
            len(
                (await storage.list_audit_events(action="issue_attachment.download"))[
                    "events"
                ]
            )
            == 5
        )
        # run_att_id was evicted from the 1-slot LRU -> records again!
        assert (
            client.get(
                f"/api/issue-attachments/{run_att_id}", headers=uploader_headers
            ).status_code
            == 200
        )
        assert (
            len(
                (await storage.list_audit_events(action="issue_attachment.download"))[
                    "events"
                ]
            )
            == 6
        )

        # R6: StreamingResponse closes the exact opened fd even on mid-stream disconnect!
        closed_flag = {"closed": False}
        real_open = issue_attachments.open_attachment

        class TrackingFile:
            def __init__(self, inner):
                self._inner = inner

            def read(self, n: int = -1) -> bytes:
                return self._inner.read(2)

            def close(self) -> None:
                closed_flag["closed"] = True
                self._inner.close()

        monkeypatch.setattr(
            issue_attachments,
            "open_attachment",
            lambda row: TrackingFile(real_open(row)),
        )
        gen = main._iter_attachment_stream(
            issue_attachments.open_attachment(
                await storage.get_issue_attachment(run_att_id)
            )
        )
        first_chunk = await anext(gen)
        assert first_chunk == b"ke"
        await gen.aclose()
        assert closed_flag["closed"] is True

        # R6: Replacing leaf blob with symlink to outside file returns 404 and never target bytes!
        outside = db_path.parent / "outside-secret.txt"
        outside.write_bytes(b"key: TOPSECRET")  # same 9-byte length if matched
        blob_path = db_path.parent / "issue-attachments" / "jobs" / job_id / run_att_id
        blob_path.unlink()
        blob_path.symlink_to(outside)
        sym_resp = client.get(
            f"/api/issue-attachments/{run_att_id}", headers=uploader_headers
        )
        assert sym_resp.status_code == 404
        assert b"TOPSECRET" not in sym_resp.content

        # Finding #5: Downloading all 3 attachments of a single issue submission records
        # at most ONE success audit row per caller (and at most ONE denied row per caller)!
        monkeypatch.setattr(main, "_DOWNLOAD_AUDIT_DEDUPE_MAX", 4096)
        main._download_audit_dedupe.clear()
        _, _, multi_stranger_headers = await _create_reviewer("multi-stranger")
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/33",
                    "number": 33,
                    "title": "Multi",
                }
            ),
        ):
            multi_fb = client.post(
                "/api/feedback/create",
                headers=uploader_headers,
                json={
                    "title": "Multi FB",
                    "body": "Body",
                    "labels": ["bug"],
                    "attachments": [
                        _upload_payload("one.log", b"111"),
                        _upload_payload("two.txt", b"222"),
                        _upload_payload("three.png", _MIN_PNG),
                    ],
                },
            )
        assert multi_fb.status_code == 201
        multi_ids = [f["id"] for f in multi_fb.json()["attachments"]["files"]]
        assert len(multi_ids) == 3

        before_events = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        for mid in multi_ids:
            dl_multi = client.get(
                f"/api/issue-attachments/{mid}", headers=uploader_headers
            )
            assert dl_multi.status_code == 200
        after_ok_events = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        assert after_ok_events == before_events + 1

        for mid in multi_ids:
            den_multi = client.get(
                f"/api/issue-attachments/{mid}", headers=multi_stranger_headers
            )
            assert den_multi.status_code == 404
        after_den_events = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        assert after_den_events == after_ok_events + 1


class TestEnvelopeMiddlewareAndRedaction:
    """Item 4: authenticated 15 MiB actual-body ASGI bound and 422/body log redaction."""

    async def test_envelope_bound_declared_and_lying_stream_and_422_redaction(
        self,
        integration_env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, _ = integration_env
        _, _, headers = await _create_reviewer("env-user")
        job_id = await _seed_completed_job()

        # Lower MAX_ENVELOPE_BYTES via monkeypatch to test actual byte counting cheaply in memory!
        monkeypatch.setattr(issue_attachments, "MAX_ENVELOPE_BYTES", 512)

        # 1) Declared Content-Length overflow -> 413 JSONResponse + actor-attributed audit row before writer!
        with patch("rootcoz.main.create_github_issue") as mock_gh:
            over = client.post(
                "/api/feedback/create",
                headers=headers,
                content=b'{"title":"T","body":"' + (b"x" * 600) + b'"}',
            )
        assert over.status_code == 413
        mock_gh.assert_not_called()
        fb_events = (await storage.list_audit_events(action="feedback.create"))[
            "events"
        ]
        assert fb_events[-1]["actor"] == "env-user"
        assert fb_events[-1]["details"]["reason"] == "payload_too_large"

        # 2) Lying small Content-Length with >512 actual streamed ASGI chunks -> 413 before handler!
        sent_status: list[int] = []

        chunks = iter(
            [
                {
                    "type": "http.request",
                    "body": b'{"title":"T","body":"' + b"a" * 300,
                    "more_body": True,
                },
                {
                    "type": "http.request",
                    "body": b"b" * 300 + b'"}',
                    "more_body": False,
                },
            ]
        )

        async def fake_receive():
            return next(chunks)

        async def fake_send(msg):
            if msg["type"] == "http.response.start":
                sent_status.append(msg["status"])

        mw = main.AttachmentEnvelopeBoundMiddleware(
            lambda s, r, snd: pytest.fail(
                "downstream handler must not be called on overflow"
            )
        )
        await mw(
            {
                "type": "http",
                "method": "POST",
                "path": f"/results/{job_id}/create-github-issue",
                "raw_path": f"/results/{job_id}/create-github-issue".encode(),
                "query_string": b"",
                "headers": [
                    (b"content-length", b"10"),
                    (b"content-type", b"application/json"),
                ],
                "state": {
                    "username": "env-user",
                    "role": "reviewer",
                    "is_admin": False,
                },
            },
            fake_receive,
            fake_send,
        )
        assert sent_status == [413]

        # 3) Preview endpoint is untouched even above attachment envelope limit!
        monkeypatch.setattr(issue_attachments, "MAX_ENVELOPE_BYTES", 100)
        with (
            patch.object(
                main,
                "_validate_catalog_pair",
                AsyncMock(return_value=("claude", "sonnet-4-5")),
            ),
            patch("rootcoz.feedback.call_ai_once") as mock_ai,
        ):
            from pi_sidecar_client import AIResult

            mock_ai.return_value = AIResult(
                success=True,
                text=json.dumps({"title": "T", "body": "B", "labels": ["bug"]}),
            )
            prev = client.post(
                "/api/feedback/preview",
                headers=headers,
                json={"description": "d" * 300},
            )
        assert prev.status_code == 200

        # 4) Redacted 422 and body logging on report create routes never leak filename or base64!
        monkeypatch.setattr(issue_attachments, "MAX_ENVELOPE_BYTES", 15 * 1024 * 1024)
        secret_b64 = _b64(b"SUPER_SECRET_ATTACHMENT_BYTES_12345")
        with capture_main_logs(caplog, level=logging.DEBUG):
            bad_422 = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "",  # invalid min_length -> 422
                    "body": "SECRET_ISSUE_BODY_TEXT",
                    "attachments": [
                        {
                            "filename": "confidential-customer.log",
                            "data_b64": secret_b64,
                            "extra_bad": 1,
                        }
                    ],
                },
            )
        assert bad_422.status_code == 422
        for leak in (
            "SUPER_SECRET_ATTACHMENT_BYTES",
            secret_b64,
            "confidential-customer.log",
            "SECRET_ISSUE_BODY_TEXT",
        ):
            assert leak not in bad_422.text
            assert leak not in caplog.text

        # 5) Trailing-slash attachment routes (/api/feedback/create/, /api/chat-issue/,
        # /results/{job_id}/create-github-issue/) NEVER log body or base64 at DEBUG level
        # across BOTH RequestBodyLoggingMiddleware and _validation_error_handler!
        from fastapi.exceptions import RequestValidationError
        from starlette.requests import Request as StarletteRequest

        caplog.clear()
        trailing_secret_b64 = _b64(b"TRAILING_SLASH_SECRET_PAYLOAD_98765")
        with capture_main_logs(caplog, level=logging.DEBUG):
            for slash_path, payload in (
                (
                    "/api/feedback/create/",
                    {
                        "title": "Valid",
                        "body": "TRAILING_SECRET_BODY_OK",
                        "labels": ["bug"],
                        "attachments": [
                            {
                                "filename": "trailing-secret.log",
                                "data_b64": trailing_secret_b64,
                            }
                        ],
                    },
                ),
                (
                    "/api/chat-issue/",
                    {
                        "target": "github",
                        "title": "Valid",
                        "body": "TRAILING_SECRET_CHAT_BODY",
                        "confirmation_id": "cid",
                        "confirmation_code": "code",
                        "attachments": [
                            {
                                "filename": "trailing-secret.log",
                                "data_b64": trailing_secret_b64,
                            }
                        ],
                    },
                ),
                (
                    f"/results/{job_id}/create-github-issue/",
                    {
                        "test_name": "test_example",
                        "title": "Valid",
                        "body": "TRAILING_SECRET_REPORT_BODY",
                        "attachments": [
                            {
                                "filename": "trailing-secret.log",
                                "data_b64": trailing_secret_b64,
                            }
                        ],
                    },
                ),
            ):
                client.post(slash_path, headers=headers, json=payload)
                fake_req = StarletteRequest(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": slash_path,
                        "raw_path": slash_path.encode(),
                        "query_string": b"",
                        "headers": [(b"content-type", b"application/json")],
                        "state": {
                            "username": "env-user",
                            "role": "reviewer",
                            "is_admin": False,
                        },
                    }
                )
                val_exc = RequestValidationError(
                    errors=[
                        {
                            "type": "value_error",
                            "loc": ("body", "attachments", 0, "data_b64"),
                            "msg": "Invalid attachment",
                            "input": trailing_secret_b64,
                        }
                    ],
                    body=payload,
                )
                val_resp = await main._validation_error_handler(fake_req, val_exc)
                assert val_resp.status_code == 422
                assert trailing_secret_b64.encode() not in val_resp.body
        for leak in (
            "TRAILING_SLASH_SECRET_PAYLOAD",
            trailing_secret_b64,
            "trailing-secret.log",
            "TRAILING_SECRET_BODY_OK",
            "TRAILING_SECRET_CHAT_BODY",
            "TRAILING_SECRET_REPORT_BODY",
        ):
            assert leak not in caplog.text


class TestLifecycleAndResiduals:
    """Item 4 + R2 + R3 + R4 + R8: job deletion, account deletion, markers, and startup reclaim."""

    async def test_r2_r3_r4_r8_account_delete_markers_recreate_and_reclaim(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, db_path = integration_env
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}

        # R8 part 1: unknown user delete returns 404 and writes NO marker.
        resp_404 = client.delete(
            "/api/admin/users/ghost-no-such-user", headers=admin_headers
        )
        assert resp_404.status_code == 404
        users_att_root = db_path.parent / "issue-attachments" / "users"
        assert not users_att_root.exists() or list(users_att_root.iterdir()) == []

        # Create alice (owner_id_1), upload one run-scoped file AND one run-less feedback file.
        owner_id_1, _, alice_headers_1 = await _create_reviewer("alice-recreate")
        _, _, viewer_headers = await _create_reviewer("viewer-check", role="viewer")
        job_id = await _seed_completed_job(submitter="viewer-check")

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/1",
                    "number": 1,
                    "title": "T",
                }
            ),
        ):
            run_create = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=alice_headers_1,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("run.log", b"run-evidence")],
                },
            )
            fb_create = client.post(
                "/api/feedback/create",
                headers=alice_headers_1,
                json={
                    "title": "FB",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("fb.log", b"fb-evidence")],
                },
            )
        run_att_id = run_create.json()["attachments"]["files"][0]["id"]
        fb_att_id_1 = fb_create.json()["attachments"]["files"][0]["id"]

        # R8 part 2: false return from delete_user while owner still exists clears attempt marker & keeps files.
        with patch.object(storage, "delete_user", AsyncMock(return_value=False)):
            false_404 = client.delete(
                "/api/admin/users/alice-recreate", headers=admin_headers
            )
        assert false_404.status_code == 404
        marker_1 = delete_marker(users_att_root / str(owner_id_1))
        assert not marker_1.exists()
        assert (users_att_root / str(owner_id_1) / fb_att_id_1).exists()

        # R3 + R2: delete alice-recreate while injecting row-delete failure!
        # Tree unlinks, run-less row stays for now, marker SURVIVES, and run-scoped file SURVIVES!
        with patch.object(
            storage,
            "delete_issue_attachment_rows_for_user",
            AsyncMock(
                side_effect=sqlite3.OperationalError("injected row delete failure")
            ),
        ):
            del_ok = client.delete(
                "/api/admin/users/alice-recreate", headers=admin_headers
            )
        assert del_ok.status_code == 200
        assert not (users_att_root / str(owner_id_1)).exists()
        assert marker_1.exists(), (
            "R3: marker must survive when row delete fails after directory unlink"
        )
        # R2: live run file & link still survive and download!
        assert (
            client.get(
                f"/api/issue-attachments/{run_att_id}", headers=viewer_headers
            ).status_code
            == 200
        )

        # R4: Re-register SAME username 'alice-recreate' -> gets new AUTOINCREMENT id!
        owner_id_2, _, alice_headers_2 = await _create_reviewer("alice-recreate")
        assert owner_id_2 != owner_id_1
        # Recreated username CANNOT read old owner_id_1 run-less row even while row still exists in DB!
        assert (
            client.get(
                f"/api/issue-attachments/{fb_att_id_1}", headers=alice_headers_2
            ).status_code
            == 404
        )

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/2",
                    "number": 2,
                    "title": "T2",
                }
            ),
        ):
            fb_create_2 = client.post(
                "/api/feedback/create",
                headers=alice_headers_2,
                json={
                    "title": "FB2",
                    "body": "B2",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("fb2.log", b"new-alice-bytes")],
                },
            )
        fb_att_id_2 = fb_create_2.json()["attachments"]["files"][0]["id"]

        # Backdate old marker_1 so startup reclaim treats it as pre-boot leftover, then run startup reclaim!
        os.utime(marker_1, (1, 1))
        job_snap = await main._startup_job_ids()
        owner_snap = await main._startup_owner_ids()
        assert owner_id_1 not in (owner_snap or set())
        assert owner_id_2 in (owner_snap or set())

        reclaimed = await main._reclaim_abandoned_workspaces(job_snap, owner_snap)
        assert reclaimed == 1
        assert not marker_1.exists()
        with sqlite3.connect(db_path) as conn:
            old_rows = conn.execute(
                "SELECT COUNT(*) FROM issue_attachments WHERE owner_id = ? AND job_id = ''",
                (owner_id_1,),
            ).fetchone()[0]
            run_rows = conn.execute(
                "SELECT COUNT(*) FROM issue_attachments WHERE id = ?",
                (run_att_id,),
            ).fetchone()[0]
        assert old_rows == 0
        assert run_rows == 1
        # New alice's run-less file and old alice's run-scoped file BOTH remain intact!
        assert (
            client.get(
                f"/api/issue-attachments/{fb_att_id_2}", headers=alice_headers_2
            ).content
            == b"new-alice-bytes"
        )
        assert (
            client.get(
                f"/api/issue-attachments/{run_att_id}", headers=viewer_headers
            ).content
            == b"run-evidence"
        )

        # R8 part 3: concurrent disappearance (user deleted right before storage.delete_user returns False)
        # still returns 404 while cleaning up old owner_id's run-less files/rows/marker!
        real_delete_user = storage.delete_user

        async def concurrent_vanish(uname: str) -> bool:
            await real_delete_user(uname)
            return False

        with patch.object(storage, "delete_user", side_effect=concurrent_vanish):
            vanish_resp = client.delete(
                "/api/admin/users/alice-recreate", headers=admin_headers
            )
        assert vanish_resp.status_code == 404
        assert not (users_att_root / str(owner_id_2)).exists()
        assert not delete_marker(users_att_root / str(owner_id_2)).exists()
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM issue_attachments WHERE owner_id = ? AND job_id = ''",
                    (owner_id_2,),
                ).fetchone()[0]
                == 0
            )

    async def test_startup_reclaim_pending_submissions_and_job_deletion_independence(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, db_path = integration_env
        owner_id, _, headers = await _create_reviewer("live-owner", role="operator")
        job_id = await _seed_completed_job(submitter="live-owner")

        # Upload a live run-less file AND a live run-scoped file.
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/3",
                    "number": 3,
                    "title": "T",
                }
            ),
        ):
            live_fb = client.post(
                "/api/feedback/create",
                headers=headers,
                json={
                    "title": "FB",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("keep.log", b"keep-live")],
                },
            )
            live_run = client.post(
                f"/results/{job_id}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("job.log", b"job-bytes")],
                },
            )
        keep_fb_id = live_fb.json()["attachments"]["files"][0]["id"]
        run_file_id = live_run.json()["attachments"]["files"][0]["id"]

        # Simulate an interrupted reserved row (with partial blob) on the SAME live owner!
        validated = issue_attachments.validate_attachments(
            [IssueAttachmentUpload(filename="crashed.log", data_b64=_b64(b"partial"))]
        )
        reserved = await storage.reserve_issue_attachments(
            validated, owner_id=owner_id, job_id="", tracker="github"
        )
        crashed_id = reserved[0]["id"]
        crashed_blob = (
            db_path.parent / "issue-attachments" / "users" / str(owner_id) / crashed_id
        )
        crashed_blob.write_bytes(b"part")

        # Startup reclaim cleans ONLY crashed_id, leaving keep_fb_id and owner tree intact!
        reclaimed = await main._reclaim_abandoned_workspaces(
            await main._startup_job_ids(), await main._startup_owner_ids()
        )
        assert reclaimed == 1
        assert not crashed_blob.exists()
        assert await storage.get_issue_attachment(crashed_id) is None
        assert (
            client.get(f"/api/issue-attachments/{keep_fb_id}", headers=headers).content
            == b"keep-live"
        )

        # Job deletion still cleans attachment dir even when chat cleanup returns False!
        with patch.object(
            main, "_cleanup_deleted_job_chat_workspaces", AsyncMock(return_value=False)
        ):
            del_job = client.delete(f"/results/{job_id}", headers=headers)
        assert del_job.status_code == 200
        assert not (db_path.parent / "issue-attachments" / "jobs" / job_id).exists()
        assert not (
            db_path.parent
            / "issue-attachments"
            / "jobs"
            / f".job-{job_id}.rootcoz-deleting"
        ).exists()
        assert await storage.get_issue_attachment(run_file_id) is None
        # Live run-less file still untouched!
        assert (
            client.get(
                f"/api/issue-attachments/{keep_fb_id}", headers=headers
            ).status_code
            == 200
        )

        # Finding #1: Unmarked directory under issue-attachments/jobs/ is NEVER swept
        # by startup recovery; ONLY explicit .job-<id>.rootcoz-deleting markers from
        # an interrupted inline deletion request are retried!
        unmarked_dead_job = str(uuid4())
        unmarked_dir = db_path.parent / "issue-attachments" / "jobs" / unmarked_dead_job
        unmarked_dir.mkdir(parents=True, exist_ok=True)
        (unmarked_dir / str(uuid4())).write_bytes(b"unmarked")
        os.utime(unmarked_dir, (1, 1))
        assert (
            await main._reclaim_abandoned_workspaces(
                await main._startup_job_ids(), await main._startup_owner_ids()
            )
            == 0
        )
        assert unmarked_dir.exists()

        # Now delete a real job while injecting inline unlink failure -> writes explicit
        # .job-<job_id_2>.rootcoz-deleting marker, which startup recovery retries cleanly!
        job_id_2 = await _seed_completed_job(submitter="live-owner")
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/4",
                    "number": 4,
                    "title": "T",
                }
            ),
        ):
            run_2 = client.post(
                f"/results/{job_id_2}/create-github-issue",
                headers=headers,
                json={
                    "test_name": "test_example",
                    "title": "T",
                    "body": "B",
                    "attachments": [_upload_payload("job2.log", b"job2-bytes")],
                },
            )
        assert run_2.status_code == 201
        job_marker_2 = (
            db_path.parent
            / "issue-attachments"
            / "jobs"
            / f".job-{job_id_2}.rootcoz-deleting"
        )
        job_dir_2 = db_path.parent / "issue-attachments" / "jobs" / job_id_2
        with patch.object(
            issue_attachments,
            "delete_job_attachments_dir",
            side_effect=OSError(13, "injected busy"),
        ):
            del_job_2 = client.delete(f"/results/{job_id_2}", headers=headers)
        assert del_job_2.status_code == 200
        assert job_dir_2.exists()
        assert job_marker_2.exists()
        os.utime(job_dir_2, (1, 1))
        os.utime(job_marker_2, (1, 1))
        assert (
            await main._reclaim_abandoned_workspaces(
                await main._startup_job_ids(), await main._startup_owner_ids()
            )
            == 1
        )
        assert not job_dir_2.exists()
        assert not job_marker_2.exists()
        assert unmarked_dir.exists()

    async def test_remaining_r1_r3_r5_r6_r7_openapi_and_reclaim_edges(
        self,
        integration_env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import annotationlib
        import inspect
        import shutil

        client, db_path = integration_env
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}

        # 1) R5 signature checks: no username parameter on store or reserve_issue_attachments.
        assert (
            "username"
            not in inspect.signature(
                issue_attachments.store,
                annotation_format=annotationlib.Format.STRING,
            ).parameters
        )
        assert (
            "username"
            not in inspect.signature(
                storage.reserve_issue_attachments,
                annotation_format=annotationlib.Format.STRING,
            ).parameters
        )

        # 2) OpenAPI operationId and request schema checks (Testing 11).
        openapi = client.get("/openapi.json").json()
        assert (
            openapi["paths"]["/api/issue-attachments/{attachment_id}"]["get"][
                "operationId"
            ]
            == "downloadIssueAttachment"
        )
        assert (
            openapi["paths"]["/api/issue-attachments/{attachment_id}"]["delete"][
                "operationId"
            ]
            == "deleteIssueAttachment"
        )
        assert (
            "attachments"
            not in openapi["components"]["schemas"]["FeedbackRequest"]["properties"]
        )
        for schema_name in (
            "CreateIssueRequest",
            "ChatIssueProposeRequest",
            "ChatIssueRequest",
            "FeedbackCreateRequest",
        ):
            assert (
                "attachments"
                in openapi["components"]["schemas"][schema_name]["properties"]
            )

        # 3) R1 concurrent reservations under same owner serialize cleanly without exceeding ceiling.
        uid_race, _, _ = await _create_reviewer("race-owner")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO issue_attachments "
                "(id, submission_id, job_id, owner_id, media_type, display_name, size_bytes, tracker, state) "
                "VALUES (?, ?, '', ?, 'text/plain', 'seed.log', ?, 'github', 'stored')",
                (str(uuid4()), str(uuid4()), uid_race, 500 * 1024 * 1024 - 10),
            )
        six_bytes = [IssueAttachmentUpload(filename="c.log", data_b64=_b64(b"123456"))]
        cfg = get_settings()
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/77",
                    "number": 77,
                    "title": "T",
                }
            ),
        ):
            outcomes = await asyncio.gather(
                main._create_issue_from_text(
                    "github",
                    "T",
                    "B",
                    username="race-owner",
                    settings=cfg,
                    github_token="ghp_tok",
                    github_repo_url=cfg.feedback_repo_url,
                    attachments=six_bytes,
                ),
                main._create_issue_from_text(
                    "github",
                    "T",
                    "B",
                    username="race-owner",
                    settings=cfg,
                    github_token="ghp_tok",
                    github_repo_url=cfg.feedback_repo_url,
                    attachments=six_bytes,
                ),
                return_exceptions=True,
            )
        successes = [o for o in outcomes if isinstance(o, dict)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1

        # 4) R6 cross-account symlink, parent symlink, root symlink, and swap-after-open fd stability!
        uid_x, _, headers_x = await _create_reviewer("owner-x")
        uid_y, _, headers_y = await _create_reviewer("owner-y")
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/88",
                    "number": 88,
                    "title": "T",
                }
            ),
        ):
            rx = client.post(
                "/api/feedback/create",
                headers=headers_x,
                json={
                    "title": "TX",
                    "body": "BX",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("x.log", b"XXXXXX")],
                },
            )
            ry = client.post(
                "/api/feedback/create",
                headers=headers_y,
                json={
                    "title": "TY",
                    "body": "BY",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("y.log", b"YYYYYY")],
                },
            )
        att_x = rx.json()["attachments"]["files"][0]["id"]
        att_y = ry.json()["attachments"]["files"][0]["id"]

        # Swap-after-open race: unlinking/replacing pathname after open_attachment still streams original fd bytes!
        row_x = await storage.get_issue_attachment(att_x)
        assert row_x is not None
        opened_fd_file = issue_attachments.open_attachment(row_x)
        path_x = db_path.parent / "issue-attachments" / "users" / str(uid_x) / att_x
        path_x.unlink()
        path_x.write_bytes(b"SWAPPED!")
        streamed = b"".join(
            [chunk async for chunk in main._iter_attachment_stream(opened_fd_file)]
        )
        assert streamed == b"XXXXXX"

        # Cross-account symlink (owner_x leaf -> owner_y leaf) returns 404, never YYYYYY!
        path_x.unlink()
        path_y = db_path.parent / "issue-attachments" / "users" / str(uid_y) / att_y
        path_x.symlink_to(path_y)
        cross = client.get(f"/api/issue-attachments/{att_x}", headers=headers_x)
        assert cross.status_code == 404
        assert b"YYYYYY" not in cross.content

        # Parent symlink (users/<uid_x> -> users/<uid_y>) returns 404!
        dir_x = db_path.parent / "issue-attachments" / "users" / str(uid_x)
        shutil.rmtree(dir_x)
        dir_x.symlink_to(db_path.parent / "issue-attachments" / "users" / str(uid_y))
        parent_sym = client.get(f"/api/issue-attachments/{att_x}", headers=headers_x)
        assert parent_sym.status_code == 404
        dir_x.unlink()

        # R7 simulated restart (clearing in-memory dedupe) re-records download audit!
        before_cnt = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        assert (
            client.get(f"/api/issue-attachments/{att_y}", headers=headers_y).status_code
            == 200
        )
        assert (
            client.get(f"/api/issue-attachments/{att_y}", headers=headers_y).status_code
            == 200
        )
        mid_cnt = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        assert mid_cnt == before_cnt + 1
        main._download_audit_dedupe.clear()
        assert (
            client.get(f"/api/issue-attachments/{att_y}", headers=headers_y).status_code
            == 200
        )
        after_cnt = len(
            (await storage.list_audit_events(action="issue_attachment.download"))[
                "events"
            ]
        )
        assert after_cnt == mid_cnt + 1

        # 5) R3 boot-time row-delete failure keeps marker and records survived=1; None/empty snapshots skip!
        uid_z, _, headers_z = await _create_reviewer("owner-z")
        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/99",
                    "number": 99,
                    "title": "T",
                }
            ),
        ):
            client.post(
                "/api/feedback/create",
                headers=headers_z,
                json={
                    "title": "TZ",
                    "body": "BZ",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("z.log", b"z-bytes")],
                },
            )
        with patch.object(
            storage,
            "delete_issue_attachment_rows_for_user",
            AsyncMock(side_effect=sqlite3.OperationalError("row fail")),
        ):
            assert (
                client.delete(
                    "/api/admin/users/owner-z", headers=admin_headers
                ).status_code
                == 200
            )
        marker_z = delete_marker(
            db_path.parent / "issue-attachments" / "users" / str(uid_z)
        )
        os.utime(marker_z, (1, 1))

        # None or empty owner_ids snapshot skips owner marker!
        assert await main._reclaim_abandoned_workspaces({"live"}, None) == 0
        assert await main._reclaim_abandoned_workspaces({"live"}, set()) == 0
        assert marker_z.exists()

        # Boot sweep with injected row-delete failure keeps marker and writes failure audit with survived>=1!
        with patch.object(
            storage,
            "delete_issue_attachment_rows_for_user",
            AsyncMock(side_effect=sqlite3.OperationalError("boot row fail")),
        ):
            assert (
                await main._reclaim_abandoned_workspaces(
                    await main._startup_job_ids(), await main._startup_owner_ids()
                )
                == 0
            )
        assert marker_z.exists()
        reclaim_events = (
            await storage.list_audit_events(action="startup.workspace_reclaim")
        )["events"]
        assert reclaim_events[-1]["outcome"] == "failure"
        assert reclaim_events[-1]["details"]["survived"] >= 1

        # Next boot without fault clears rows THEN marker in exact order!
        order: list[str] = []
        orig_del_rows = storage.delete_issue_attachment_rows_for_user
        orig_rm_marker = issue_attachments.remove_owner_delete_marker

        async def track_rows(oid: int) -> None:
            order.append("rows")
            await orig_del_rows(oid)

        async def track_marker(oid: int) -> None:
            order.append("marker")
            await orig_rm_marker(oid)

        with (
            patch.object(
                storage, "delete_issue_attachment_rows_for_user", side_effect=track_rows
            ),
            patch.object(
                issue_attachments,
                "remove_owner_delete_marker",
                side_effect=track_marker,
            ),
        ):
            assert (
                await main._reclaim_abandoned_workspaces(
                    await main._startup_job_ids(), await main._startup_owner_ids()
                )
                == 1
            )
        assert order == ["rows", "marker"]
        assert not marker_z.exists()

        # 6) Valid no-Content-Length ASGI request succeeds and /api/user/ai-credentials logging parity holds!
        valid_body = json.dumps(
            {"title": "Chunked", "body": "Chunked body", "labels": ["bug"]}
        ).encode()
        req_msgs = iter(
            [
                {"type": "http.request", "body": valid_body[:15], "more_body": True},
                {"type": "http.request", "body": valid_body[15:], "more_body": False},
            ]
        )
        downstream_bodies: list[bytes] = []

        async def capture_downstream(scope, receive, send):
            msg = await receive()
            downstream_bodies.append(msg["body"])
            await JSONResponse({"ok": True}, status_code=201)(scope, receive, send)

        sent_codes: list[int] = []

        async def capture_send(msg):
            if msg["type"] == "http.response.start":
                sent_codes.append(msg["status"])

        await main.AttachmentEnvelopeBoundMiddleware(capture_downstream)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/feedback/create",
                "raw_path": b"/api/feedback/create",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "state": {"username": "owner-y", "role": "reviewer", "is_admin": False},
            },
            lambda: asyncio.sleep(0, result=next(req_msgs)),
            capture_send,
        )
        assert sent_codes == [201]
        assert downstream_bodies == [valid_body]

        caplog.clear()
        with capture_main_logs(caplog, level=logging.DEBUG):
            ai_cred_resp = client.put(
                "/api/user/ai-credentials/openai",
                headers=headers_y,
                json={
                    "api_key": "sk-secret-ai-cred-value-999",  # pragma: allowlist secret
                },
            )
        assert ai_cred_resp.status_code == 422
        assert "Incoming PUT /api/user/ai-credentials" not in caplog.text
        assert "body=<skipped>" not in caplog.text
        assert "sk-secret-ai-cred-value-999" not in caplog.text

        # 7) Post-create bookkeeping failure preserves created issue URL and warns not to refile!
        with (
            patch(
                "rootcoz.main.create_github_issue",
                AsyncMock(
                    return_value={
                        "url": "https://github.com/myk-org/rootcoz/issues/555",
                        "number": 555,
                        "title": "T",
                    }
                ),
            ),
            patch.object(
                main, "FeedbackResponse", side_effect=RuntimeError("bookkeeping boom")
            ),
        ):
            bk = client.post(
                "/api/feedback/create",
                headers=headers_y,
                json={"title": "T", "body": "B", "labels": ["bug"]},
            )
        assert bk.status_code == 502
        assert "https://github.com/myk-org/rootcoz/issues/555" in bk.json()["detail"]
        assert "do not submit it again" in bk.json()["detail"]

    async def test_delete_issue_attachment_endpoint_frees_quota_and_audits(
        self, integration_env
    ) -> None:
        client, db_path = integration_env
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}
        uid_owner, _, owner_headers = await _create_reviewer("quota-del-owner")
        _, _, stranger_headers = await _create_reviewer("quota-del-stranger")

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/201",
                    "number": 201,
                    "title": "T",
                }
            ),
        ):
            c1 = client.post(
                "/api/feedback/create",
                headers=owner_headers,
                json={
                    "title": "FB1",
                    "body": "B1",
                    "labels": ["bug"],
                    "attachments": [
                        _upload_payload("private-evidence.log", b"1234567890")
                    ],
                },
            )
            c2 = client.post(
                "/api/feedback/create",
                headers=owner_headers,
                json={
                    "title": "FB2",
                    "body": "B2",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("second.log", b"abcdef")],
                },
            )
        att_1 = c1.json()["attachments"]["files"][0]["id"]
        att_2 = c2.json()["attachments"]["files"][0]["id"]

        # Inflate att_1's recorded size_bytes to hit the 500 MiB owner ceiling.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE issue_attachments SET size_bytes = ? WHERE id = ?",
                (500 * 1024 * 1024 - 6, att_1),
            )

        # Owner is now blocked by the 500 MiB ceiling!
        blocked = client.post(
            "/api/feedback/create",
            headers=owner_headers,
            json={
                "title": "Blocked",
                "body": "B",
                "labels": ["bug"],
                "attachments": [_upload_payload("next.log", b"new-bytes")],
            },
        )
        assert blocked.status_code == 400
        assert "rootcoz delete-attachment" in blocked.json()["detail"]

        # Unknown / invalid UUID returns 404 without writing an audit row.
        assert (
            client.delete(
                "/api/issue-attachments/not-a-uuid", headers=owner_headers
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/issue-attachments/{uuid4()}", headers=owner_headers
            ).status_code
            == 404
        )
        assert (await storage.list_audit_events(action="issue_attachment.delete"))[
            "events"
        ] == []

        # Stranger gets 404 (hiding attachment existence & filename) + denied audit row!
        stranger_del = client.delete(
            f"/api/issue-attachments/{att_1}", headers=stranger_headers
        )
        assert stranger_del.status_code == 404
        assert "private-evidence" not in stranger_del.text
        den_events = (
            await storage.list_audit_events(action="issue_attachment.delete")
        )["events"]
        assert len(den_events) == 1
        assert den_events[0]["outcome"] == "denied"
        assert "private-evidence" not in json.dumps(den_events[0])

        # Uploader deletes att_1 -> quota is freed immediately, so the new upload succeeds!
        del_resp = client.delete(
            f"/api/issue-attachments/{att_1}", headers=owner_headers
        )
        assert del_resp.status_code == 200
        assert del_resp.json() == {
            "status": "deleted",
            "attachment_id": att_1,
            "freed_bytes": 500 * 1024 * 1024 - 6,
        }
        assert await storage.get_issue_attachment(att_1) is None
        assert not (
            db_path.parent / "issue-attachments" / "users" / str(uid_owner) / att_1
        ).exists()

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/202",
                    "number": 202,
                    "title": "Unblocked",
                }
            ),
        ):
            unblocked = client.post(
                "/api/feedback/create",
                headers=owner_headers,
                json={
                    "title": "Unblocked",
                    "body": "B",
                    "labels": ["bug"],
                    "attachments": [_upload_payload("next.log", b"new-bytes")],
                },
            )
        assert unblocked.status_code == 201, unblocked.text

        # Admin can delete another user's attachment too.
        admin_del = client.delete(
            f"/api/issue-attachments/{att_2}", headers=admin_headers
        )
        assert admin_del.status_code == 200
        assert await storage.get_issue_attachment(att_2) is None
        all_del_events = (
            await storage.list_audit_events(action="issue_attachment.delete")
        )["events"]
        assert [e["outcome"] for e in all_del_events].count("success") == 2

    async def test_delete_issue_attachment_enforces_allow_list(
        self, integration_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, db_path = integration_env
        admin_headers = {"Authorization": "Bearer test-bootstrap-admin-key"}
        uid_excluded, _, excluded_headers = await _create_reviewer("excluded-uploader")
        uid_allowed, _, allowed_headers = await _create_reviewer("allowed-uploader")

        with patch(
            "rootcoz.main.create_github_issue",
            AsyncMock(
                return_value={
                    "url": "https://github.com/myk-org/rootcoz/issues/205",
                    "number": 205,
                    "title": "T",
                }
            ),
        ):
            c_excl = client.post(
                "/api/feedback/create",
                headers=excluded_headers,
                json={
                    "title": "FB Excl",
                    "body": "B1",
                    "labels": ["bug"],
                    "attachments": [
                        _upload_payload("confidential-trace.log", b"evidence-1")
                    ],
                },
            )
            c_allow = client.post(
                "/api/feedback/create",
                headers=allowed_headers,
                json={
                    "title": "FB Allow",
                    "body": "B2",
                    "labels": ["bug"],
                    "attachments": [
                        _upload_payload("allowed-trace.log", b"evidence-2")
                    ],
                },
            )
        assert c_excl.status_code == 201, c_excl.text
        assert c_allow.status_code == 201, c_allow.text
        att_excl = c_excl.json()["attachments"]["files"][0]["id"]
        att_allow = c_allow.json()["attachments"]["files"][0]["id"]
        blob_excl = (
            db_path.parent
            / "issue-attachments"
            / "users"
            / str(uid_excluded)
            / att_excl
        )
        blob_allow = (
            db_path.parent
            / "issue-attachments"
            / "users"
            / str(uid_allowed)
            / att_allow
        )

        # Restrict write allow-list to allowed-uploader only.
        monkeypatch.setenv("ALLOWED_USERS", "allowed-uploader")
        clear_settings_cache()

        # Non-admin excluded from ALLOWED_USERS gets 403 + denied audit row; row/blob untouched.
        denied = client.delete(
            f"/api/issue-attachments/{att_excl}", headers=excluded_headers
        )
        assert denied.status_code == 403
        assert "confidential-trace" not in denied.text
        assert await storage.get_issue_attachment(att_excl) is not None
        assert blob_excl.is_file()

        events = (await storage.list_audit_events(action="issue_attachment.delete"))[
            "events"
        ]
        assert len(events) == 1
        assert events[0]["actor"] == "excluded-uploader"
        assert events[0]["outcome"] == "denied"
        assert events[0]["target_type"] == "issue_attachment"
        assert events[0]["target_id"] == att_excl
        assert events[0]["details"] == {
            "reason": "forbidden",
            "attachment_id": att_excl,
        }
        assert "confidential-trace" not in json.dumps(events[0])

        # Invalid/secret-shaped ID on allow-list refusal never leaks into response or audit row.
        bad_seg = "ghp_" + ("x" * 36)
        denied_bad = client.delete(
            f"/api/issue-attachments/{bad_seg}", headers=excluded_headers
        )
        assert denied_bad.status_code == 403
        assert bad_seg not in denied_bad.text
        events_after_bad = (
            await storage.list_audit_events(action="issue_attachment.delete")
        )["events"]
        assert len(events_after_bad) == 2
        bad_event = next(e for e in events_after_bad if e["target_id"] is None)
        assert bad_event["outcome"] == "denied"
        assert bad_event["details"] == {"reason": "forbidden"}
        assert bad_seg not in json.dumps(bad_event)

        # Allowed uploader succeeds (200) and frees row + blob.
        ok_allowed = client.delete(
            f"/api/issue-attachments/{att_allow}", headers=allowed_headers
        )
        assert ok_allowed.status_code == 200
        assert await storage.get_issue_attachment(att_allow) is None
        assert not blob_allow.exists()

        # Admin bypasses ALLOWED_USERS automatically and succeeds (200).
        ok_admin = client.delete(
            f"/api/issue-attachments/{att_excl}", headers=admin_headers
        )
        assert ok_admin.status_code == 200
        assert await storage.get_issue_attachment(att_excl) is None
        assert not blob_excl.exists()

    async def test_slow_tracker_does_not_stall_job_or_user_deletion(
        self, integration_env
    ) -> None:
        _, db_path = integration_env
        uid_slow, _, _ = await _create_reviewer("slow-tracker-user", role="operator")
        job_id = await _seed_completed_job(submitter="slow-tracker-user")
        cfg = get_settings()

        # 1) Job deletion completes immediately while slow report issue create is in flight!
        in_tracker_job = asyncio.Event()
        release_tracker_job = asyncio.Event()

        async def slow_github_job(**kwargs):
            in_tracker_job.set()
            await release_tracker_job.wait()
            return {
                "url": "https://github.com/org/tests-repo/issues/301",
                "number": 301,
                "title": kwargs["title"],
            }

        with patch("rootcoz.main.create_github_issue", side_effect=slow_github_job):
            create_task = asyncio.create_task(
                main._create_issue_from_text(
                    "github",
                    "Slow Job Issue",
                    "Body",
                    username="slow-tracker-user",
                    settings=cfg,
                    github_token="ghp_tok",
                    github_repo_url="https://github.com/org/tests-repo",
                    job_id=job_id,
                    attachments=[
                        IssueAttachmentUpload(
                            filename="slow.log", data_b64=_b64(b"slow-bytes")
                        )
                    ],
                )
            )
            await asyncio.wait_for(in_tracker_job.wait(), timeout=2.0)

            # While slow_github_job is still blocked, job deletion under _hold_attachment_run_locks
            # finishes immediately without waiting on release_tracker_job!
            origins = await main._reanalysis_origins([job_id])

            async def delete_job_now() -> None:
                async with main._hold_attachment_run_locks([job_id]):
                    deleted = [job_id] if await storage.delete_job(job_id) else []
                    await main._finish_job_deletion([job_id], deleted, origins)

            await asyncio.wait_for(delete_job_now(), timeout=1.0)
            assert await storage.get_result(job_id) is None
            assert not (db_path.parent / "issue-attachments" / "jobs" / job_id).exists()

            release_tracker_job.set()
            created = await asyncio.wait_for(create_task, timeout=2.0)
            assert created["url"] == "https://github.com/org/tests-repo/issues/301"
            assert (
                await storage.get_issue_attachment(
                    created["attachments"]["files"][0]["id"]
                )
                is None
            )

        # 2) User deletion completes immediately while slow run-less create is in flight!
        in_tracker_user = asyncio.Event()
        release_tracker_user = asyncio.Event()

        async def slow_github_user(**kwargs):
            in_tracker_user.set()
            await release_tracker_user.wait()
            req = httpx.Request(
                "POST", "https://api.github.com/repos/myk-org/rootcoz/issues"
            )
            raise httpx.HTTPStatusError(
                "503", request=req, response=httpx.Response(503, request=req)
            )

        with patch("rootcoz.main.create_github_issue", side_effect=slow_github_user):
            user_create_task = asyncio.create_task(
                main._create_issue_from_text(
                    "github",
                    "Slow Feedback",
                    "Body",
                    username="slow-tracker-user",
                    settings=cfg,
                    github_token="ghp_tok",
                    github_repo_url=cfg.feedback_repo_url,
                    attachments=[
                        IssueAttachmentUpload(
                            filename="fb-slow.log", data_b64=_b64(b"fb-slow")
                        )
                    ],
                )
            )
            await asyncio.wait_for(in_tracker_user.wait(), timeout=2.0)

            async def delete_user_now() -> None:
                async with main._hold_attachment_scope_locks(f"owner:{uid_slow}"):
                    await issue_attachments.write_owner_delete_marker(uid_slow)
                    assert await storage.delete_user("slow-tracker-user")
                    assert await main._cleanup_deleted_owner_attachments(uid_slow)

            await asyncio.wait_for(delete_user_now(), timeout=1.0)
            assert not await storage.user_id_exists(uid_slow)
            assert not (
                db_path.parent / "issue-attachments" / "users" / str(uid_slow)
            ).exists()

            release_tracker_user.set()
            with pytest.raises(main.HTTPException) as exc_info:
                await asyncio.wait_for(user_create_task, timeout=2.0)
            assert exc_info.value.status_code == 502
            assert not (
                db_path.parent / "issue-attachments" / "users" / str(uid_slow)
            ).exists()
