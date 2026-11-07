"""Key-scoped catalog and forced server routing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rootcoz import ai_client, main, storage
from rootcoz import models as models_module


@pytest.mark.asyncio
async def test_key_discovery_keeps_only_session_limits(monkeypatch):
    monkeypatch.setattr(
        ai_client,
        "get_sidecar_client",
        lambda: SimpleNamespace(
            get_models_for_api_key=AsyncMock(
                return_value={
                    "modelListingSupported": True,
                    "models": [
                        {
                            "provider": "google",
                            "id": "new-model",
                            "name": "New model",
                            "capabilities": {
                                "inputTokenLimit": 1000,
                                "outputTokenLimit": 100,
                                "extra": "do-not-expose",
                            },
                        }
                    ],
                }
            )
        ),
    )
    assert await ai_client.models_for_api_key("google", "secret") == {
        "modelListingSupported": True,
        "models": [
            {
                "provider": "google",
                "id": "new-model",
                "name": "New model",
                "source": "listing",
                "capabilities": {"inputTokenLimit": 1000, "outputTokenLimit": 100},
            }
        ],
    }


@pytest.mark.asyncio
async def test_scoped_discovery_and_call_fail_closed(monkeypatch):
    secret = "private-key-do-not-return"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(
            return_value=[
                {"provider": "openai", "id": "shared", "name": "Shared"},
                {"provider": "anthropic", "id": "claude", "name": "Claude"},
            ]
        ),
    )
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": secret})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            return_value={
                "modelListingSupported": True,
                "models": [
                    {"provider": "openai", "id": "shared", "name": "Shared"},
                    {"provider": "openai", "id": "user-only", "name": "User only"},
                ],
            }
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        models = await ai_client.scoped_models()
        assert models["openai"] == [
            {
                "provider": "openai",
                "id": "shared",
                "name": "Shared",
                "source": "api",
                "credential_sources": ["user", "server"],
                "can_use_server_providers": False,
                "verified": True,
            },
            {
                "provider": "openai",
                "id": "user-only",
                "name": "User only",
                "source": "api",
                "credential_sources": ["user"],
                "can_use_server_providers": False,
                "verified": True,
            },
        ]
        assert "xai" not in models
        assert secret not in str(models)
        with pytest.raises(ValueError, match="Unknown Pi-sidecar provider/model pair"):
            await ai_client.resolve_catalog_pair("openai", "not-listed-anywhere")
        # A key-listed model without token limits is still usable; the sidecar
        # reports no capabilities for key-scoped discovery.
        assert await ai_client.resolve_catalog_pair("openai", "user-only") == (
            "openai",
            "user-only",
        )
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("openai", "claude")
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_listed_unknown_requires_reliable_limits(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"google": "secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["google"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            return_value={
                "modelListingSupported": True,
                "models": [
                    {
                        "provider": "google",
                        "id": "good",
                        "capabilities": {
                            "inputTokenLimit": 1000,
                            "outputTokenLimit": 100,
                        },
                    },
                    {
                        "provider": "google",
                        "id": "missing",
                        "capabilities": {
                            "inputTokenLimit": 1000,
                        },
                    },
                    {
                        "provider": "google",
                        "id": "boolean",
                        "capabilities": {
                            "inputTokenLimit": True,
                            "outputTokenLimit": 100,
                        },
                    },
                    {
                        "provider": "google",
                        "id": "negative",
                        "capabilities": {
                            "inputTokenLimit": 1000,
                            "outputTokenLimit": -1,
                        },
                    },
                ],
            }
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert [entry["id"] for entry in scoped["google"]] == ["good"]
        assert scoped["google"][0]["verified"] is True
        assert await ai_client.resolve_catalog_pair("google", "good") == (
            "google",
            "good",
        )
        for model in ("missing", "boolean", "negative"):
            with pytest.raises(
                ValueError, match="Unknown Pi-sidecar provider/model pair"
            ):
                await ai_client.resolve_catalog_pair("google", model)
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_no_listing_allows_manual_only_with_matching_key(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"xai": "x-secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["xai"])
    )
    discovery = AsyncMock(return_value={"models": [], "modelListingSupported": False})
    monkeypatch.setattr(ai_client, "models_for_api_key", discovery)
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert scoped["xai"]
        assert all(not m["verified"] for m in scoped["xai"])
        assert await ai_client.resolve_catalog_pair("xai", "manual-model") == (
            "xai",
            "manual-model",
        )
        assert ai_client._selected_credential_source.get() == "user"
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("openai", "manual-model")
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("xai", "  ")
        assert discovery.await_args.args == ("xai", "x-secret")
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_manual_only_provider_visible_in_endpoint(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"custom-provider": "key"}),
    )
    monkeypatch.setattr(
        ai_client,
        "supported_key_providers",
        AsyncMock(return_value=["custom-provider"]),
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(return_value={"models": [], "modelListingSupported": False}),
    )
    monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
    request = type(
        "Request",
        (),
        {"state": type("State", (), {"username": "alice", "is_admin": False})()},
    )()
    data = await main.list_ai_models(request, provider="custom-provider")
    assert data["models"] == []
    assert data["modelListingSupported"] is False
    assert "key" not in str(data)


@pytest.mark.asyncio
async def test_missing_upstream_does_not_guess_user_models(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404",
                request=httpx.Request("POST", "http://localhost/models/for-api-key"),
                response=httpx.Response(404),
            )
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        assert await ai_client.scoped_models() == {}
        assert ai_client.model_listing_status.get()["openai"]["unavailable"]
        with pytest.raises(ValueError, match="Key-scoped model discovery unavailable"):
            await ai_client.resolve_catalog_pair("openai", "made-up")
        # Even an exact server pair cannot silently switch credential source.
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(return_value=[{"provider": "openai", "id": "shared"}]),
        )
        with pytest.raises(ValueError, match="Key-scoped model discovery unavailable"):
            await ai_client.resolve_catalog_pair("openai", "shared")
        monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
        request = type(
            "Request",
            (),
            {"state": type("State", (), {"username": "alice", "is_admin": False})()},
        )()
        data = await main.list_ai_models(request, provider="openai")
        assert data["modelListingSupported"] is True
        assert data["unavailable"] is True
        assert [model["id"] for model in data["models"]] == ["shared"]
        monkeypatch.setattr(
            ai_client,
            "models_for_api_key",
            AsyncMock(return_value={"models": [], "modelListingSupported": False}),
        )
        assert await ai_client.resolve_catalog_pair("openai", "manual") == (
            "openai",
            "manual",
        )
    finally:
        ai_client.ai_username.reset(token)


def _mock_key_discovery(monkeypatch, provider, discovery, catalog=None):
    """Point the user-key path at a single provider with one discovery response.

    Keeps every key-scoped test on the same mock wiring, so a change to how
    discovery is stubbed is made once.
    """
    monkeypatch.setattr(
        ai_client, "_get_model_catalog", AsyncMock(return_value=catalog or [])
    )
    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={provider: "secret"}),
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=[provider])
    )
    monkeypatch.setattr(
        ai_client, "models_for_api_key", AsyncMock(return_value=discovery)
    )


@pytest.mark.asyncio
async def test_key_listed_models_without_capabilities_stay_usable(monkeypatch):
    """Regression: pi-sidecar reports no capabilities for key-scoped discovery.

    Treating absent limits as unusable dropped every user-key model, so a stored
    key produced zero selectable models and the provider vanished from the picker
    (observed on prod for openai in v4.5.0).
    """
    # The provider is absent from the server catalog, as it is on prod.
    _mock_key_discovery(
        monkeypatch,
        "openai",
        {
            "modelListingSupported": True,
            "models": [
                {"provider": "openai", "id": "gpt-4o-mini", "name": "gpt-4o-mini"},
                {
                    "provider": "openai",
                    "id": "gpt-5.6-luna",
                    "name": "gpt-5.6-luna",
                },
            ],
        },
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert [entry["id"] for entry in scoped["openai"]] == [
            "gpt-4o-mini",
            "gpt-5.6-luna",
        ]
        assert all(
            entry["credential_sources"] == ["user"] for entry in scoped["openai"]
        )
        assert await ai_client.resolve_catalog_pair("openai", "gpt-5.6-luna") == (
            "openai",
            "gpt-5.6-luna",
        )
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_declared_but_unusable_limits_are_still_rejected(monkeypatch):
    """Absence is fine; a declared-but-broken limit is not."""
    _mock_key_discovery(
        monkeypatch,
        "google",
        {
            "modelListingSupported": True,
            "models": [
                {
                    "provider": "google",
                    "id": "unlisted",
                    "name": "No limits declared",
                },
                {
                    "provider": "google",
                    "id": "absurd",
                    "name": "Beyond safe integer",
                    "capabilities": {
                        "inputTokenLimit": 2**53,
                        "outputTokenLimit": 100,
                    },
                },
            ],
        },
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert [entry["id"] for entry in scoped["google"]] == ["unlisted"]
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_status_reports_models_the_key_actually_yielded(monkeypatch):
    """A key that stored but surfaced nothing must not look ready."""
    _mock_key_discovery(
        monkeypatch,
        "openai",
        {
            "modelListingSupported": True,
            "models": [
                {"provider": "openai", "id": "gpt-4o-mini", "name": "gpt-4o-mini"},
                {
                    "provider": "openai",
                    "id": "broken",
                    "name": "Broken",
                    "capabilities": {
                        "inputTokenLimit": 2**53,
                        "outputTokenLimit": 1,
                    },
                },
            ],
        },
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert [entry["id"] for entry in scoped["openai"]] == ["gpt-4o-mini"]
        status = ai_client.model_listing_status.get()["openai"]
        assert status["has_api_key"] is True
        # One model survived the filter, so one is reported, not the two returned.
        assert status["model_count"] == 1
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_status_counts_distinct_model_ids(monkeypatch):
    """A discovery response repeating an ID must not inflate model_count."""
    _mock_key_discovery(
        monkeypatch,
        "openai",
        {
            "modelListingSupported": True,
            "models": [
                {"provider": "openai", "id": "dup", "name": "Dup"},
                {"provider": "openai", "id": "dup", "name": "Dup again"},
                {"provider": "openai", "id": "other", "name": "Other"},
            ],
        },
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert sorted(entry["id"] for entry in scoped["openai"]) == ["dup", "other"]
        assert ai_client.model_listing_status.get()["openai"]["model_count"] == 2
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_manual_only_key_reports_zero_without_listing_support(monkeypatch):
    """No listing + no suggestions means manual entry, not a dead key."""
    _mock_key_discovery(
        monkeypatch, "mystery", {"modelListingSupported": False, "models": []}
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        # Preserved as an empty bucket so the provider stays selectable.
        assert scoped["mystery"] == []
        status = ai_client.model_listing_status.get()["mystery"]
        assert status["modelListingSupported"] is False
        assert status["model_count"] == 0
        # Manual entry is still accepted, so the key is usable.
        assert await ai_client.resolve_catalog_pair("mystery", "typed-by-hand") == (
            "mystery",
            "typed-by-hand",
        )
    finally:
        ai_client.ai_username.reset(token)


# -- Session support marks (#423) -----------------------------------------
#
# A provider flagged ``trustKeyScopedListing`` reports the gateway's whole
# catalog, including ids the sidecar will refuse to open a session with: it
# needs declared token limits, which a listed-but-undeclared id has nowhere.
# Each pair is therefore marked (``session_supported``), and every assertion
# below is **paired** — one pair marked, its neighbour in the same payload not —
# because an implementation that never writes the key at all would satisfy an
# absence-only assertion.

FLAGGED = "EnMaaS"
DECLARED = "rits/zai-org/glm-5-3"
UNDECLARED = "gemini-3.1-pro-preview"
LIMITS = {"inputTokenLimit": 1000, "outputTokenLimit": 100}


def _use_agent_manifest(monkeypatch, tmp_path, providers):
    """Point PI_SIDECAR_AGENT_DIR at a manifest this test wrote."""
    agent_dir = tmp_path / "pi-sidecar-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.json").write_text(json.dumps({"providers": providers}))
    monkeypatch.setenv("PI_SIDECAR_AGENT_DIR", str(agent_dir))


def _capture_server_listing(monkeypatch, rows):
    """Serve one provider's captured key-scoped listing without a database."""
    captured = [
        {
            "provider": provider,
            "model": model,
            "name": model,
            "capabilities": capabilities,
        }
        for provider, model, capabilities in rows
    ]

    async def _rows():
        return captured

    monkeypatch.setattr(storage, "list_server_provider_key_models", _rows)


