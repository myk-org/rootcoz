"""AI client adapter — rootcoz-specific AI setup."""

from pi_sidecar_client import AIResult, call_ai, call_ai_once, set_usage_recorder

# rootcoz-specific: valid providers for this application
VALID_AI_PROVIDERS = {"claude", "cursor", "gemini"}

# Builtin tools for AI sessions — no bash access (MANDATORY per project rules)
# Separate constants to allow independent evolution: analysis may gain tools
# (e.g. write) that chat should never have.
CHAT_BUILTIN_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep", "subagent")
ANALYSIS_BUILTIN_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep", "subagent")


def _setup_usage_recorder() -> None:
    """Register rootcoz's token tracking as the usage recorder.

    Must be called once at startup (main.py app lifespan).
    The callback maps pi-sidecar's request_id to rootcoz's job_id.
    """

    async def _rootcoz_recorder(
        *,
        request_id: str,
        result: AIResult,
        call_type: str,
        prompt_chars: int = 0,
        ai_provider: str = "",
        ai_model: str = "",
    ) -> None:
        # Late import so test mocks on rootcoz.token_tracking.record_ai_usage
        # are picked up at call time, not registration time.
        from rootcoz.token_tracking import record_ai_usage

        await record_ai_usage(
            job_id=request_id,  # rootcoz uses job_id
            result=result,
            call_type=call_type,
            prompt_chars=prompt_chars,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )

    set_usage_recorder(_rootcoz_recorder)


__all__ = [
    "AIResult",
    "ANALYSIS_BUILTIN_TOOLS",
    "CHAT_BUILTIN_TOOLS",
    "VALID_AI_PROVIDERS",
    "_setup_usage_recorder",
    "call_ai",
    "call_ai_once",
]
