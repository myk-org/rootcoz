import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rootcoz import ai_client, main, storage
from rootcoz.config import Settings
from rootcoz.models import ReAnalyzeFailureRequest, UnifiedAnalyzeRequest


@pytest.mark.parametrize(
    "payload, expected", [({}, None), ({"force_server_credentials": True}, True)]
)
def test_failure_override_accepts_force(payload, expected):
    assert (
        ReAnalyzeFailureRequest.model_validate(payload).force_server_credentials
        is expected
    )


@pytest.mark.parametrize("saved, current", [(True, False), (False, True)])
def test_waiting_job_keeps_saved_force(saved, current, monkeypatch):
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(force_server_credentials=current)
    )
    body, merged = main._reconstruct_from_params(
        {
            "request_params": {
                "force_server_credentials": saved,
                "analysis_type": "jenkins",
            },
            "job_name": "job",
            "build_number": 1,
        }
    )
    assert body.force_server_credentials is saved
    assert merged.force_server_credentials is saved


def test_force_override_roundtrip():
    body = UnifiedAnalyzeRequest(
        type="raw",
        failures=[{"test_name": "test", "error": "failure"}],
        force_server_credentials=False,
    )
    settings = Settings(force_server_credentials=True)
    merged = main._merge_settings(body, settings)
    assert merged.force_server_credentials is False
    params = {}
    main._apply_base_analysis_overrides(params, body, merged)
    assert params["force_server_credentials"] is False
    reconstructed, effective = main._reconstruct_from_params(
        {
            "request_params": {**params, "analysis_type": "jenkins"},
            "job_name": "test",
            "build_number": 1,
        }
    )
    assert reconstructed.force_server_credentials is False
    assert effective.force_server_credentials is False


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_overlay_updates_background_credential_scope(
    force, monkeypatch, tmp_path
):
    from rootcoz.rootcoz_repo_settings import EffectiveRepoAnalysisSettings
    from rootcoz.sources.base import CISourceResult

    seen = []

    async def validate(provider, model):
        seen.append((ai_client.force_server_credentials.get(), provider, model))
        return provider, model

    async def overlay(*args, **kwargs):
        return EffectiveRepoAnalysisSettings(
            settings=Settings(force_server_credentials=force),
            ai_provider="openai",
            ai_model="m",
            peer_ai_configs=[{"ai_provider": "openai", "ai_model": "peer"}],
            additional_repos=[],
        )

    source = SimpleNamespace(
        requires_pre_fetch=lambda: False,
        fetch=AsyncMock(return_value=CISourceResult(failures=[], skip_analysis=False)),
        persist_fetch_metadata=AsyncMock(),
        cleanup=AsyncMock(),
    )
    monkeypatch.setattr(main, "create_source_from_request", lambda *args: source)
    monkeypatch.setattr(main, "resolve_repo_analysis_settings", overlay)
    monkeypatch.setattr(main, "_validate_catalog_pair", validate)
    monkeypatch.setattr(main, "_preflight_sidecar_check", AsyncMock(return_value=True))
    monkeypatch.setattr(
        main,
        "setup_analysis_workspace",
        AsyncMock(
            return_value=(
                SimpleNamespace(repo_manager=None, repo_path=tmp_path, cloned_repos={}),
                "",
            )
        ),
    )
    monkeypatch.setattr(main, "get_settings", lambda: Settings())
    monkeypatch.setattr(main.storage, "DB_PATH", tmp_path / "overlay.db")
    await main.storage.init_db()
    await main.storage.save_result("job", status="pending", result={})
    await main._process_ci_source_analysis(
        job_id="job",
        body=UnifiedAnalyzeRequest(
            type="raw", failures=[{"test_name": "t", "error": "e"}]
        ),
        merged=Settings(force_server_credentials=not force),
        display_name="job",
        ai_provider="openai",
        ai_model="m",
        peer_ai_configs=None,
        tests_repo_url="",
        tests_repo_ref="",
        resolved_tests_repo_token="",
        additional_repos_list=[],
        base_url="",
        username="alice",
    )
    assert seen == [(force, "openai", "m"), (force, "openai", "peer")]


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,current", [(True, False), (False, True)])
async def test_job_chat_init_and_resume_keep_saved_scope(
    saved, current, tmp_path, monkeypatch
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "job-chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    await storage.save_result(
        "job",
        "",
        "completed",
        {
            "status": "completed",
            "ai_provider": "openai",
            "ai_model": "m",
            "request_params": {"force_server_credentials": saved},
        },
    )
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(force_server_credentials=current)
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main, "_resolve_chat_credentials", AsyncMock(return_value=("", "", "", "", ""))
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    seen = []

    async def init(**kwargs):
        seen.append(ai_client.force_server_credentials.get())
        return "sid"

    async def send(**kwargs):
        seen.append(ai_client.force_server_credentials.get())
        assert kwargs["session_id"] == "sid"
        return True, "reply", "sid"

    monkeypatch.setattr(chat, "init_chat_session", init)
    monkeypatch.setattr(chat, "chat_with_ai", send)
    await main._init_chat_under_barrier("job", "alice")
    user, assistant = await storage.add_chat_message_pair(
        "job", "hello", username="alice", ai_provider="openai", ai_model="m"
    )
    await main._process_chat_message(
        job_id="job",
        user_msg_id=user,
        assistant_msg_id=assistant,
        message="hello",
        ai_provider_override=None,
        ai_model_override=None,
        username="alice",
    )
    assert seen == [saved, saved]


