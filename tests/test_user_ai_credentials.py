"""Per-user AI credential storage and sidecar boundary checks."""

import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pi_sidecar_client
import pytest
from fastapi import HTTPException
from pi_sidecar_client import AIResult, SidecarClient
from pi_sidecar_client import call_ai as sidecar_real_call_ai

from rootcoz import ai_client, encryption, issue_matching, main, storage
from rootcoz.ai_client import call_ai as real_call_ai
from rootcoz.ai_client import call_ai_once as real_call_ai_once
from rootcoz.engine import chat


@pytest.fixture(autouse=True)
async def session_provenance_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "provenance.db")
    await storage.init_db()
    await storage.save_ai_session_source("existing", "alice", "p", "user")


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
    monkeypatch.setattr(ai_client, "list_models", AsyncMock(return_value=[]))
    client = AsyncMock()
    client.get_providers.return_value = [
        {"provider": "unknown", "supportsSessionApiKey": False},
        {"provider": "supported", "supportsSessionApiKey": True},
        {"provider": "cli-hidden", "supportsSessionApiKey": False},
    ]
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    assert await ai_client.supported_key_providers() == ["supported"]
    client.get_providers.return_value = [
        {"provider": "supported", "supportsSessionApiKey": False}
    ]
    assert await ai_client.supported_key_providers() == []

    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(
            return_value={
                "supported": "wrong-key",
                "unknown": "bad-key",
                "unregistered": "bad-key",
            }
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("supported")
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("unknown")
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("unregistered")
    finally:
        ai_client.ai_username.reset(token)
    assert await ai_client.session_key("supported") is None


@pytest.mark.asyncio
async def test_key_lookup_checks_only_selected_provider(monkeypatch):
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"acpx/a": "key"})
    )
    client = AsyncMock()
    client.get_providers.return_value = [
        {"provider": "acpx/a", "supportsSessionApiKey": True},
        {"provider": "other", "supportsSessionApiKey": False},
    ]
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "list_models", AsyncMock(return_value=[]))
    token = ai_client.ai_username.set("alice")
    try:
        assert await ai_client.session_key("acpx/a") == "key"
    finally:
        ai_client.ai_username.reset(token)
    client.get_providers.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cli_providers_follow_sidecar_capability(monkeypatch):
    client = AsyncMock()
    client.get_providers.return_value = [
        {"provider": "cli-keyed", "supportsSessionApiKey": True},
        {"provider": "cli-ambient", "supportsSessionApiKey": False},
    ]
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"cli-keyed": "key", "cli-ambient": "other"}),
    )
    assert await ai_client.supported_key_providers() == ["cli-keyed"]
    token = ai_client.ai_username.set("alice")
    try:
        assert await ai_client.session_key("cli-keyed") == "key"
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("cli-ambient")
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_discovery_failure_fails_closed(monkeypatch):
    client = AsyncMock()
    client.get_providers.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "key"})
    )
    assert await ai_client.supported_key_providers() == []
    token = ai_client.ai_username.set("alice")
    try:
        with pytest.raises(ValueError, match="capability unavailable"):
            await ai_client.session_key("openai")
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_api_status_redacts_and_rejects_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    monkeypatch.setattr(
        main, "supported_key_providers", AsyncMock(return_value=["custom"])
    )
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
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
async def test_openai_key_without_server_auth_or_catalog_models(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "openai.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    client = AsyncMock()
    client.get_providers.return_value = [
        {"provider": "openai", "supportsSessionApiKey": True}
    ]
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "list_models", AsyncMock(return_value=[]))
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
    assert (await main.get_user_ai_credentials(request)).body == (
        b'{"providers":[{"provider":"openai","configured":false}]}'
    )
    await main.set_user_ai_credential(
        "openai",
        main.AiCredentialInput(api_key="user-key"),  # pragma: allowlist secret
        request,
    )
    assert (await main.get_user_ai_credentials(request)).body == (
        b'{"providers":[{"provider":"openai","configured":true}]}'
    )
    token = ai_client.ai_username.set("alice")
    try:
        assert (
            await ai_client.session_key("openai") == "user-key"
        )  # pragma: allowlist secret
    finally:
        ai_client.ai_username.reset(token)
    ai_client.list_models.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar_name", ["_call_ai", "_call_ai_once"])
