"""Guard: unit tests must never reach a real sidecar."""

from __future__ import annotations

import pytest

from rootcoz import ai_client


@pytest.mark.asyncio
async def test_call_ai_once_denied_by_default() -> None:
    with pytest.raises(AssertionError, match="Unexpected real sidecar"):
        await ai_client.call_ai_once("hello", ai_provider="cursor", ai_model="x")


@pytest.mark.asyncio
async def test_call_ai_denied_by_default() -> None:
    with pytest.raises(AssertionError, match="Unexpected real sidecar"):
        await ai_client.call_ai("hello", ai_provider="claude", ai_model="y")


@pytest.mark.asyncio
async def test_list_models_uses_mock_client_not_network(
    _mock_sidecar_calls,
) -> None:
    """list_models may run, but only against the autouse mock client."""
    models = await ai_client.list_models("cursor")
    assert models == []
    _mock_sidecar_calls.get_models.assert_awaited()
