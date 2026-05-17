import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:9100")


@dataclass
class AITokenUsage:
    """Token usage data from an AI call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    provider: str = ""
    model: str = ""
    session_id: str = ""


@dataclass
class AIResult:
    """Result from an AI call."""

    success: bool
    text: str
    usage: AITokenUsage | None = None
    session_id: str | None = None

    async def record_usage(
        self,
        *,
        job_id: str,
        call_type: str,
        prompt_chars: int = 0,
        ai_provider: str = "",
        ai_model: str = "",
    ) -> None:
        """Record token usage to the database. Best-effort — never raises."""
        from rootcoz.token_tracking import record_ai_usage

        await record_ai_usage(
            job_id=job_id,
            result=self,
            call_type=call_type,
            prompt_chars=prompt_chars,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )


# Provider mapping: rootcoz provider names → sidecar provider names
_PROVIDER_MAP = {
    "cursor": "acpx-cursor",
    "claude": "google-vertex-claude",
    "gemini": "google",
}


def _map_provider_model(provider: str, model: str) -> tuple[str, str]:
    """Map rootcoz provider/model to sidecar provider/model."""
    sidecar_provider = _PROVIDER_MAP.get(provider, provider)
    sidecar_model = model
    # Cursor models need the cursor: prefix
    if sidecar_provider == "acpx-cursor" and not model.startswith("cursor:"):
        sidecar_model = f"cursor:{model}"
    return sidecar_provider, sidecar_model


class SidecarClient:
    """HTTP client for the Pi SDK sidecar service."""

    def __init__(self, base_url: str = SIDECAR_URL):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=600.0)

    async def health(self) -> dict:
        """Check sidecar health."""
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def get_models(self) -> list[dict]:
        """Get available models."""
        resp = await self._client.get("/models")
        resp.raise_for_status()
        return resp.json().get("models", [])

    async def refresh_models(self) -> list[dict]:
        """Trigger model discovery and return updated list."""
        resp = await self._client.post("/models/refresh")
        resp.raise_for_status()
        return resp.json().get("models", [])

    async def create_session(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        cwd: str = "/tmp",
    ) -> str:
        """Create a new AI session. Returns session_id."""
        sidecar_provider, sidecar_model = _map_provider_model(provider, model)
        body: dict[str, Any] = {
            "provider": sidecar_provider,
            "model": sidecar_model,
            "system_prompt": system_prompt,
            "cwd": cwd,
        }
        resp = await self._client.post("/sessions", json=body)
        resp.raise_for_status()
        return resp.json()["session_id"]

    async def prompt(
        self, session_id: str, message: str, timeout: float | None = None
    ) -> AIResult:
        """Send a message to a session. Returns AIResult."""
        request_timeout = timeout or self._client.timeout
        resp = await self._client.post(
            f"/sessions/{session_id}/prompt",
            json={"message": message},
            timeout=request_timeout,
        )
        if resp.status_code != 200:
            error = resp.json().get("error", resp.text)
            return AIResult(success=False, text=error)

        data = resp.json()
        usage_data = data.get("usage", {})
        usage = AITokenUsage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            cache_read_tokens=usage_data.get("cache_read_tokens", 0),
            cache_write_tokens=usage_data.get("cache_write_tokens", 0),
            cost_usd=usage_data.get("cost_usd"),
            duration_ms=usage_data.get("duration_ms"),
        )
        return AIResult(
            success=True,
            text=data.get("text", ""),
            usage=usage,
        )

    async def abort(self, session_id: str) -> None:
        """Abort an in-progress prompt."""
        resp = await self._client.post(f"/sessions/{session_id}/abort")
        resp.raise_for_status()

    async def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        resp = await self._client.delete(f"/sessions/{session_id}")
        resp.raise_for_status()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


# Singleton client
_client: SidecarClient | None = None


def get_sidecar_client() -> SidecarClient:
    """Get the singleton sidecar client."""
    global _client
    if _client is None:
        _client = SidecarClient()
    return _client


# --- Convenience functions for single-shot AI calls ---


async def call_ai(
    prompt: str,
    *,
    ai_provider: str = "",
    ai_model: str = "",
    cwd: str | None = None,
    system_prompt: str = "",
    ai_cli_timeout: int | None = None,
    session_id: str | None = None,
    **kwargs: Any,
) -> AIResult:
    """Call AI via the sidecar.

    Creates a new session (or reuses *session_id*), sends the prompt,
    and returns the result with session_id attached.

    Session lifecycle:
    - Caller is responsible for deleting sessions when done.
    - For single-shot calls, use ``async with call_ai_ephemeral(...)``
      or call ``client.delete_session()`` manually after.
    - For multi-turn (peer debate), pass ``session_id`` from the
      previous result to continue the conversation.
    """
    client = get_sidecar_client()
    created_session = False
    try:
        if not session_id:
            session_id = await client.create_session(
                provider=ai_provider,
                model=ai_model,
                system_prompt=system_prompt or "You are a helpful assistant.",
                cwd=cwd or "/tmp",
            )
            created_session = True
        # Convert minutes to seconds for httpx timeout
        timeout = ai_cli_timeout * 60.0 if ai_cli_timeout else None
        result = await client.prompt(session_id, prompt, timeout=timeout)
        # Attach session_id to result so callers can reuse or clean up
        result.session_id = session_id
        return result
    except Exception as e:
        logger.error("Sidecar call failed: %s", e)
        # Clean up session if WE created it and the prompt failed
        if created_session and session_id:
            try:
                await client.delete_session(session_id)
            except Exception:
                logger.debug(
                    "Failed to cleanup leaked session %s", session_id, exc_info=True
                )
        return AIResult(success=False, text=str(e))


async def call_ai_once(
    prompt: str,
    *,
    ai_provider: str = "",
    ai_model: str = "",
    cwd: str | None = None,
    system_prompt: str = "",
    ai_cli_timeout: int | None = None,
    **kwargs: Any,
) -> AIResult:
    """Single-shot AI call with automatic session cleanup.

    Creates a session, sends the prompt, and always deletes the session.
    Use this for one-off calls (analysis, bug creation, etc.).
    Use ``call_ai`` directly for multi-turn conversations (peer debate).
    """
    result = await call_ai(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        cwd=cwd,
        system_prompt=system_prompt,
        ai_cli_timeout=ai_cli_timeout,
        **kwargs,
    )
    # Always clean up — this is a single-shot call
    if result.session_id:
        try:
            await get_sidecar_client().delete_session(result.session_id)
        except Exception:
            pass
        result.session_id = None  # Clear so caller doesn't try to reuse
    return result


async def check_sidecar_available() -> tuple[bool, str]:
    """Check if the sidecar is healthy. Returns (available, message)."""
    try:
        client = get_sidecar_client()
        data = await client.health()
        if data.get("status") == "ok":
            return True, "Sidecar is healthy"
        return False, f"Sidecar unhealthy: {data}"
    except Exception as e:
        return False, f"Sidecar not reachable: {e}"


async def list_models(provider: str = "") -> list[dict]:
    """List available models, optionally filtered by provider."""
    client = get_sidecar_client()
    models = await client.get_models()
    if provider:
        sidecar_provider = _PROVIDER_MAP.get(provider, provider)
        models = [m for m in models if m.get("provider") == sidecar_provider]
    return models


async def run_parallel_with_limit(
    tasks: list,
    max_concurrency: int = 5,
) -> list:
    """Run async tasks in parallel with concurrency limit.

    Run async tasks in parallel with a concurrency limit.
    Each task is a coroutine.
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    async def limited(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(limited(t) for t in tasks), return_exceptions=True)


# Valid providers — matches what the sidecar supports
VALID_AI_PROVIDERS = {"claude", "cursor", "gemini"}