@pytest.mark.parametrize("session_id", [None, "existing"])
async def test_sidecar_exception_never_exposes_user_key(
    monkeypatch, caplog, sidecar_name, session_id
):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(
        ai_client, "session_key", AsyncMock(return_value="secret-value")
    )

    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"p": "secret-value"}),
    )
    token = ai_client.ai_username.set("alice")

    async def fail(*args, **kwargs):
        raise RuntimeError("sidecar secret-value failed")

    monkeypatch.setattr(ai_client, "_call_user_session", fail)
    with caplog.at_level(logging.ERROR):
        try:
            await (real_call_ai if sidecar_name == "_call_ai" else real_call_ai_once)(
                "prompt", ai_provider="p", ai_model="m", session_id=session_id
            )
        except Exception as exc:
            assert exc.__context__ is None
            logging.getLogger(__name__).exception("Caller logged sidecar failure")
            import traceback

            rendered = traceback.format_exc()
    assert "secret-value" not in rendered + caplog.text
    assert "RuntimeError" in rendered
    ai_client.ai_username.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar_name", ["_call_ai", "_call_ai_once"])
@pytest.mark.parametrize("session_id", [None, "existing"])
@pytest.mark.asyncio
async def test_failed_sidecar_result_redacted_before_chat_logging(
    monkeypatch, caplog, sidecar_name, session_id
):
    secret = "user-test-key"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    original = AIResult(
        success=False, text=f"sidecar rejected {secret}", error=f"error: {secret}"
    )
    monkeypatch.setattr(
        ai_client, "_call_user_session", AsyncMock(return_value=original)
    )
    token = ai_client.ai_username.set("alice")
    try:
        with caplog.at_level(logging.ERROR):
            result = await (
                real_call_ai if sidecar_name == "_call_ai" else real_call_ai_once
            )("prompt", ai_provider="p", ai_model="m", session_id=session_id)
            logging.getLogger(__name__).error(
                "AI failure: %s / %s", result.text, result.error
            )
        assert not result.success
        assert secret not in result.text + (result.error or "") + caplog.text
        assert result.text and result.error
        assert original.text == f"sidecar rejected {secret}"
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "existing"])
@pytest.mark.parametrize("failure", ["response", "exception"])
async def test_real_sidecar_http_error_never_logs_user_key(
    monkeypatch, caplog, capsys, session_id, failure
):
    secret = "rotated-secret-test"  # pragma: allowlist secret
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            if failure == "exception":
                raise RuntimeError(f"HTTP failure: {secret}")
            return httpx.Response(400, json={"error": f"sidecar rejected {secret}"})
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(pi_sidecar_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    monkeypatch.setattr(ai_client, "_call_ai", pi_sidecar_client.call_ai)
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    token = ai_client.ai_username.set("alice")
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai(
                "prompt", ai_provider="p", ai_model="m", session_id=session_id
            )
            assert not result.success
            logging.getLogger(__name__).error("AI failed: %s", result.text)
        assert secret not in caplog.text + capsys.readouterr().err + result.text + (
            result.error or ""
        )
        if failure == "response":
            assert result.text == "AI call failed"
        assert len([r for r in requests if r.url.path == "/sessions"]) == (
            0 if session_id else 1
        )
        if session_id:
            assert all(b"api_key" not in r.content for r in requests)
            assert [r.method for r in requests] == ["POST"]
        else:
            assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
    finally:
        ai_client.ai_username.reset(token)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail, expected",
    [
        ("session not found: review%2Fkey", "session not found"),
        ("not authenticated: review%2Fkey", "authentication required"),
    ],
)
async def test_keyed_failure_preserves_safe_retry_and_auth_signal(
    monkeypatch, detail, expected
):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="review/key"))
    monkeypatch.setattr(
        ai_client,
        "_call_user_session",
        AsyncMock(return_value=AIResult(success=False, text=detail, error=detail)),
    )
    result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
    assert result.text == result.error == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", ["url", "json", "base64"])