@pytest.mark.asyncio
@pytest.mark.parametrize("init", ["init_chat_session", "init_admin_chat_session"])
async def test_chat_session_uses_selected_server_pair(init, monkeypatch, tmp_path):
    from rootcoz import storage
    from rootcoz.engine import chat

    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "server-only"}]),
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            return_value={
                "modelListingSupported": True,
                "models": [{"provider": "openai", "id": "user-only"}],
            }
        ),
    )
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    created = AsyncMock(return_value="sid")
    monkeypatch.setattr(chat, "create_session_safely", created)
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: SimpleNamespace())
    token = ai_client.ai_username.set("alice")
    try:
        kwargs = {
            "ai_provider": "openai",
            "ai_model": "server-only",
            "repo_path": tmp_path,
        }
        if init == "init_chat_session":
            kwargs.update(job_id="job", job_name="job", build_number=1)
        assert await getattr(chat, init)(**kwargs) == "sid"
        assert "api_key" not in created.await_args.kwargs
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved, current, request_body, expected",
    [
        (True, False, b"", True),
        (False, True, b"", False),
        (False, False, b'{"force_server_credentials":true}', True),
    ],
)
async def test_reanalysis_validates_with_parent_force(
    saved, current, request_body, expected, monkeypatch, tmp_path
):
    from rootcoz import storage
    from rootcoz.models import AnalysisDetail, FailureAnalysis

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reanalysis.db")
    await storage.init_db()
    fa = FailureAnalysis(
        test_name="test",
        error="failure",
        analysis=AnalysisDetail(classification="CODE ISSUE"),
    )
    await storage.save_result(
        "job",
        status="completed",
        result={
            "failures": [fa.model_dump(mode="json")],
            "request_params": {
                "ai_provider": "openai",
                "ai_model": "m",
                "force_server_credentials": saved,
            },
        },
    )
    monkeypatch.setattr(
        main, "get_settings", lambda: Settings(force_server_credentials=current)
    )
    seen = []

    async def validate(provider, model):
        seen.append(ai_client.force_server_credentials.get())
        return provider, model

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)
    monkeypatch.setattr(main, "_reanalyze_failure_background", AsyncMock())
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", role="admin", is_admin=True),
        headers={},
        body=AsyncMock(return_value=request_body),
        json=AsyncMock(return_value=json.loads(request_body or b"{}")),
    )
    await main.re_analyze_failure(fa.id, request)
    assert seen == [expected]


@pytest.mark.asyncio
async def test_keyed_adapter_contract(monkeypatch):
    requests = []

    async def post(path, json):
        requests.append((path, json))
        return httpx.Response(
            200,
            json={
                "modelListingSupported": True,
                "models": [
                    {"provider": "openai", "id": "gpt", "name": "GPT"},
                    {"provider": "xai", "id": "other"},
                ],
            },
        )

    monkeypatch.setattr(
        ai_client,
        "get_sidecar_client",
        lambda: SimpleNamespace(_client=SimpleNamespace(post=post)),
    )
    # A cross-provider model poisons the entire response rather than being selectable.
    with pytest.raises(ValueError, match="Invalid key-scoped"):
        await ai_client.models_for_api_key("openai", "secret")
    expected = {"provider": "openai", "api_key": "secret"}  # pragma: allowlist secret
    assert requests == [("/models/for-api-key", expected)]


@pytest.mark.asyncio
async def test_public_discovery_contract_and_no_listing(monkeypatch):
    calls = []

    async def get_models_for_api_key(provider, key):
        calls.append((provider, key))
        return {"models": [], "modelListingSupported": False}

    monkeypatch.setattr(
        ai_client,
        "get_sidecar_client",
        lambda: SimpleNamespace(get_models_for_api_key=get_models_for_api_key),
    )
    assert await ai_client.models_for_api_key("xai", "secret") == {
        "models": [],
        "modelListingSupported": False,
    }
    assert calls == [("xai", "secret")]


@pytest.mark.asyncio
async def test_listed_empty_is_not_manual(monkeypatch):
    from rootcoz import storage

    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "key"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(return_value={"models": [], "modelListingSupported": True}),
    )
    token = ai_client.ai_username.set("alice")
    try:
        assert await ai_client.scoped_models() == {}
        with pytest.raises(ValueError, match="Unknown Pi-sidecar"):
            await ai_client.resolve_catalog_pair("openai", "gpt-4o")
    finally:
        ai_client.ai_username.reset(token)