def _fake_request(username: str = "alice"):
    return type(
        "Request",
        (),
        {"state": type("State", (), {"username": username, "is_admin": False})()},
    )()


def _patch_request_scope(monkeypatch):
    """Make the /api/ai-models route callable with no authenticated user DB."""
    monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
    # The route resolves the caller's own keys; a test with no database keeps
    # that answer empty, unless it stubbed discovery itself.
    if not isinstance(storage.get_user_ai_credentials, AsyncMock):
        monkeypatch.setattr(
            storage, "get_user_ai_credentials", AsyncMock(return_value={})
        )


async def _provider_models(monkeypatch, provider):
    """GET /api/ai-models?provider=<provider>, exactly as a picker sees it."""
    _patch_request_scope(monkeypatch)
    payload = await main.list_ai_models(_fake_request(), provider=provider)
    return payload["models"]


def _flagged_manifest(models):
    return {
        FLAGGED: {
            "trustKeyScopedListing": True,
            "apiKey": "$ENMAAS_API_KEY",
            "baseUrl": "https://gw.example/v1",
            "models": models,
        }
    }


@pytest.mark.asyncio
async def test_undeclared_listing_model_is_marked_unsessionable(monkeypatch, tmp_path):
    """A listed-but-undeclared id is marked unusable for sessions.

    Fails an implementation that never writes ``session_supported`` (the key is
    absent, not ``False``).
    """
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _capture_server_listing(monkeypatch, [(FLAGGED, UNDECLARED, None)])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": UNDECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    entry = next(model for model in models if model["id"] == UNDECLARED)
    assert entry["session_supported"] is False
    assert entry["session_unsupported_reason"] == ai_client.SESSION_UNSUPPORTED_REASON