async def test_real_sidecar_encoded_prompt_error_never_logs_user_key(
    monkeypatch, caplog, encoded
):
    import base64

    secret = "review/key"  # pragma: allowlist secret
    variants = {
        "url": quote(secret, safe=""),
        "json": r"review\/key",
        "base64": base64.b64encode(secret.encode()).decode(),
    }

    def handle(request):
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(400, json={"error": f"rejected {variants[encoded]}"})
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
            logging.getLogger(__name__).error(
                "AI failed: %s / %s", result.text, result.error
            )
        assert not result.success
        assert result.text == result.error == "AI call failed"
        assert secret not in caplog.text
        assert variants[encoded] not in caplog.text + result.text + result.error
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_initial_creation_encoded_error_never_logs_user_key(
    monkeypatch, caplog
):
    secret = "review/key"  # pragma: allowlist secret
    encoded = base64.b64encode(secret.encode()).decode()

    def handle(request):
        assert request.url.path == "/sessions"
        assert request.content and secret.encode() in request.content
        return httpx.Response(400, json={"error": f"rejected {encoded}"})

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    try:
        with caplog.at_level(logging.DEBUG):
            assert (
                await chat._create_chat_session(
                    system_prompt="system", ai_provider="p", ai_model="m"
                )
                is None
            )
        assert encoded not in caplog.text
        assert secret not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "existing"])
async def test_keyed_prompt_empty_text_usage_never_logs_encoded_key(
    monkeypatch, caplog, session_id
):
    secret = "review/key"  # pragma: allowlist secret
    encoded = base64.b64encode(secret.encode()).decode()
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(
                200, json={"text": "", "usage": {"input_tokens": 7, "note": encoded}}
            )
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    token = ai_client.ai_username.set("alice")
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai(
                "prompt", ai_provider="p", ai_model="m", session_id=session_id
            )
        assert result.success and result.text == ""
        assert result.usage.input_tokens == 7
        assert result.session_id == (session_id or "created")
        assert len([r for r in requests if r.url.path == "/sessions"]) == (
            0 if session_id else 1
        )
        assert encoded not in caplog.text
        assert secret not in caplog.text
    finally:
        ai_client.ai_username.reset(token)
        await client.close()


@pytest.mark.asyncio
async def test_keyed_prompt_http_200_error_keeps_usage_without_logging_response(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "error.db")
    await storage.init_db()
    await storage.save_ai_session_source("existing", "alice", "p", "user")
    secret = "review/key"  # pragma: allowlist secret
    encoded = base64.b64encode(secret.encode()).decode()

    def handle(request):
        assert request.url.path == "/sessions/existing/prompt"
        return httpx.Response(
            200,
            json={
                "text": "",
                "error": f"authentication required: {encoded}",
                "usage": {"output_tokens": 3, "note": encoded},
            },
        )

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    token = ai_client.ai_username.set("alice")
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai(
                "prompt", ai_provider="p", ai_model="m", session_id="existing"
            )
        assert (result.success, result.text, result.error, result.session_id) == (
            False,
            "authentication required",
            "authentication required",
            "existing",
        )
        assert result.usage.output_tokens == 3
        assert encoded not in caplog.text
    finally:
        ai_client.ai_username.reset(token)
        await client.close()


@pytest.mark.asyncio
async def test_real_sidecar_creation_encoded_error_never_logs_user_key(
    monkeypatch, caplog
):
    import base64

    secret = "review/key"  # pragma: allowlist secret
    encoded = base64.b64encode(secret.encode()).decode()

    def handle(request):
        return httpx.Response(400, json={"error": f"rejected {encoded}"})

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
        assert not result.success
        assert result.text == result.error == "AI call failed"
        assert encoded not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "exception"])
