"""Per-user AI credential storage and sidecar boundary checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.ai_client import call_ai as real_call_ai
from rootcoz.engine import chat


@pytest.mark.asyncio
async def test_credential_map_encrypted_atomic_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "credentials.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.create_admin_user("bob")
    await storage.update_user_ai_credential("alice", "provider-a", "secret-alpha")
    await storage.update_user_ai_credential("alice", "provider-b", "secret-beta")
    assert await storage.get_user_ai_credentials("alice") == {
        "provider-a": "secret-alpha",
        "provider-b": "secret-beta",
    }
    assert await storage.get_user_ai_credentials("bob") == {}
    assert b"secret-alpha" not in (tmp_path / "credentials.db").read_bytes()
    await storage.update_user_ai_credential("alice", "provider-a", None)
    assert await storage.get_user_ai_credentials("alice") == {
        "provider-b": "secret-beta"
    }


@pytest.mark.asyncio
async def test_capability_fails_closed_and_exact_key(monkeypatch):
    monkeypatch.setattr(
        ai_client,
        "list_models",
        AsyncMock(
            return_value=[
                {"provider": "unknown", "id": "m"},
                {"provider": "supported", "id": "m"},
            ]
        ),
    )
    client = AsyncMock()
    client.get_model_provider_status.side_effect = lambda p: (
        {"supportsSessionApiKey": p == "supported"}
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    assert await ai_client.supported_key_providers() == ["supported"]
    client.get_model_provider_status.side_effect = lambda p: {}
    assert await ai_client.supported_key_providers() == []

    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"supported": "wrong-key"}),
    )
    token = ai_client.ai_username.set("alice")
    try:
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("supported")
        assert await ai_client.session_key("unknown") is None
    finally:
        ai_client.ai_username.reset(token)
    assert await ai_client.session_key("supported") is None


@pytest.mark.asyncio
async def test_api_status_redacts_and_rejects_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    monkeypatch.setattr(
        main, "supported_key_providers", AsyncMock(return_value=["custom"])
    )
    request = SimpleNamespace(state=SimpleNamespace(username="alice"))
    body = main.AiCredentialInput(api_key="private-value")
    response = await main.set_user_ai_credential("custom", body, request)
    assert response.headers["cache-control"] == "no-store"
    status = await main.get_user_ai_credentials(request)
    assert status.body == b'{"providers":[{"provider":"custom","configured":true}]}'
    assert b"private-value" not in status.body
    with pytest.raises(HTTPException) as exc:
        await main.set_user_ai_credential("unsupported", body, request)
    assert exc.value.status_code == 400
    await main.delete_user_ai_credential("custom", request)
    assert (
        await main.get_user_ai_credentials(request)
    ).body == b'{"providers":[{"provider":"custom","configured":false}]}'


@pytest.mark.asyncio
async def test_call_ai_uses_key_only_for_new_sessions(monkeypatch):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="secret"))
    sidecar_call = AsyncMock(return_value=object())
    monkeypatch.setattr(ai_client, "_call_ai", sidecar_call)
    await real_call_ai("first", ai_provider="p", ai_model="m", session_id=None)
    expected_key = "secret"  # pragma: allowlist secret
    assert sidecar_call.await_args.kwargs["api_key"] == expected_key
    await real_call_ai("second", ai_provider="p", ai_model="m", session_id="sid")
    assert "api_key" not in sidecar_call.await_args.kwargs


@pytest.mark.asyncio
async def test_rotating_credential_deletes_sidecar_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "revoke.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.add_chat_message(
        job_id="job",
        role="assistant",
        content="hi",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(main, "supported_key_providers", AsyncMock(return_value=["p"]))
    sidecar = AsyncMock()
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    request = SimpleNamespace(state=SimpleNamespace(username="alice"))
    new_key = "new"  # pragma: allowlist secret
    await main.set_user_ai_credential(
        "p", main.AiCredentialInput(api_key=new_key), request
    )
    sidecar.delete_session.assert_awaited_once_with("sid")


@pytest.mark.asyncio
async def test_invalid_user_key_does_not_fall_back_to_server(monkeypatch):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="invalid"))
    sidecar_call = AsyncMock(side_effect=ValueError("invalid key"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar_call)
    with pytest.raises(ValueError, match="invalid key"):
        await real_call_ai("prompt", ai_provider="p", ai_model="m")
    sidecar_call.assert_awaited_once()
    expected_key = "invalid"  # pragma: allowlist secret
    assert sidecar_call.await_args.kwargs["api_key"] == expected_key


@pytest.mark.asyncio
async def test_chat_session_creation_and_resumed_turn(monkeypatch):
    client = AsyncMock()
    client.create_session.return_value = "sid"
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="secret"))
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    assert (
        await chat._create_chat_session(
            system_prompt="system", ai_provider="p", ai_model="m"
        )
        == "sid"
    )
    expected_key = "secret"  # pragma: allowlist secret
    assert client.create_session.await_args.kwargs["api_key"] == expected_key

    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar_call = AsyncMock()

    async def respond(*args, **kwargs):
        assert kwargs["session_id"] == "sid"
        assert "api_key" not in kwargs
        return SimpleNamespace(
            success=True, text="reply", session_id="sid", record_usage=AsyncMock()
        )

    sidecar_call.side_effect = respond
    monkeypatch.setattr(ai_client, "_call_ai", sidecar_call)
    monkeypatch.setattr(chat, "call_ai", real_call_ai)
    ok, text, sid = await chat._chat_with_ai_impl(
        message="next",
        history=[],
        ai_provider="p",
        ai_model="m",
        build_prompt_fn=lambda: "system",
        session_id="sid",
        install_mcp=False,
    )
    assert (ok, text, sid) == (True, "reply", "sid")


@pytest.mark.asyncio
async def test_rotating_key_invalidates_only_owners_provider_sessions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.create_admin_user("bob")
    for user, provider in (("alice", "p"), ("alice", "other"), ("bob", "p")):
        await storage.add_chat_message(
            job_id="job",
            role="assistant",
            content="hi",
            username=user,
            ai_provider=provider,
            ai_model="m",
            session_id="sid",
        )
    revoked = await storage.update_user_ai_credential("alice", "p", "new")
    assert revoked == ["sid"]
    assert [
        m["session_id"]
        for m in await storage.get_chat_messages("job", username="alice")
    ] == ["", "sid"]
    assert (await storage.get_chat_messages("job", username="bob"))[0][
        "session_id"
    ] == "sid"
    assert await storage.update_user_ai_credential("alice", "other", None) == ["sid"]
    assert all(
        not m["session_id"]
        for m in await storage.get_chat_messages("job", username="alice")
    )


@pytest.mark.asyncio
async def test_bootstrap_admin_gets_explicit_unavailable(monkeypatch):
    monkeypatch.setattr(storage, "get_user_by_username", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await main.get_user_ai_credentials(
            SimpleNamespace(state=SimpleNamespace(username="bootstrap"))
        )
    assert exc.value.status_code == 403
    assert "database" in exc.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,generator",
    [
        ("preview_github_issue", "generate_github_issue_content"),
        ("preview_jira_bug", "generate_jira_bug_content"),
    ],
)
async def test_preview_binds_user_for_content_and_resets(
    endpoint, generator, monkeypatch
):
    monkeypatch.setattr(main, "_check_allow_list", lambda request: None)
    monkeypatch.setattr(
        main, "_load_effective_failure", AsyncMock(return_value=(object(), {}, None))
    )
    monkeypatch.setattr(main, "_build_report_context", lambda **kwargs: ("", ""))
    monkeypatch.setattr(main, "_resolve_github_repo_url", lambda settings: "")
    monkeypatch.setattr(main, "_jira_issue_creation_enabled", lambda settings: True)

    async def generate(**kwargs):
        assert ai_client.ai_username.get() == "alice"
        return {"title": "test", "body": "test"}

    monkeypatch.setattr(main, generator, generate)
    settings = SimpleNamespace(
        enable_github_issues=True,
        jira_url="https://jira",
        jira_project_key="",
        jira_api_token=None,
        jira_pat=None,
    )
    body = main.PreviewIssueRequest(test_name="failure")
    request = SimpleNamespace(state=SimpleNamespace(username="alice"))
    token = ai_client.ai_username.set("prior")
    try:
        result = await getattr(main, endpoint)("job", body, request, settings=settings)
        assert result["title"] == "test"
        assert ai_client.ai_username.get() == "prior"
    finally:
        ai_client.ai_username.reset(token)


def test_provider_with_encoded_slash_routes():
    routes = [
        r
        for r in main.app.routes
        if getattr(r, "path", "").startswith("/api/user/ai-credentials/")
    ]
    assert all("{provider:path}" in r.path for r in routes)
    assert all(
        r.path_regex.match("/api/user/ai-credentials/openai%2Fcustom")
        or r.path_regex.match("/api/user/ai-credentials/openai/custom")
        for r in routes
    )