@pytest.mark.asyncio
async def test_declared_model_with_a_stale_capture_stays_supported(
    monkeypatch, tmp_path
):
    """A declared id is session-capable even when its capture is stale.

    The measured prod shape: ``DECLARED`` is in the manifest with both limits,
    yet its captured row still carries ``{}`` from before the declaration.
    """
    _use_agent_manifest(
        monkeypatch,
        tmp_path,
        _flagged_manifest(
            [{"id": DECLARED, "contextWindow": 1048576, "maxTokens": 393216}]
        ),
    )
    _capture_server_listing(monkeypatch, [(FLAGGED, DECLARED, None)])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": DECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    entry = next(model for model in models if model["id"] == DECLARED)
    assert entry["session_supported"] is True
    assert "session_unsupported_reason" not in entry


@pytest.mark.asyncio
async def test_declared_without_limits_still_resolves(monkeypatch, tmp_path):
    """Declaring a model is enough: limits are optional per manifest model.

    One response, two pairs: (a) a declared model with no
    ``contextWindow``/``maxTokens`` is supported — fails an implementation that
    never calls ``_mark_session_support`` (the key would be absent) and rev 1-3's
    rule that marked every undeclared id off the provider flag alone; (b) its
    undeclared sibling is refused with the reason — fails an implementation that
    marks every pair ``True`` without reading ``models[]``.
    """
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _capture_server_listing(
        monkeypatch, [(FLAGGED, DECLARED, None), (FLAGGED, UNDECLARED, None)]
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": DECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    by_id = {model["id"]: model for model in models}
    assert by_id[DECLARED]["session_supported"] is True
    assert "session_unsupported_reason" not in by_id[DECLARED]
    assert by_id[UNDECLARED]["session_supported"] is False
    assert by_id[UNDECLARED]["session_unsupported_reason"] == (
        ai_client.SESSION_UNSUPPORTED_REASON
    )


@pytest.mark.asyncio
async def test_limit_bearing_listing_entry_stays_supported(monkeypatch, tmp_path):
    """An undeclared id whose capture carries both limits is session-capable.

    The sidecar reads the listing's capabilities before the manifest snapshot,
    so the picker must not over-block: (a) fails the implementation that marks
    every undeclared id ``False`` without consulting ``_entry_limits``, (b) fails
    one that marks everything ``True``. Reading ``contextWindow``/``maxTokens``
    from the entry instead of ``capabilities`` yields ``None`` here.
    """
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _capture_server_listing(
        monkeypatch, [(FLAGGED, DECLARED, dict(LIMITS)), (FLAGGED, UNDECLARED, {})]
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": DECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    by_id = {model["id"]: model for model in models}
    assert by_id[DECLARED]["session_supported"] is True
    assert "session_unsupported_reason" not in by_id[DECLARED]
    assert by_id[UNDECLARED]["session_supported"] is False
    assert by_id[UNDECLARED]["session_unsupported_reason"] == (
        ai_client.SESSION_UNSUPPORTED_REASON
    )


@pytest.mark.asyncio
async def test_unflagged_key_listing_is_not_marked(monkeypatch):
    """An unflagged provider is never touched, whatever its captured rows say."""
    _capture_server_listing(monkeypatch, [("openai", "gpt-5.6-luna", None)])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5.6-luna"}]),
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    models = await _provider_models(monkeypatch, "openai")
    entry = next(model for model in models if model["id"] == "gpt-5.6-luna")
    assert "session_supported" not in entry
    assert "session_unsupported_reason" not in entry
    # v4.5.0 guard: the pair still resolves, so nothing was gated.
    monkeypatch.setattr(
        ai_client, "require_server_provider_grant", AsyncMock(return_value=None)
    )
    assert await ai_client.resolve_catalog_pair("openai", "gpt-5.6-luna") == (
        "openai",
        "gpt-5.6-luna",
    )


@pytest.mark.asyncio
async def test_snapshot_suggestions_are_never_marked(monkeypatch, tmp_path):
    """A ``modelListingSupported: false`` key keeps the manual-entry path."""
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _mock_key_discovery(
        monkeypatch, "deepseek", {"modelListingSupported": False, "models": []}
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        entries = scoped["deepseek"]
        assert entries
        assert all("session_supported" not in entry for entry in entries)
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_mark_reaches_user_key_discovered_pairs_and_casing(monkeypatch, tmp_path):
    """A user's own gateway key is marked, and the manifest casing matches.

    No server capture at all: the pair comes from the user's listing, and the
    manifest spells the provider ``EnMaaS`` while the credential key is
    lowercase.
    """
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _mock_key_discovery(
        monkeypatch,
        "enmaas",
        {
            "modelListingSupported": True,
            "models": [
                {"provider": "enmaas", "id": DECLARED},
                {"provider": "enmaas", "id": UNDECLARED},
            ],
        },
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        by_id = {entry["id"]: entry for entry in scoped["enmaas"]}
        assert by_id[DECLARED]["session_supported"] is True
        assert by_id[UNDECLARED]["session_supported"] is False
        assert by_id[UNDECLARED]["session_unsupported_reason"] == (
            ai_client.SESSION_UNSUPPORTED_REASON
        )
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_manual_only_sentinel_pair_is_never_marked(monkeypatch, tmp_path):
    """A flagged provider with no listing keeps its manual-entry sentinel pair."""
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _mock_key_discovery(
        monkeypatch, FLAGGED, {"modelListingSupported": False, "models": []}
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "shared"}]),
    )
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        # The sentinel is an empty dict under an empty id and is dropped here.
        assert scoped[FLAGGED] == []
        assert [entry["id"] for entry in scoped["openai"]] == ["shared"]
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_flagged_provider_payload_carries_no_manifest_values(
    monkeypatch, tmp_path
):
    """No manifest value leaks into the payload, and the mark is positive.

    Fails the never-called mark (the two positive assertions), the mark applied
    regardless of ``_is_key_scoped_provider`` (the unflagged neighbour would
    carry the key), and any manifest value reaching the picker payload.
    """
    manifest = _flagged_manifest([{"id": DECLARED}])
    manifest[FLAGGED]["apiKey"] = (
        "sk-manifest-secret-not-a-response-field"  # pragma: allowlist secret
    )
    _use_agent_manifest(monkeypatch, tmp_path, manifest)
    _capture_server_listing(
        monkeypatch, [(FLAGGED, DECLARED, None), (FLAGGED, UNDECLARED, None)]
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(
            return_value=[
                {"provider": FLAGGED, "id": DECLARED},
                {"provider": "openai", "id": "gpt-5.6-luna"},
            ]
        ),
    )
    _patch_request_scope(monkeypatch)
    payload = await main.list_ai_models(_fake_request(), provider="")
    flagged = {model["id"]: model for model in payload["providers"][FLAGGED]}
    assert flagged[DECLARED]["session_supported"] is True
    assert flagged[UNDECLARED]["session_supported"] is False
    assert flagged[UNDECLARED]["session_unsupported_reason"] == (
        ai_client.SESSION_UNSUPPORTED_REASON
    )
    unflagged = payload["providers"]["openai"]
    assert unflagged
    for entry in unflagged:
        assert "session_supported" not in entry
        assert "session_unsupported_reason" not in entry
    serialised = json.dumps(payload)
    for banned in (
        "sk-manifest-secret-not-a-response-field",
        "https://gw.example/v1",
        "trustKeyScopedListing",
        "contextWindow",
    ):
        assert banned not in serialised
    for entry in payload["providers"][FLAGGED]:
        assert "apiKey" not in entry
        assert "baseUrl" not in entry
        assert "models" not in entry
    # A real analysis serialises byte-identically to before the flag existed.
    assert "placeholder" not in models_module.AnalysisDetail(
        classification="CODE ISSUE", details="x"
    ).model_dump(mode="json")
    assert (
        models_module.AnalysisDetail(
            details=models_module.ANALYSIS_PLACEHOLDER, placeholder=True
        ).model_dump(mode="json")["placeholder"]
        is True
    )


@pytest.mark.asyncio
async def test_flagged_provider_catalog_only_models_stay_unmarked(
    monkeypatch, tmp_path
):
    """A flagged provider with no capture keeps its catalog pairs unmarked."""
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _capture_server_listing(monkeypatch, [])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(
            return_value=[
                {"provider": FLAGGED, "id": "catalog-only"},
                {"provider": "openai", "id": "shared"},
            ]
        ),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    assert [model["id"] for model in models] == ["catalog-only"]
    assert "session_supported" not in models[0]


@pytest.mark.asyncio
async def test_absent_limits_stay_usable_but_not_session_supported(
    monkeypatch, tmp_path
):
    """The one boundary between the pool filter and the picker mark.

    Absent limits stay *usable* (the entry is offered) yet are *not*
    session-supported (declaring the model is the fix), and ``_entry_limits``
    agrees with the filter about every declared-but-broken pair.
    """
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    _capture_server_listing(monkeypatch, [(FLAGGED, UNDECLARED, {})])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": UNDECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    assert [model["id"] for model in models] == [UNDECLARED]
    assert models[0]["session_supported"] is False
    assert models[0]["session_unsupported_reason"] == (
        ai_client.SESSION_UNSUPPORTED_REASON
    )
    assert ai_client._entry_limits({"capabilities": {}}) is None
    assert ai_client._entry_limits({"capabilities": {"inputTokenLimit": 1000}}) is None
    assert (
        ai_client._entry_limits(
            {"capabilities": {"inputTokenLimit": 2**53, "outputTokenLimit": 1}}
        )
        is None
    )
    assert ai_client._entry_limits({"capabilities": dict(LIMITS)}) == (1000, 100)
    assert not ai_client._listing_entry_usable(
        {"capabilities": {"inputTokenLimit": 2**53, "outputTokenLimit": 1}}
    )


@pytest.mark.asyncio
async def test_flagged_provider_with_empty_models_is_not_marked(monkeypatch, tmp_path):
    """One flag is never a blanket block: no declared ids means no mark at all."""
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([]))
    _capture_server_listing(monkeypatch, [(FLAGGED, UNDECLARED, None)])
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": FLAGGED, "id": UNDECLARED}]),
    )
    models = await _provider_models(monkeypatch, FLAGGED)
    assert "session_supported" not in models[0]
    # The same manifest with one declared id does mark the pair.
    _use_agent_manifest(monkeypatch, tmp_path, _flagged_manifest([{"id": DECLARED}]))
    models = await _provider_models(monkeypatch, FLAGGED)
    assert models[0]["session_supported"] is False
    assert models[0]["session_unsupported_reason"] == (
        ai_client.SESSION_UNSUPPORTED_REASON
    )