async def test_real_sidecar_creation_error_never_logs_user_key(
    monkeypatch, caplog, failure
):
    secret = "new-user-secret"  # pragma: allowlist secret

    def handle(request):
        if failure == "exception":
            raise RuntimeError(f"session failed: {secret}")
        return httpx.Response(400, json={"error": f"session failed: {secret}"})

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
            logging.getLogger(__name__).error(
                "AI failed: %s / %s", result.text, result.error
            )
        assert (result.success, result.text, result.error, result.session_id) == (
            False,
            "AI call failed",
            "AI call failed",
            None,
        )
        assert secret not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_sidecar_keyed_once_preserves_usage_and_cleans_up(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            assert (
                b'"api_key":"user-key"' in request.content  # pragma: allowlist secret
            )
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(
                200,
                json={"text": "reply", "usage": {"input_tokens": 7}},
            )
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="user-key"))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    try:
        result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
        assert (result.success, result.text, result.session_id) == (True, "reply", None)
        assert result.usage.input_tokens == 7
        assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_sidecar_wrapper_logs_raw_resumed_exception(monkeypatch, caplog):
    secret = "rotated-secret-test"  # pragma: allowlist secret

    def handle(request):
        raise RuntimeError(f"HTTP failure: {secret}")

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(pi_sidecar_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(pi_sidecar_client.logger, "propagate", True)
    try:
        with caplog.at_level(logging.ERROR):
            await sidecar_real_call_ai("prompt", session_id="existing")
        assert secret in caplog.text  # raw wrapper is unsafe for resumed sessions
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_issue_filter_result_does_not_log_key(monkeypatch):
    secret = "user-test-key"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client,
        "_call_user_session",
        AsyncMock(
            return_value=AIResult(
                success=False,
                text=f"sidecar rejected {secret}",
                error=f"error: {secret}",
            )
        ),
    )
    monkeypatch.setattr(issue_matching, "call_ai_once", real_call_ai_once)
    warnings = []
    monkeypatch.setattr(
        issue_matching.logger, "warning", lambda fmt, *args: warnings.append(fmt % args)
    )
    token = ai_client.ai_username.set("alice")
    try:
        assert (
            await issue_matching.filter_issue_matches_with_ai(
                "bug", "description", [{"key": "TEST-1"}], "p", "m"
            )
            == []
        )
        assert secret not in str(warnings)
        assert "AI call failed" in str(warnings)
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_resumed_chat_failure_hides_rotated_key_from_log_and_response(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "echo.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "p", "user")
    old_key = "rotated-user-key"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(
        ai_client,
        "_call_user_session",
        AsyncMock(
            return_value=AIResult(
                success=False,
                text=f"sidecar rejected {old_key}",
                error=f"error: {old_key}",
            )
        ),
    )
    monkeypatch.setattr(chat, "call_ai", real_call_ai)
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=old_key))
    token = ai_client.ai_username.set("alice")
    try:
        with caplog.at_level(logging.ERROR):
            ok, text, sid = await chat._chat_with_ai_impl(
                message="next",
                history=[],
                ai_provider="p",
                ai_model="m",
                build_prompt_fn=lambda: "system",
                session_id="sid",
                install_mcp=False,
            )
        assert (ok, text, sid) == (False, "AI call failed", None)
        assert old_key not in caplog.text
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_resumed_success_redacts_current_key_and_denies_rotated_key(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "resume-echo.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    old = "old-resume-key"  # pragma: allowlist secret
    await storage.update_user_ai_credential("alice", "p", old)
    await storage.save_ai_session_source("sid", "alice", "p", "user")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=old))
    prompt = AsyncMock(return_value=AIResult(success=True, text=f"echo {old}"))
    monkeypatch.setattr(ai_client, "_call_user_session", prompt)
    token = ai_client.ai_username.set("alice")
    try:
        result = await real_call_ai(
            "next", ai_provider="p", ai_model="m", session_id="sid"
        )
        assert result.text == "echo [REDACTED]"
        await storage.update_user_ai_credential("alice", "p", "new-resume-key")
        denied = await real_call_ai(
            "next", ai_provider="p", ai_model="m", session_id="sid"
        )
        assert not denied.success
        assert old not in denied.text
        assert prompt.await_count == 1
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_resumed_chat_lost_session_still_retries(monkeypatch):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    sidecar = AsyncMock(
        side_effect=[
            AIResult(success=False, text="Session not found: old key"),
            AIResult(success=True, text="reply", session_id="new"),
        ]
    )
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar)
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    monkeypatch.setattr(chat, "call_ai", real_call_ai)
    token = ai_client.ai_username.set("alice")
    try:
        ok, text, sid = await chat._chat_with_ai_impl(
            message="next",
            history=[],
            ai_provider="p",
            ai_model="m",
            build_prompt_fn=lambda: "system",
            session_id="sid",
            install_mcp=False,
        )
        assert (ok, text, sid) == (True, "reply", "new")
        assert sidecar.await_count == 2
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_successful_sidecar_result_keeps_analysis_text(monkeypatch):
    secret = "user-test-key"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    original = AIResult(success=True, text=f"analysis mentions {secret}")
    monkeypatch.setattr(
        ai_client, "_call_user_session", AsyncMock(return_value=original)
    )
    result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
    assert result.text == "analysis mentions [REDACTED]"
    assert original.text == f"analysis mentions {secret}"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["raw", "url", "base64", "urlsafe", "json"])
async def test_real_sidecar_success_echo_is_not_logged_or_persisted(
    monkeypatch, tmp_path, caplog, encoding
):
    secret = "review/key+é"  # pragma: allowlist secret
    encoded = {
        "raw": secret,
        "url": quote(secret, safe=""),
        "base64": base64.b64encode(secret.encode()).decode(),
        "urlsafe": base64.urlsafe_b64encode(secret.encode()).decode(),
        "json": "review\\/key+\\u00e9",
    }[encoding]
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"text": f"analysis: {encoded} done"})
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
            logging.getLogger(__name__).info("Analysis: %s", result.text)
        stored = tmp_path / "analysis.txt"
        stored.write_text(result.text)
        assert result.success
        assert result.text == "analysis: [REDACTED] done"
        assert encoded not in caplog.text + stored.read_text()
        assert secret not in caplog.text + stored.read_text()
        assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_sidecar_failed_new_chat_prompt_deletes_session(monkeypatch, caplog):
    secret = "user-test-key"  # pragma: allowlist secret
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(400, json={"error": f"rejected {secret}"})
        return httpx.Response(204)

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(chat, "call_ai", real_call_ai)
    try:
        with caplog.at_level(logging.DEBUG):
            ok, text, sid = await chat._chat_with_ai_impl(
                message="hello",
                history=[],
                ai_provider="p",
                ai_model="m",
                build_prompt_fn=lambda: "system",
                install_mcp=False,
            )
        assert (ok, text, sid) == (False, "AI call failed", None)
        assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
        assert secret not in caplog.text + text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_prompt_cleanup_error_keeps_session_without_logging(
    monkeypatch, caplog
):
    secret = "user-test-key"  # pragma: allowlist secret
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "created"})
        if request.method == "DELETE":
            raise RuntimeError(f"cleanup failed: {secret}")
        return httpx.Response(400, json={"error": "prompt failed"})

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    try:
        with caplog.at_level(logging.DEBUG):
            result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert not result.success
        assert result.session_id == "created"
        assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
        assert secret not in caplog.text + result.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_credential_mutations_require_reviewer(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "roles.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    monkeypatch.setattr(main, "supported_key_providers", AsyncMock(return_value=["p"]))
    for role in ("viewer", "reviewer", "operator", "admin"):
        request = SimpleNamespace(state=SimpleNamespace(username="alice", role=role))
        await main.get_user_ai_credentials(request)
        for mutation in (
            lambda request=request: main.set_user_ai_credential(
                "p",
                main.AiCredentialInput(api_key="value"),  # pragma: allowlist secret
                request,
            ),
            lambda request=request: main.delete_user_ai_credential("p", request),
        ):
            if role == "viewer":
                with pytest.raises(HTTPException) as exc:
                    await mutation()
                assert exc.value.status_code == 403
            else:
                assert (await mutation()).status_code == 200


async def test_call_ai_uses_key_only_for_new_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "resume.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "p", "user")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="secret"))
    sidecar_call = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar_call)
    token = ai_client.ai_username.set("alice")
    try:
        await real_call_ai("first", ai_provider="p", ai_model="m", session_id=None)
        expected_key = "secret"  # pragma: allowlist secret
        assert sidecar_call.await_args.kwargs["api_key"] == expected_key
        await real_call_ai("second", ai_provider="p", ai_model="m", session_id="sid")
        assert "api_key" not in sidecar_call.await_args.kwargs
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_rotating_credential_deletes_sidecar_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "revoke.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old")
    await storage.save_ai_session_source("sid", "alice", "p", "user")
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
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
    new_key = "new"  # pragma: allowlist secret
    await main.set_user_ai_credential(
        "p", main.AiCredentialInput(api_key=new_key), request
    )
    sidecar.delete_session.assert_awaited_once_with("sid")
    assert (await storage.get_chat_messages("job", username="alice"))[0][
        "session_id"
    ] == ""


@pytest.mark.asyncio
async def test_invalid_user_key_does_not_fall_back_to_server(monkeypatch):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="invalid"))
    sidecar_call = AsyncMock(side_effect=ValueError("invalid key"))
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar_call)
    with pytest.raises(ValueError, match="ValueError: sidecar call failed"):
        await real_call_ai("prompt", ai_provider="p", ai_model="m")
    sidecar_call.assert_awaited_once()
    expected_key = "invalid"  # pragma: allowlist secret
    assert sidecar_call.await_args.kwargs["api_key"] == expected_key


@pytest.mark.asyncio
async def test_chat_session_creation_and_resumed_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"session_id": "sid"})

    client = SidecarClient(base_url="http://sidecar.invalid")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="secret"))
    ai_client._selected_credential_source.set("user")
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    assert (
        await chat._create_chat_session(
            system_prompt="system", ai_provider="p", ai_model="m"
        )
        == "sid"
    )
    expected_key = "secret"  # pragma: allowlist secret
    assert expected_key.encode() in requests[0].content

    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar_call = AsyncMock()

    async def respond(*args, **kwargs):
        assert kwargs["session_id"] == "sid"
        assert "api_key" not in kwargs
        return AIResult(success=True, text="reply", session_id="sid")

    sidecar_call.side_effect = respond
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar_call)
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
    await client.close()


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
    assert await storage.update_user_ai_credential("alice", "other", "key") == ["sid"]
    assert await storage.update_user_ai_credential("alice", "other", None) == []
    assert all(
        not m["session_id"]
        for m in await storage.get_chat_messages("job", username="alice")
    )


@pytest.mark.asyncio
async def test_noop_rotation_keeps_provider_session_and_generation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "noop.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p.with/slash", "key")
    generation = await storage.get_user_ai_credential_generation(
        "alice", "p.with/slash"
    )
    await storage.add_chat_message(
        "job",
        "assistant",
        "hello",
        username="alice",
        ai_provider="p.with/slash",
        session_id="sid",
    )
    assert await storage.update_user_ai_credential("alice", "p.with/slash", "key") == []
    assert (
        await storage.get_user_ai_credential_generation("alice", "p.with/slash")
        == generation
    )
    assert (await storage.get_chat_messages("job", username="alice"))[0][
        "session_id"
    ] == "sid"
    assert await storage.update_user_ai_credential("alice", "absent", None) == []
    assert (
        await storage.get_user_ai_credential_generation("alice", "p.with/slash")
        == generation
    )
    assert await storage.add_chat_message(
        "job",
        "assistant",
        "next",
        username="alice",
        ai_provider="p.with/slash",
        credential_generation=generation,
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

    async def generate(**_kwargs):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["corrupt", "wrong_key", "malformed_map"])
async def test_unreadable_credential_map_refuses_list_set_delete_and_preserves_data(
    tmp_path, monkeypatch, caplog, failure
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "corrupt.db")
    monkeypatch.setenv("ROOTCOZ_ENCRYPTION_KEY", "original-key")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "stored-secret")
    if failure == "corrupt":
        ciphertext = "enc:corrupt-secret"
    elif failure == "malformed_map":
        ciphertext = encryption.encrypt_value('["not a map"]')
    else:
        async with storage._connect_db() as db:
            ciphertext = (
                await (
                    await db.execute(
                        "SELECT ai_credentials_enc FROM users WHERE username = 'alice'"
                    )
                ).fetchone()
            )[0]
        monkeypatch.setenv("ROOTCOZ_ENCRYPTION_KEY", "different-key")
    if failure != "wrong_key":
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE users SET ai_credentials_enc = ? WHERE username = ?",
                (ciphertext, "alice"),
            )
            await db.commit()
    monkeypatch.setattr(main, "supported_key_providers", AsyncMock(return_value=["p"]))
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
    new_key = "new"  # pragma: allowlist secret
    with caplog.at_level(logging.WARNING):
        for operation in (
            lambda: main.get_user_ai_credentials(request),
            lambda: main.set_user_ai_credential(
                "p", main.AiCredentialInput(api_key=new_key), request
            ),
            lambda: main.delete_user_ai_credential("p", request),
        ):
            with pytest.raises(HTTPException) as exc:
                await operation()
            assert exc.value.status_code == 409
            assert "stored-secret" not in exc.value.detail
    async with storage._connect_db() as db:
        row = await (
            await db.execute(
                "SELECT ai_credentials_enc, ai_credential_generation FROM users WHERE username = 'alice'"
            )
        ).fetchone()
    assert row[0] == ciphertext
    assert row[1] == 1
    assert "stored-secret" not in caplog.text
    assert "corrupt-secret" not in caplog.text
    if failure == "wrong_key":
        monkeypatch.setenv("ROOTCOZ_ENCRYPTION_KEY", "original-key")
        assert await storage.get_user_ai_credentials("alice") == {"p": "stored-secret"}


@pytest.mark.asyncio
async def test_feedback_preview_uses_requester_context_and_resets(monkeypatch):
    monkeypatch.setattr(main, "_check_allow_list", lambda request: None)
    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(feedback_enabled=True)
    )
    monkeypatch.setattr(
        main, "_resolve_ai_config_values", lambda *args, **kwargs: ("p", "m")
    )
    monkeypatch.setattr(
        main, "_validate_catalog_pair", AsyncMock(return_value=("p", "m"))
    )

    async def generate(*_args, **_kwargs):
        assert ai_client.ai_username.get() == "alice"
        return object()

    monkeypatch.setattr(main, "generate_feedback_preview", generate)
    token = ai_client.ai_username.set("prior")
    try:
        await main.preview_feedback(
            SimpleNamespace(state=SimpleNamespace(username="alice")), object()
        )
        assert ai_client.ai_username.get() == "prior"
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_js_blank_key_rejected_before_storage(monkeypatch):
    monkeypatch.setattr(main, "_credential_user", AsyncMock(return_value="alice"))
    monkeypatch.setattr(main, "supported_key_providers", AsyncMock(return_value=["p"]))
    update = AsyncMock()
    monkeypatch.setattr(storage, "update_user_ai_credential", update)
    for blank in ("\ufeff", " \ufeff\u00a0"):
        with pytest.raises(HTTPException) as exc:
            await main.set_user_ai_credential(
                "p",
                main.AiCredentialInput(api_key=blank),
                SimpleNamespace(state=SimpleNamespace(username="alice", role="admin")),
            )
        assert exc.value.status_code == 400
    update.assert_not_awaited()
    await main.set_user_ai_credential(
        "p",
        main.AiCredentialInput(api_key="\u200b\ufeff"),
        SimpleNamespace(state=SimpleNamespace(username="alice", role="admin")),
    )
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_creation_logs_frames_not_secret(monkeypatch):
    client = AsyncMock()
    secret = "do-not-log-key"  # pragma: allowlist secret
    client.create_session.side_effect = ValueError(secret)
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    messages = []
    monkeypatch.setattr(
        chat.logger, "warning", lambda fmt, *args: messages.append(fmt % args)
    )
    assert (
        await chat._create_chat_session(system_prompt="", ai_provider="p", ai_model="m")
        is None
    )
    assert "_create_chat_session" in messages[0]
    assert secret not in messages[0]


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
