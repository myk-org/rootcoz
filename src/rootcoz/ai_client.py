"""AI client adapter — rootcoz-specific AI setup."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from string import Template
from typing import Any, cast
from urllib.parse import quote

import httpx
from pi_sidecar_client import (
    AIResult,
    AITokenUsage,
    SidecarClient,
    _validate_api_key,
    get_sidecar_client,
    set_usage_recorder,
)
from pi_sidecar_client import call_ai as _call_ai
from pi_sidecar_client import list_models as _list_models_raw

from rootcoz import get_child_logger

logger = get_child_logger(__name__)

# Only explicitly initiated AI tasks set this; unrelated request tasks never inherit credentials.
ai_username: ContextVar[str] = ContextVar("ai_username", default="")
_selected_credential_source: ContextVar[str] = ContextVar(
    "selected_credential_source", default=""
)
chat_session_source: ContextVar[str] = ContextVar("chat_session_source", default="")
model_listing_status: ContextVar[dict[str, dict[str, bool | int]] | None] = ContextVar(
    "model_listing_status", default=None
)
_selected_key_generation: ContextVar[tuple[str, str, str, int] | None] = ContextVar(
    "selected_key_generation", default=None
)


def _provider_registry_entries(payload: object) -> list[Mapping[str, Any]] | None:
    """Sidecar ``/providers`` entries that carry a usable provider ID, or ``None``.

    Fails closed: a payload that is not a list, or a list holding *any*
    malformed entry (non-mapping, or a missing/empty/non-string ``provider``),
    returns None — registration is unknown, never a filtered subset of the
    valid entries. A subset would make an omitted provider look unregistered,
    and credential rotation would then delete a case-variant provider's stored
    key and drop its sessions. Only a fully valid list returns its entries.

    The warning names no entry contents: malformed entries are counted, and
    only valid provider IDs are listed.
    """
    if not isinstance(payload, list):
        logger.warning("Sidecar provider registry payload is not a list")
        return None
    entries = [
        entry
        for entry in payload
        if isinstance(entry, Mapping)
        and isinstance(entry.get("provider"), str)
        and entry["provider"]
    ]
    if len(entries) != len(payload):
        logger.warning(
            "Sidecar provider registry has %d malformed entries; usable providers=%s",
            len(payload) - len(entries),
            sorted({entry["provider"] for entry in entries}),
        )
        return None
    return entries


def parse_ambient_auth_provider_ids(value: str) -> list[str]:
    """Parse an AMBIENT_AUTH_PROVIDERS CSV the way its consumers read it.

    Split on comma, strip, drop empties, de-duplicate case-insensitively; the
    first occurrence's spelling wins so a stored value round-trips. Shared by
    the settings write paths (which canonicalise and store the result) and the
    exclusion set below.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for part in value.split(","):
        provider = part.strip()
        if provider and provider.lower() not in seen:
            seen.add(provider.lower())
            ids.append(provider)
    return ids


def ambient_auth_provider_exclusions() -> set[str]:
    """Normalized provider IDs excluded from the per-user key flow.

    ``AMBIENT_AUTH_PROVIDERS`` is admin/env-typed, so its spelling is not
    guaranteed to match the sidecar's registry: compare through
    :func:`normalize_provider` on both sides, or an ID saved as
    ``Google-Vertex-Claude`` would never exclude ``google-vertex-claude`` and
    the provider would keep offering keys that do nothing.
    """
    from rootcoz.config import get_settings

    return {
        normalize_provider(provider)
        for provider in parse_ambient_auth_provider_ids(
            get_settings().ambient_auth_providers or ""
        )
    }


async def _discover_session_key_providers() -> list[str] | None:
    """Sidecar key-capable providers minus the ambient set; ``None`` on failure.

    Shared by the fail-closed :func:`supported_key_providers` gate and the
    fail-open :func:`provider_accepts_user_keys` availability probe, which
    must distinguish "confirmed unsupported" from "could not discover".
    """
    try:
        providers = await get_sidecar_client().get_providers()
    except httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError:
        logger.warning("Unable to discover session-key providers")
        return None
    entries = _provider_registry_entries(providers)
    if entries is None:
        return None
    ambient = ambient_auth_provider_exclusions()
    return sorted(
        {
            entry["provider"]
            for entry in entries
            if entry.get("supportsSessionApiKey") is True
            and normalize_provider(entry["provider"]) not in ambient
        }
    )


async def supported_key_providers() -> list[str]:
    """List registered providers with affirmative session-key capability.

    Providers in the ``AMBIENT_AUTH_PROVIDERS`` setting are excluded even
    when the sidecar reports them as key-capable: they authenticate via
    ambient credentials (ADC, OAuth, CLI login) and never consume a
    per-session user API key, so offering them in the credential flow only
    lets users store keys that do nothing. Fails closed: an unreachable
    sidecar yields no providers.
    """
    discovered = await _discover_session_key_providers()
    return discovered if discovered is not None else []


# Bounded cache for the availability probe: (ambient setting, fetch time,
# supported set). Chat-history reads recheck eligibility per request, so the
# raw discovery is cached briefly instead of hitting the sidecar every poll.
_provider_eligibility_cache: tuple[str, float, frozenset[str]] | None = None
_PROVIDER_ELIGIBILITY_TTL_SEC = 30.0


async def provider_accepts_user_keys(provider: str) -> bool | None:
    """Whether a provider currently accepts user API keys, for availability.

    Returns ``None`` when discovery failed — chat-session availability then
    preserves the session instead of retiring it on a transient sidecar
    outage. The result is cached for a short window (invalidated immediately
    when ``AMBIENT_AUTH_PROVIDERS`` changes) so repeated chat-history reads
    do not each perform a sidecar round-trip. The credential save gate uses
    the uncached :func:`supported_key_providers` directly.
    """
    global _provider_eligibility_cache
    from rootcoz.config import get_settings

    ambient_setting = get_settings().ambient_auth_providers
    now = time.monotonic()
    cached = _provider_eligibility_cache
    if (
        cached is not None
        and cached[0] == ambient_setting
        and now - cached[1] < _PROVIDER_ELIGIBILITY_TTL_SEC
    ):
        return provider in cached[2]
    discovered = await _discover_session_key_providers()
    if discovered is None:
        # Do not cache failures: a recovered sidecar is seen on the next read.
        return None
    _provider_eligibility_cache = (
        ambient_setting,
        now,
        frozenset(discovered),
    )
    return provider in _provider_eligibility_cache[2]


def clear_provider_eligibility_cache() -> None:
    """Reset the availability-probe cache (tests, forced re-discovery)."""
    global _provider_eligibility_cache
    _provider_eligibility_cache = None


class InactiveServerProviderError(ValueError):
    """Server credentials for this provider are deactivated (ACTIVE_SERVER_PROVIDERS)."""


def _active_server_provider_entries() -> frozenset[str] | None:
    """Admin-selected active server providers (public-normalized); None = all.

    Each saved entry is normalized through :func:`public_provider_name`, so a
    public picker name (``claude``) and a legacy catalog ID (``cli-claude``)
    saved by an earlier admin both resolve to the same canonical form that
    catalog-provider comparisons use.  Case is preserved here; resolving it
    against the catalog is :func:`is_server_provider_active`'s job.
    """
    from rootcoz.config import get_settings

    raw = get_settings().active_server_providers
    entries = frozenset(
        public_provider_name(part.strip()) for part in raw.split(",") if part.strip()
    )
    return entries or None


def _catalog_public_provider_names() -> frozenset[str] | None:
    """Public provider names in the last-known catalog snapshot, or None.

    Membership checks run synchronously (grant guards call them directly),
    so they cannot await the live catalog; they resolve against
    ``_model_catalog_cache``, which the async catalog paths keep current.
    ``None`` means no catalog is known yet and resolution falls back to
    plain case-insensitive matching.
    """
    cache = _model_catalog_cache
    if cache is None:
        return None
    return frozenset(
        public_provider_name(provider)
        for entry in cache
        if isinstance(provider := entry.get("provider"), str) and provider
    )


def is_server_provider_active(provider: str) -> bool:
    """Whether server credentials may serve this provider (empty setting = all).

    Both sides of the comparison go through :func:`public_provider_name`:
    admins save public picker names (``claude``) while catalog and grant paths
    hold raw provider IDs (``cli-claude``) — one canonical form matches both.
    Saved entries are resolved against the current catalog before matching:
    an exact match wins, a unique case-insensitive match resolves to that
    catalog provider, and an ambiguous match (two catalog providers differing
    only in case, e.g. ``Foo`` and ``foo`` registered as distinct providers)
    stays unresolved so selecting one never activates the other.  Only when
    the queried provider's case-insensitive match is unambiguous does the
    saved spelling's case stop mattering (``mygateway`` vs ``MyGateway``);
    display spelling is never affected.
    """
    entries = _active_server_provider_entries()
    if entries is None:
        return True
    query = public_provider_name(provider)
    catalog = _catalog_public_provider_names()
    resolved: set[str] = set()
    by_fold: dict[str, set[str]] = {}
    if catalog is not None:
        for name in catalog:
            by_fold.setdefault(name.lower(), set()).add(name)
        for entry in entries:
            if entry in catalog:
                resolved.add(entry)
            elif len(matches := by_fold.get(entry.lower(), ())) == 1:
                resolved.add(next(iter(matches)))
    if query in resolved:
        return True
    # Two or more catalog providers differing only in case are distinct
    # providers: a case-insensitive match cannot tell them apart, so only
    # the exact (resolved) match above may activate the queried one.
    if len(by_fold.get(query.lower(), ())) > 1:
        logger.debug(
            "active_server_providers: case-ambiguous provider=%s requires exact match",
            provider,
        )
        return False
    return query.lower() in {entry.lower() for entry in entries}


# The two variable sentences rootcoz raises for a deactivated provider and a
# deselected model, as templates: the chat mapper matches them whole (see
# ``_is_rootcoz_actionable_error``), so the wording has exactly one source.
_INACTIVE_PROVIDER_TEMPLATE = Template(
    "Server credentials for provider '$provider' are deactivated "
    "by the administrator (active_server_providers)"
)
_EXCLUDED_MODEL_TEMPLATE = Template(
    "Model '$model' is excluded from server credentials for "
    "provider '$provider' by the administrator's model selection"
)


def _raise_inactive_server_provider(provider: str) -> None:
    raise InactiveServerProviderError(
        _INACTIVE_PROVIDER_TEMPLATE.substitute(provider=provider)
    )


class ExcludedServerModelError(ValueError):
    """Model deselected from server credentials (admin model selection)."""


# Managed providers (lowercase) and their selected (provider, model) pairs.
_ServerPoolSelection = tuple[set[str], set[tuple[str, str]]]


async def _load_server_pool_selection() -> _ServerPoolSelection | None:
    """Managed server providers and their selected pairs; ``None`` on failure.

    A provider is *managed* when it has a stored model selection or a stored
    server key (#347): even with an empty selection (every model deselected)
    the server pool stays scoped — "no rows" must mean "nothing exposed",
    not "never scoped", or deselecting every model would re-expose the whole
    catalog. Shared by :func:`scoped_models` discovery filtering and the
    :func:`require_selected_server_pair` authorization check so both always
    agree on what the server pool exposes.
    """
    selected_pairs: set[tuple[str, str]] = set()
    selected_providers: set[str] = set()
    try:
        from rootcoz.storage import (
            list_server_ai_providers,
            list_server_provider_models,
        )

        for row in await list_server_provider_models():
            selected_providers.add(row["provider"].lower())
            selected_pairs.add((row["provider"].lower(), row["model"]))
        for row in await list_server_ai_providers():
            selected_providers.add(row["provider"].lower())
    except sqlite3.Error, OSError:
        logger.warning("Unable to load server provider model selection")
        return None
    return selected_providers, selected_pairs


async def require_selected_server_pair(provider: str, model: str) -> None:
    """Reject server-credential use of an admin-deselected model (#347).

    :func:`scoped_models` already hides deselected pairs from discovery,
    but a client sending the raw catalog pair must be re-checked here so
    every server-source call and chat session creation refuses it. The
    restriction scopes the server credential pool only — user-key calls
    never pass through here and stay allowed.
    """
    selection = await _load_server_pool_selection()
    if selection is None:
        # Storage unavailable: discovery and the grant checks degrade the
        # same way rather than failing every AI call.
        return
    selected_providers, selected_pairs = selection
    if (
        provider.lower() in selected_providers
        and (provider.lower(), model) not in selected_pairs
    ):
        raise ExcludedServerModelError(
            _EXCLUDED_MODEL_TEMPLATE.substitute(model=model, provider=provider)
        )


# Cached settings picker options (monotonic_ts, provider list) — success and
# failure alike, so a sidecar outage costs one attempt, not one per page load.
_server_capable_cache: tuple[float, list[str]] | None = None
_SERVER_CAPABLE_CACHE_TTL_SEC = 60.0


def _invalidate_server_capable_cache() -> None:
    """Drop cached settings picker options whenever the catalog changes."""
    global _server_capable_cache
    _server_capable_cache = None


async def server_capable_providers() -> list[str]:
    """Provider IDs with server-credential models, for admin settings options.

    Both success and failure are cached for a short TTL: an unreachable
    sidecar must not slow (or queue behind the catalog lock on) every
    admin settings page load. Only public names are ever returned —
    internal ``cli-*``/``acpx-*`` catalog IDs map through
    :func:`public_provider_name`, collapsing to one entry per name.
    """
    global _server_capable_cache
    now = time.monotonic()
    if (
        _server_capable_cache is not None
        and now - _server_capable_cache[0] < _SERVER_CAPABLE_CACHE_TTL_SEC
    ):
        return _server_capable_cache[1]
    try:
        catalog = await _get_model_catalog()
        providers = sorted(
            {public_provider_name(p) for p in build_friendly_catalog(catalog)}
        )
    except httpx.HTTPError, OSError, RuntimeError, ValueError:
        logger.warning("Unable to list server-capable providers for settings")
        providers = []
    _server_capable_cache = (now, providers)
    return providers


async def require_server_provider_grant(provider: str = "", model: str = "") -> None:
    """Recheck the initiator on every server AI operation, including resumed turns.

    A user without the full server grant may still run one admin-declared
    free provider+model pair when they hold the free-providers grant.
    """
    if provider and not is_server_provider_active(provider):
        logger.debug(
            "Server credentials for provider=%s deactivated by active_server_providers",
            provider,
        )
        _raise_inactive_server_provider(provider)
    if provider and model:
        # Admin-deselected models are not runnable on server credentials,
        # regardless of grant — the selection is the server pool (#347).
        await require_selected_server_pair(provider, model)
    from rootcoz.storage import (
        can_user_use_free_providers,
        can_user_use_server_providers,
    )

    username = ai_username.get()
    try:
        allowed = bool(username) and await can_user_use_server_providers(username)
    except Exception as exc:
        raise ValueError("Server provider access unavailable") from exc
    if allowed:
        return
    if provider and model:
        try:
            granted = bool(username) and await can_user_use_free_providers(username)
        except Exception as exc:
            raise ValueError("Server provider access unavailable") from exc
        if granted and await is_free_ai_pair(provider, model):
            return
    raise ValueError("Server provider access requires an administrator grant")


async def is_free_ai_pair(provider: str, model: str) -> bool:
    """True when the admin declared this exact provider+model pair free.

    Provider matching is case-insensitive (stored spelling may differ in
    case from the catalog); the model id is exact.
    """
    from rootcoz.storage import list_free_ai_models

    try:
        rows = await list_free_ai_models()
    except sqlite3.Error, OSError:
        return False
    wanted = (provider or "").lower()
    return any(
        (row["provider"] or "").lower() == wanted and row["model"] == model
        for row in rows
    )


async def session_key(provider: str) -> str | None:
    """Resolve the current initiator's exact provider key; missing means server auth."""
    username = ai_username.get()
    if not username:
        return None
    from rootcoz.storage import get_user_ai_credential_with_generation

    _selected_key_generation.set(None)
    key, generation = await get_user_ai_credential_with_generation(username, provider)
    if key is not None:
        if provider not in await supported_key_providers():
            raise ValueError("Provider session API-key capability unavailable")
        if generation is None:
            raise ValueError("AI credential generation unavailable")
        _selected_key_generation.set((username, provider, key, generation))
        logger.info("Using user AI credential for provider=%s", provider)
    return key


def _deployment_provider_key(provider: str) -> str | None:
    """Deployment-env/ambient key of a provider with no stored credential."""
    credentials = deployment_provider_credentials()
    if credentials is None:
        # Manifest unreadable or catalog still cold: unknown, so leave the
        # session on the sidecar's own env bootstrap.
        return None
    wanted = provider.lower()
    return next(
        (key for name, key in credentials.items() if name.lower() == wanted), None
    )


async def server_provider_key(provider: str) -> str | None:
    """Server-side provider key for a session (#347, #376), or None.

    A stored key wins over the deployment's env/ambient credentials and is
    passed as a per-session apiKey exactly like a user key. With no stored key
    the provider's deployment credential is resolved instead (#376): a model
    captured from an ambient ``<PROVIDER>_API_KEY`` exists only for that key,
    so the session must authenticate with it — otherwise the sidecar cannot
    construct an id the static catalog lacks and rejects the session.
    Unreadable or unreachable storage falls back to the env bootstrap with a
    warning — the deployment credentials still authenticate the call.

    The returned key is in-memory only: never logged, persisted, or returned
    by an API.
    """
    from rootcoz.storage import UnreadableAiCredentialsError, get_server_ai_credential

    try:
        key = await get_server_ai_credential(provider)
    except UnreadableAiCredentialsError:
        logger.warning(
            "Server AI credential store unreadable for provider=%s; "
            "using deployment credentials",
            provider,
        )
        return None
    except sqlite3.Error, OSError, RuntimeError:
        logger.warning("Unable to read server AI credential for provider=%s", provider)
        return None
    return key if key is not None else _deployment_provider_key(provider)


# Pi-sidecar's catalog is the provider/model contract.  These aliases only
# preserve unambiguous legacy spelling; friendly provider names are resolved
# against the selected model below and never choose a default route.
_LEGACY_PROVIDER_ALIASES: dict[str, str] = {
    "cursor-cli": "cli-cursor",
    "claude-cli": "cli-claude",
    "gemini-cli": "cli-gemini",
}

# Friendly provider names and the exact catalog providers each may resolve to.
# One table serves route validation (:func:`resolve_catalog_pair`) and the
# reverse mapping a public surface needs: the API only ever exposes
# ``claude``/``gemini``/``cursor``, never a ``cli-*`` catalog ID.
_LEGACY_PROVIDER_TARGETS: dict[str, tuple[str, ...]] = {
    "gemini": ("google", "cli-gemini"),
    "claude": ("google-vertex-claude", "cli-claude"),
    "cursor": ("acpx-cursor", "cli-cursor"),
}
_PUBLIC_PROVIDER_NAMES: dict[str, str] = {
    provider_id: name
    for name, provider_ids in _LEGACY_PROVIDER_TARGETS.items()
    for provider_id in provider_ids
}


def public_provider_name(provider: str) -> str:
    """Return the public name of a catalog provider ID (identity if unknown).

    ``cli-claude`` -> ``claude``; ``acpx-cursor`` -> ``cursor``.  Anything the
    table does not know is already public and passes through unchanged.
    """
    return _PUBLIC_PROVIDER_NAMES.get(normalize_provider(provider), provider)


# Last successful sidecar catalog.  AI calls share this with the catalog API so
# a transient refresh failure never rejects a pair already known to be valid.
_model_catalog_cache: list[dict[str, Any]] | None = None
_model_catalog_generation = 0
_model_catalog_lock = asyncio.Lock()

# Cached cursor auth probe: (monotonic_ts, status_dict)
_cursor_auth_cache: tuple[float, dict[str, Any]] | None = None
_CURSOR_AUTH_CACHE_TTL_SEC = 60.0
# Browser `agent login` expires. CURSOR_API_KEY does NOT — when set in env it keeps working.
_CURSOR_BROWSER_LOGIN_EXPIRED_HINT = (
    "Cursor browser login (`agent login`) expired or is missing. "
    "Set CURSOR_API_KEY on the server (does not expire; always works when set), "
    "or re-run `agent login` on the host and restart the sidecar. "
    "Browser login cannot be auto-refreshed."
)
_CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT = (
    "CURSOR_API_KEY is set (that key does not expire) but Cursor models are "
    "unavailable. Check the key is visible to the sidecar process, restart "
    "the sidecar, and verify network to Cursor APIs."
)

# Every refusal sentence starts with this marker: it is the one non-retryable
# signal (engine/core.py), the prefix the send-time mapper recognises and the
# string a log grep finds.
SESSION_REFUSAL_MARKER = "AI session refused"
# The sidecar's own limits refusal, byte for byte (sessions.js:1764-1766), the
# third whitelisted literal — forwarded verbatim because it names the fix.
_SIDECAR_LIMITS_REFUSAL = (
    "Model metadata missing reliable input/output token limits for unknown model. "
    "Declare the model in the agent-dir models.json with contextWindow and maxTokens "
    "to use it with a session API key."
)
# Static, key-free sidecar refusal literals; matched by exact string (a
# whitelist) and forwarded verbatim. Anything else stays in the server log.
_SIDECAR_REFUSAL_LITERALS = (
    "Invalid model for session API key: [REDACTED]",
    "Selected provider does not support session API key authentication",
    _SIDECAR_LIMITS_REFUSAL,
)
# Picker reason only — the 422 forwards the sidecar's literal instead.
SESSION_UNSUPPORTED_REASON = (
    "AI session refused: this model is listed by the provider but is not declared in that"
    " provider's models[] and the listing reports no token limits. Declare it in the"
    " agent-dir models.json models[] — limits are optional there, pi's defaults apply —"
    " or pick a model that is declared or lists both limits."
)

# The two variable sentences ``_session_refusal`` builds, as templates. They are
# the only source of the wording: the validator below turns each into a pattern,
# so a sidecar body that merely starts with the marker can never pass as ours.
_REFUSAL_TEMPLATES = (
    Template(
        f"{SESSION_REFUSAL_MARKER}: the provider rejected the AI credential for"
        " $pair. Check that the credential is valid for that provider, or ask an"
        " administrator to verify the provider key on the server."
    ),
    Template(
        f"{SESSION_REFUSAL_MARKER}: the session could not be created for $pair"
        " (sidecar HTTP $status). Ask an administrator to check the server log for"
        " the sidecar's reason."
    ),
)
# Fixed sentences rootcoz builds whole: the whitelisted sidecar literals, the
# Cursor legacy hint, and the picker reason.
_OWN_REFUSAL_SENTENCES = (
    *(f"{SESSION_REFUSAL_MARKER}: {literal}" for literal in _SIDECAR_REFUSAL_LITERALS),
    f"{SESSION_REFUSAL_MARKER}: {_CURSOR_BROWSER_LOGIN_EXPIRED_HINT}",
    SESSION_UNSUPPORTED_REASON,
)
# Each variable bounded to the shape ``_session_refusal`` can put there, so a
# body cannot park free text in a variable slot and pass as our sentence.
# ``$status`` renders ``None`` when the body carried no status.
# shortcut: ``$pair`` is bounded to no-whitespace, upgrade only if a catalog
# provider or model id ever contains a space.
_REFUSAL_VARIABLES: dict[str, str] = {
    "pair": r"\S+/\S*",
    "status": r"(?:None|\d{3})",
    # A provider or model id: no whitespace, no quote — the delimiters the
    # templates wrap them in. Free text cannot fit the slot.
    "provider": r"[^'\s]+",
    "model": r"[^'\s]+",
}


def _refusal_pattern(template: Template) -> re.Pattern[str]:
    """Compile a refusal template, bounding every variable to its real shape."""
    parts = re.split(r"\$(\w+)", template.template)
    pattern = "".join(
        _REFUSAL_VARIABLES[part] if index % 2 else re.escape(part)
        for index, part in enumerate(parts)
    )
    return re.compile(f"^{pattern}$")


_OWN_REFUSAL_PATTERNS = tuple(_refusal_pattern(t) for t in _REFUSAL_TEMPLATES)


def _is_own_refusal_sentence(text: str) -> bool:
    """Whether ``text`` is a sentence rootcoz built, not a raw sidecar body.

    The prefix alone is not evidence: a non-200 body lands in ``AIResult.text``
    verbatim, so a body beginning with the marker must not be forwarded to a
    user or skip keyed redaction. Matching the whole sentence is what makes it
    ours.
    """
    sentence = (text or "").strip()
    return sentence in _OWN_REFUSAL_SENTENCES or any(
        pattern.match(sentence) for pattern in _OWN_REFUSAL_PATTERNS
    )


# Builtin tools for AI sessions — no bash access (MANDATORY per project rules)
# Separate constants to allow independent evolution: analysis may gain tools
# (e.g. write) that chat should never have.
# Prompts must not claim shell/git — derive browse hint from filesystem tools.
_FS_BROWSE_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep")
CHAT_BUILTIN_TOOLS: tuple[str, ...] = (*_FS_BROWSE_TOOLS, "subagent")
ANALYSIS_BUILTIN_TOOLS: tuple[str, ...] = _FS_BROWSE_TOOLS
# Prompt wording for cloned repos — must match tool policy (no shell/git).
RESOURCE_REPO_BROWSE_HINT = (
    f"browse with {', '.join(_FS_BROWSE_TOOLS[:-1])}, and {_FS_BROWSE_TOOLS[-1]} only "
    "(no shell or bash execution)"
)


def normalize_provider(provider: str) -> str:
    """Normalize provider name (lowercase + legacy *-cli aliases → canonical)."""
    p = (provider or "").lower().strip()
    return _LEGACY_PROVIDER_ALIASES.get(p, p)


def _catalog_provider_spelling(provider_ids: Any, provider: str) -> str:
    """Adopt the catalog's exact spelling for a provider ID, case-insensitively.

    ``normalize_provider()`` lowercases built-in aliases, but a custom agent-dir
    provider may be registered with mixed case (e.g. ``MyGateway``). Matching
    case-insensitively and returning the catalog's spelling keeps sessions
    working with the exact ID the sidecar registered while built-in alias
    normalization stays intact. The comparison normalizes both sides: the input
    may already carry a spelling adopted from a cached catalog, and the
    refreshed catalog may spell the same provider differently. Zero or
    ambiguous matches (two providers differing only in case) fall through
    unchanged and fail the exact pair check downstream.
    """
    wanted = provider.lower()
    matches = {
        p for p in provider_ids if isinstance(p, str) and p and p.lower() == wanted
    }
    return next(iter(matches)) if len(matches) == 1 else provider


def _source_for_sidecar(provider: str) -> str:
    if provider.startswith("cli-"):
        return "cli"
    if provider.startswith("acpx-"):
        return "acpx"
    return "api"


# Marks model IDs taken from the bundled snapshot rather than a live listing.
_SNAPSHOT_SOURCE = "snapshot"


def is_cursor_provider(provider: str) -> bool:
    """Whether a sidecar provider uses Cursor diagnostics."""
    return provider == "cursor" or provider.endswith("-cursor")


def build_friendly_catalog(
    all_models: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group the sidecar catalog by its exact provider identifiers."""
    result: dict[str, list[dict[str, Any]]] = {}
    for entry in all_models:
        provider, model = entry.get("provider"), entry.get("id")
        if not isinstance(provider, str) or not isinstance(model, str) or not model:
            continue
        result.setdefault(provider, []).append(
            {
                "id": model,
                "name": entry.get("name") or model,
                "provider": provider,
                "source": _source_for_sidecar(provider),
            }
        )
    return result


async def _get_model_catalog(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Return the shared successful sidecar catalog, refreshing only when asked."""
    global _model_catalog_cache
    if _model_catalog_cache is not None and not refresh:
        return _model_catalog_cache
    async with _model_catalog_lock:
        if _model_catalog_cache is not None and not refresh:
            return _model_catalog_cache
        generation = _model_catalog_generation
        catalog = await _list_models_raw("")
        if generation != _model_catalog_generation:
            return _model_catalog_cache if _model_catalog_cache is not None else catalog
        _model_catalog_cache = catalog
        _invalidate_server_capable_cache()
        logger.debug("Loaded Pi-sidecar model catalog: %d models", len(catalog))
        return catalog


def update_model_catalog(models: list[dict[str, Any]] | None = None) -> None:
    """Replace or clear the successful catalog after model discovery refresh."""
    global _model_catalog_cache, _model_catalog_generation
    _model_catalog_generation += 1
    _model_catalog_cache = models
    _invalidate_server_capable_cache()


async def registered_provider_ids() -> set[str] | None:
    """Exact provider IDs the sidecar currently registers, or None if unavailable.

    Uses the sidecar's provider registry (``GET /providers``), not the model
    catalog: a registered provider with no current models is still registered,
    and treating it as unregistered would wrongly consolidate a case-distinct
    credential. Credential rotation uses this to decide whether a case-variant
    spelling is a registered, distinct provider (keep the credentials separate)
    or a drifted spelling of the same one (consolidate). Fails closed: a failed
    lookup, or a registry payload with any malformed entry, returns None —
    registration is unknown, never a confirmed partial set. The caller must
    treat unknown registration as "keep separate", never as evidence that a
    spelling is unregistered.
    """
    try:
        providers = await get_sidecar_client().get_providers()
    except httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError, TypeError:
        logger.warning("Unable to list registered providers for credential update")
        return None
    entries = _provider_registry_entries(providers)
    if entries is None:
        logger.warning("Unable to list registered providers: unusable payload")
        return None
    return {entry["provider"] for entry in entries}


async def list_models(provider: str = "") -> list[dict[str, Any]]:
    """List sidecar catalog models, optionally for an exact provider ID."""
    catalog = await _get_model_catalog()
    provider = normalize_provider(provider)
    return [
        entry for entry in catalog if not provider or entry.get("provider") == provider
    ]


# Fallback model IDs, used only when the sidecar cannot list a provider for a
# user's own key (pi-sidecar#892: a multi-API builtin like OpenRouter is refused
# outright). This is a frozen copy of the pi-ai generated catalog at pi-ai 0.84.4
# — it is NOT refreshed at runtime, so it can lag real models badly. A
# model added upstream after that version is simply absent, which reads as "this
# model does not exist" even though typing it by hand works. Entries derived from
# it are therefore tagged `snapshot` so the UI can say so, and never presented as
# a verified or current listing. Refresh the file and this comment together.
_PI_MODEL_SUGGESTIONS: dict[str, list[str]] = json.loads(
    Path(__file__).with_name("pi_model_suggestions.json").read_text()
)


# Unambiguous auth phrases only. A bare word like "rejected" also occurs in
# unrelated failures ("model rejected by provider"), and blaming the key for
# those sends the admin after the wrong cause.
_AUTH_FAILURE_MARKERS: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "invalid api key": 401,
    "api key is invalid": 401,
    "incorrect api key": 401,
    "authentication failed": 401,
}

# The sidecar client flattens an HTTP status into the message text, so only the
# explicit ``(HTTP 401)``/``(HTTP 403)`` suffix is evidence — never a bare digit.
_AUTH_STATUS_SUFFIX = re.compile(r"\(http\s+(401|403)\)")


def auth_failure_status(exc: BaseException | None) -> int | None:
    """HTTP status when ``exc`` says the credential was rejected, else ``None``.

    Distinguishes a bad key from every other failure: a model-discovery error
    for an unrelated reason (unusable model, provider outage) must not be
    reported as a credential rejection. Structured evidence only — an
    ``httpx.HTTPStatusError`` still in the chain, the sidecar's explicit
    ``(HTTP 401)``/``(HTTP 403)`` suffix (``Sidecar API key model discovery
    failed (HTTP 401)``, raised ``from None``), or an unambiguous auth phrase.
    """
    seen: set[int] = set()
    error = exc
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (
            401,
            403,
        ):
            return error.response.status_code
        text = str(error).lower()
        if status := _AUTH_STATUS_SUFFIX.search(text):
            return int(status.group(1))
        for marker, status_code in _AUTH_FAILURE_MARKERS.items():
            if marker in text:
                return status_code
        error = error.__cause__ or error.__context__
    return None


async def verify_ai_key(provider: str, model: str, key: str) -> str:
    """Probe a submitted key with a real sidecar turn, without storing it."""
    client = get_sidecar_client()
    session_id = None
    outcome = "inconclusive"

    def auth_error(response: httpx.Response) -> bool:
        if response.status_code == 401:
            return True
        try:
            payload = response.json()
            error = (
                str(payload.get("error", "")).lower()
                if isinstance(payload, dict)
                else ""
            )
        except ValueError:
            return False
        return any(marker in error for marker in ("invalid_api_key", "invalid api key"))

    try:
        response = await client._client.post(
            "/sessions",
            json={
                "provider": provider,
                "model": model,
                "api_key": key,
                "system_prompt": "Reply briefly.",
                "cwd": tempfile.gettempdir(),
                "tools": [],
            },
            timeout=30,
        )
        if auth_error(response):
            outcome = "rejected"
        elif response.status_code in (200, 201):
            session_id = response.json()["session_id"]
            result = await client._client.post(
                f"/sessions/{quote(session_id, safe='')}/prompt",
                json={"message": "Say OK."},
                timeout=30,
            )
            if auth_error(result):
                outcome = "rejected"
            elif result.status_code == 200:
                data = result.json()
                if (
                    not data.get("error")
                    and isinstance(data.get("text"), str)
                    and data["text"].strip()
                ):
                    outcome = "accepted"
    except (
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as _exc:
        pass  # Transport and malformed responses cannot prove a key is bad.
    finally:
        if session_id:
            try:
                await client.delete_session(session_id)
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as _exc:
                logger.warning("Unable to clean up credential verification session")
        logger.info(
            "AI credential verification: provider=%s outcome=%s", provider, outcome
        )
    return outcome


async def models_for_api_key(provider: str, api_key: str) -> dict[str, Any]:
    """Ask Pi for key-scoped models and listing capability; never retain the key."""
    response = await get_sidecar_client().get_models_for_api_key(provider, api_key)
    if (
        not isinstance(response, dict)
        or type(response.get("modelListingSupported")) is not bool
        or not isinstance(response.get("models"), list)
    ):
        raise ValueError("Invalid key-scoped model discovery response")
    models = response["models"]
    if any(
        not isinstance(m, dict)
        or m.get("provider") != provider
        or not isinstance(m.get("id"), str)
        or not m["id"].strip()
        for m in models
    ):
        raise ValueError("Invalid key-scoped model discovery response")
    if not response["modelListingSupported"] and models:
        raise ValueError("Invalid key-scoped model discovery response")
    return {
        "modelListingSupported": response["modelListingSupported"],
        "models": [
            {
                "id": m["id"],
                "name": m.get("name") or m["id"],
                "provider": provider,
                # "listing" models come from the key's live listing;
                # "manifest" models only exist in the provider manifest
                # and are NOT key-verified (#350 r1: manifest-only models
                # must not enter the server pool as verified).
                "source": m.get("source") or "listing",
                # Keep only the metadata the sidecar uses to construct unknown models.
                "capabilities": {
                    field: m.get("capabilities", {}).get(field)
                    for field in ("inputTokenLimit", "outputTokenLimit")
                    if isinstance(m.get("capabilities"), dict)
                },
            }
            for m in models
        ],
    }


def _sidecar_agent_dir() -> Path | None:
    """Sidecar agent dir from ``PI_SIDECAR_AGENT_DIR``; ``None`` when unset.

    The sidecar resolves the same variable at start (unset: its own ephemeral
    scratch dir, which registers no custom providers), so an unset variable
    means there is no manifest to read. Rootcoz runs in the sidecar's
    container, so the deployment env that exported a referenced API key is in
    ``os.environ`` too.
    """
    path = os.environ.get("PI_SIDECAR_AGENT_DIR", "").strip()
    return Path(path) if path else None


def _env_reference_var(reference: object) -> str | None:
    """Variable name of a models.json apiKey reference (``$VAR``/``${VAR}``)."""
    if not isinstance(reference, str):
        return None
    name = reference.strip()
    if name.startswith("${") and name.endswith("}"):
        name = name[2:-1]
    elif name.startswith("$"):
        name = name[1:]
    else:
        return None
    return name if name.isidentifier() else None


# Providers whose ambient API key variable breaks the ``<ID>_API_KEY``
# convention. Each entry is tried in order, before the convention-derived name.
# shortcut: only the google/gemini aliases are mapped — upstream also accepts
# HF_TOKEN, AI_GATEWAY_API_KEY, CLOUDFLARE_API_KEY, ...; add an entry here when
# one of those providers needs ambient resolution.
_AMBIENT_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def ambient_api_key_var(provider: str) -> str:
    """Environment variable holding a provider's ambient API key.

    The convention is ``<PROVIDER_ID>_API_KEY`` with every non-alphanumeric
    character folded to ``_``: ``openai`` -> ``OPENAI_API_KEY``,
    ``my-gateway`` -> ``MY_GATEWAY_API_KEY``. Providers whose key variable
    breaks the convention are handled by :data:`_AMBIENT_KEY_ALIASES`.
    """
    name = re.sub(r"[^A-Za-z0-9]+", "_", provider).strip("_").upper()
    return f"{name}_API_KEY"


def _ambient_api_key_vars(provider: str) -> tuple[str, ...]:
    """Candidate variables for a provider, aliases first, convention last."""
    aliases = _AMBIENT_KEY_ALIASES.get(normalize_provider(provider), ())
    return tuple(dict.fromkeys((*aliases, ambient_api_key_var(provider))))


def _cached_catalog_provider_ids() -> list[str]:
    """Provider IDs in the last-known sidecar catalog, sorted (empty if none).

    Ambient credential resolution is synchronous (grant guards and page loads
    call it directly), so it resolves against ``_model_catalog_cache``, which
    the async catalog paths keep current — the same snapshot
    :func:`_catalog_public_provider_names` uses.
    """
    return sorted(
        {
            provider
            for entry in _model_catalog_cache or ()
            if isinstance(provider := entry.get("provider"), str) and provider
        }
    )


def _agent_manifest() -> dict[str, Any] | None:
    """The agent-dir ``models.json`` ``providers`` mapping, or ``None``.

    One read and one failure behaviour for every consumer: a configured agent
    dir whose manifest is missing, unreadable or unparseable is *unknown*
    (``None``), never "nothing declared". An unset ``PI_SIDECAR_AGENT_DIR``
    also answers ``None`` — there is no manifest to read.
    """
    agent_dir = _sidecar_agent_dir()
    if agent_dir is None:
        return None
    try:
        manifest = json.loads((agent_dir / "models.json").read_text())
    except (OSError, ValueError) as exc:
        logger.warning(
            "Deployment provider manifest unreadable (%s); keeping captured"
            " key-scoped listings",
            exc,
            exc_info=True,
        )
        return None
    providers = manifest.get("providers") if isinstance(manifest, dict) else None
    return providers if isinstance(providers, dict) else {}


def _is_key_scoped_provider(config: object) -> bool:
    """The one ``trustKeyScopedListing`` read."""
    return isinstance(config, dict) and config.get("trustKeyScopedListing") is True


def _manifest_session_models() -> dict[str, set[str]]:
    """Declared model ids per flagged provider: ``{lowercase provider: {ids}}``.

    Membership-only input: the value is the set of ids, so no manifest entry
    (its ``apiKey``, ``baseUrl`` or limits) ever leaves this function, and
    nothing here is logged. A flagged provider declaring no ``models[]``
    contributes no map at all — with no declared id there is nothing to
    discriminate, and one flag is never a blanket block.
    """
    declared: dict[str, set[str]] = {}
    for provider, config in (_agent_manifest() or {}).items():
        if not isinstance(provider, str) or not _is_key_scoped_provider(config):
            continue
        models = config.get("models")
        ids = {
            entry["id"]
            for entry in (models if isinstance(models, list) else ())
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry["id"]
        }
        if ids:
            declared[provider.lower()] = ids
    return declared


def _entry_limits(entry: Mapping[str, Any]) -> tuple[int, int] | None:
    """The entry's captured ``(input, output)`` token limits, when both are usable.

    ``None`` for absent, partial, non-integer or out-of-range limits. The one
    limits predicate: the pool filter and the picker mark share it and differ
    only in how they treat *absence*.
    """
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    limits = (capabilities.get("inputTokenLimit"), capabilities.get("outputTokenLimit"))
    if any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in limits):
        return None
    return cast(tuple[int, int], limits)


def _mark_session_support(
    provider: str,
    entry: dict[str, Any],
    declared: Mapping[str, set[str]],
    *,
    mark_on: dict[str, Any] | None = None,
) -> None:
    """Mark one key-scoped listing entry session-capable, or say why not.

    The sidecar's own two clauses: the id is declared in the provider's
    manifest ``models[]`` (it is then on the runtime and resolves before the
    limits check), or the captured row carries both limits. A provider with no
    declared ids and an unflagged provider are left exactly as they are.

    ``mark_on`` writes the mark on another row: a user's discovery entry holds
    the ``capabilities``, the merged pair row is what the API answers with.
    """
    allowed = declared.get(provider.lower())
    if allowed is None:
        return
    target = entry if mark_on is None else mark_on
    if entry.get("id") in allowed or _entry_limits(entry) is not None:
        target["session_supported"] = True
        # A re-mark replaces the verdict, reason included.
        target.pop("session_unsupported_reason", None)
    else:
        target["session_supported"] = False
        target["session_unsupported_reason"] = SESSION_UNSUPPORTED_REASON


def deployment_provider_credentials() -> dict[str, str] | None:
    """Deployment-env keys of the providers the server can list models with.

    Two sources, both of which leave the provider with no stored server key,
    so the add/rotate capture (#350) never runs for it — its live key-scoped
    listing has to be captured with the env-resolved key instead (#372):

    1. A custom provider whose ``models.json`` ``apiKey`` is an environment
       reference (``"$VAR"`` / ``"${VAR}"``), flagged
       ``trustKeyScopedListing``. Only flagged providers count: the flag is
       what makes the listing trustworthy for a custom host, and the sidecar
       honors it solely from the admin-controlled manifest.
    2. A sidecar-registered provider (the last-known catalog) authenticated
       from the ambient environment: the variable named by
       :func:`ambient_api_key_var` — ``openai`` with ``OPENAI_API_KEY`` set.
       A provider whose manifest entry carries an ``apiKey`` (literal or
       reference) is skipped here: that is the key the sidecar uses. Merely
       *mentioning* a provider is not declaring its key — an entry holding
       only ``modelOverrides`` (or any other key-less content) changes model
       metadata, it does not authenticate anything, so pi's own environment
       resolution still applies (#389). Cursor's ``acpx-``/``cli-`` agents are
       skipped too: they authenticate through the CLI, not through
       ``CURSOR_API_KEY``.

    Unset/empty variables and literal keys are skipped. Key material is never
    logged — provider names only, at DEBUG so a page-load read does not log
    the same names on every request.

    ``None`` means "unknown": the raw answer a caller must not act on. It is
    returned when a configured agent dir did not yield a readable manifest
    right now — the file is missing (atomic replace, mount blip), unreadable,
    or unparseable — and also while the catalog is cold (``None``), since
    ambient eligibility is then unknown rather than empty: startup, a refresh
    in flight, or a failed ``list_models()`` must never look like "no provider
    is eligible" (#376). Unknown is never "nothing is eligible": a caller must
    keep whatever it captured before, so ambient resolution is skipped too
    rather than reporting an empty-capture pass. An empty dict is a
    successful read of a warm catalog that resolved no credential.
    """
    if _model_catalog_cache is None:
        # Only a warm catalog may produce an ambient-derived map (#376): an
        # unwarmed one cannot answer the ambient half of this resolve.
        logger.debug(
            "Sidecar model catalog not loaded yet; deployment credentials unknown"
        )
        return None
    resolved: dict[str, str] = {}
    declared: set[str] = set()
    agent_dir = _sidecar_agent_dir()
    if agent_dir is not None:
        providers = _agent_manifest()
        if providers is None:
            # A configured agent dir without a readable manifest is unknown,
            # not "no eligible providers" (#376): the caller would sweep every
            # captured deployment listing away over a transient blip.
            return None
        for provider, config in providers.items():
            if not isinstance(provider, str) or not isinstance(config, dict):
                continue
            # Only an entry that supplies a key owns it; a key-less entry
            # (``modelOverrides`` only) leaves the key to the ambient env.
            if config.get("apiKey"):
                declared.add(provider.lower())
            if not _is_key_scoped_provider(config):
                continue
            var = _env_reference_var(config.get("apiKey"))
            key = os.environ.get(var, "").strip() if var else ""
            if not key:
                logger.debug(
                    "Deployment credential unavailable for provider=%s (%s unset)",
                    provider,
                    var or "no env reference",
                )
                continue
            resolved[provider] = key
    ambient: list[str] = []
    for provider in _cached_catalog_provider_ids():
        # Cursor's agents authenticate through acpx / the CLI login, so
        # CURSOR_API_KEY is not an ambient listing credential — out of scope.
        if provider.lower() in declared or is_cursor_provider(
            normalize_provider(provider)
        ):
            continue
        for var in _ambient_api_key_vars(provider):
            key = os.environ.get(var, "").strip()
            if key:
                resolved[provider] = key
                ambient.append(provider)
                break
    if ambient:
        logger.debug(
            "Ambient deployment credentials resolved for providers: %s", sorted(ambient)
        )
    logger.debug("Deployment credentials resolved for providers: %s", sorted(resolved))
    return resolved


def _listing_entry_usable(entry: dict[str, Any]) -> bool:
    """A discovered entry is usable unless it declares broken capability limits.

    pi-sidecar reports no capabilities for key-scoped discovery, so absence
    must stay usable. A limit that is declared but partial, non-integer, or
    beyond the safe range breaks token math and must not reach the pool.
    """
    limits = entry.get("capabilities")
    limits = limits if isinstance(limits, Mapping) else {}
    declared = [limits.get(field) for field in ("inputTokenLimit", "outputTokenLimit")]
    if all(value is None for value in declared):
        return True
    # Any declared limit must be a complete, valid pair — the same bounds
    # _entry_limits applies (absence is the only thing they disagree on).
    return _entry_limits(entry) is not None


async def scoped_models() -> dict[str, list[dict[str, Any]]]:
    """Return models usable by the active user or server, with source per pair."""
    catalog = await _get_model_catalog()
    status: dict[str, dict[str, bool | int]] = {}
    model_listing_status.set(status)
    from rootcoz.storage import can_user_use_server_providers

    try:
        allowed = (
            await can_user_use_server_providers(ai_username.get())
            if ai_username.get()
            else False
        )
    except sqlite3.Error, OSError, RuntimeError, ValueError, KeyError, TypeError:
        allowed = False
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    active = _active_server_provider_entries()
    if active is not None:
        logger.debug("active_server_providers filtering active: %s", sorted(active))
    # Admin-managed server models (#347): a managed provider (stored
    # selection or stored server key) exposes only its selected models on
    # server credentials; a never-managed provider keeps its full catalog
    # (legacy behavior, e.g. ambient-auth providers).
    selection = await _load_server_pool_selection()
    if selection is None:
        selection = (set(), set())
    selected_providers, selected_pairs = selection
    # Key-scoped listings captured at add/rotate (#350): for a keyed provider
    # the stored listing replaces the static catalog as the server pool's
    # model set — a restricted key must not expose models it cannot use, and
    # may offer models the catalog lacks. Providers with no rows (listing
    # unsupported, discovery failed, or pre-#350 rows) keep the catalog.
    key_scoped: dict[str, dict[str, dict[str, Any]]] = {}
    # Stored spelling per lowercase join key (#350 r2): fallback entries for
    # providers the catalog no longer lists must keep the stored casing —
    # pi-config looks up session providers by their exact id, so emitting
    # ``mygateway`` for a stored ``MyGateway`` breaks session creation.
    key_scoped_ids: dict[str, str] = {}
    # Declared ids per flagged provider: the picker mark's manifest half (§#423).
    # Computed once per call, and empty when the manifest is not readable.
    declared_session_models = _manifest_session_models()
    try:
        from rootcoz.storage import list_server_provider_key_models

        for row in await list_server_provider_key_models():
            key_scoped.setdefault(row["provider"].lower(), {})[row["model"]] = row
            key_scoped_ids.setdefault(row["provider"].lower(), row["provider"])
    except sqlite3.Error, OSError:
        logger.warning("Unable to load key-scoped server provider models")

    def _key_scoped_entries(
        provider: str, scoped_rows: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Server-pool entries for one provider's captured key-scoped listing.

        The empty-string sentinel row (a supported listing with zero
        #350-accessible models) yields no entries — the provider exposes
        nothing on server credentials rather than falling back to the
        catalog. Entries with declared-but-broken capability limits are
        skipped, the same rule the user-key path applies.
        """
        entries = [
            {
                "id": model,
                "name": row.get("name") or model,
                "provider": provider,
                "source": _source_for_sidecar(provider),
                "capabilities": row.get("capabilities") or {},
            }
            for model, row in sorted(scoped_rows.items())
            if model and _listing_entry_usable(row)
        ]
        for entry in entries:
            _mark_session_support(provider, entry, declared_session_models)
        return entries

    seen_catalog_providers: set[str] = set()
    for provider, entries in build_friendly_catalog(catalog).items():
        seen_catalog_providers.add(provider.lower())
        if active is not None and not is_server_provider_active(provider):
            # Server credential source withdrawn; user-key discovery below
            # can still surface this provider's models with 'user' only.
            continue
        has_selection = provider.lower() in selected_providers
        scoped_rows = key_scoped.get(provider.lower())
        if scoped_rows is not None:
            entries = _key_scoped_entries(provider, scoped_rows)
        for entry in entries:
            if has_selection and (provider.lower(), entry["id"]) not in selected_pairs:
                # Admin deselected this model from server exposure; user-key
                # discovery below can still surface it with 'user' only.
                continue
            pairs[(provider, entry["id"])] = {
                **entry,
                "credential_sources": ["server"],
                "can_use_server_providers": allowed,
                "verified": True,
            }
    # A captured listing for a provider the static catalog no longer lists
    # still defines its server pool — the admin API offers those models, so
    # discovery must not silently drop the provider (#350 r1).
    # The stored spelling is the provider id (#350 r2): the catalog join
    # key is lowercase, but the emitted entries must carry the exact id
    # pi-config resolves sessions with.
    for provider_lower, scoped_rows in key_scoped.items():
        if provider_lower in seen_catalog_providers:
            continue
        provider = key_scoped_ids.get(provider_lower, provider_lower)
        if active is not None and not is_server_provider_active(provider):
            continue
        has_selection = provider_lower in selected_providers
        for entry in _key_scoped_entries(provider, scoped_rows):
            if has_selection and (provider_lower, entry["id"]) not in selected_pairs:
                continue
            pairs[(provider, entry["id"])] = {
                **entry,
                "credential_sources": ["server"],
                "can_use_server_providers": allowed,
                "verified": True,
            }
    if ai_username.get():
        from rootcoz.storage import get_user_ai_credentials

        credentials = await get_user_ai_credentials(ai_username.get())
        supported = await supported_key_providers() if credentials else []
        for provider, key in credentials.items():
            if provider not in supported:
                continue
            try:
                discovery = await models_for_api_key(provider, key)
            except httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError:
                logger.warning(
                    "Key-scoped model discovery unavailable for provider=%s", provider
                )
                status[provider] = {
                    "has_api_key": True,
                    "modelListingSupported": True,
                    "unavailable": True,
                }
                continue
            listing = discovery["modelListingSupported"]
            status[provider] = {"has_api_key": True, "modelListingSupported": listing}
            # Without a listing the only IDs we can offer come from the frozen
            # snapshot, so tag them as such. `api`/`cli`/`acpx` would claim these
            # came from a live source and hide how old they are.
            entries = (
                discovery["models"]
                if listing
                else [
                    {
                        "provider": provider,
                        "id": model,
                        "name": model,
                        "source": _SNAPSHOT_SOURCE,
                        "verified": False,
                    }
                    for model in _PI_MODEL_SUGGESTIONS.get(provider, ())
                ]
            )
            if not listing:
                pairs.setdefault((provider, ""), {})  # Preserve manual-only provider.
            # Distinct accepted IDs: the catalog keys on (provider, id), so a
            # discovery response repeating an ID must not inflate the count.
            usable: set[tuple[str, str]] = set()
            for entry in entries:
                pair = (provider, entry["id"])
                # pi-sidecar reports no capabilities for key-scoped discovery, so
                # absence must stay usable. A limit that is declared but partial,
                # non-integer, or beyond the safe range still breaks token math.
                unusable = not _listing_entry_usable(entry)
                if listing and not (pair in pairs or not unusable):
                    continue
                if pair in pairs:
                    pairs[pair]["credential_sources"].insert(0, "user")
                    pairs[pair]["verified"] = True
                    target = pairs[pair]
                else:
                    pairs[pair] = {
                        "provider": provider,
                        "id": entry["id"],
                        "name": entry.get("name") or entry["id"],
                        "source": _source_for_sidecar(provider)
                        if listing
                        else _SNAPSHOT_SOURCE,
                        "credential_sources": ["user"],
                        "can_use_server_providers": allowed,
                        "verified": listing,
                    }
                    target = pairs[pair]
                if listing:
                    # The user's own listing ranks first, so its mark replaces
                    # the server capture's for this pair — and its entry is the
                    # one that carries the limits. Snapshot suggestions keep the
                    # manually-enterable path unmarked.
                    _mark_session_support(
                        provider, entry, declared_session_models, mark_on=target
                    )
                usable.add(pair)
            # Report what the key actually yielded, so a key that stored fine but
            # surfaced no usable model is visible instead of silently looking ready.
            status[provider]["model_count"] = len(usable)
    free_pairs: set[tuple[str, str]] = set()
    try:
        from rootcoz.storage import list_free_ai_models

        free_pairs = {
            (row["provider"].lower(), row["model"])
            for row in await list_free_ai_models()
        }
    except sqlite3.Error, OSError:
        logger.warning("Unable to load free model pairs")
    result: dict[str, list[dict[str, Any]]] = {}
    for (provider, model), entry in pairs.items():
        # Admin-declared free pairs are flagged regardless of credential
        # source so every consumer (badge, filters) sees one flag.
        if (provider.lower(), model) in free_pairs:
            entry["free"] = True
        bucket = result.setdefault(provider, [])
        if model:
            bucket.append(entry)
    return result


async def resolve_catalog_pair(provider: str, model: str) -> tuple[str, str]:
    """Validate an exact pair, retaining only unambiguous legacy routes."""
    provider, model = normalize_provider(provider), (model or "").strip()
    if ai_username.get():
        scoped = await scoped_models()
        status = model_listing_status.get() or {}
        provider = _catalog_provider_spelling(
            (*scoped.keys(), *status.keys()), provider
        )
        if status.get(provider, {}).get("unavailable"):
            raise ValueError(
                "Key-scoped model discovery unavailable; retry or select server credentials"
            )
        for entry in scoped.get(provider, []):
            if entry["id"] == model and (
                entry["verified"] or "server" in entry["credential_sources"]
            ):
                # scoped_models ranks the caller's own key ahead of the
                # server pool, so the first source is the one to use.
                source = entry["credential_sources"][0]
                if source == "server":
                    await require_server_provider_grant(provider, model)
                _selected_credential_source.set(source)
                return provider, model
        if provider in status and status[provider]["modelListingSupported"]:
            raise ValueError(
                f"Unknown Pi-sidecar provider/model pair: {provider}/{model}"
            )
        # Manual IDs require an affirmative no-listing response for this key.
        if (
            model
            and provider in status
            and not status[provider]["modelListingSupported"]
        ):
            _selected_credential_source.set("user")
            return provider, model
        # scoped_models withdrew this pair's server source — the
        # administrator deactivated the provider or deselected the model —
        # so nothing above could match it: say why.
        if provider in build_friendly_catalog(await _get_model_catalog()):
            if not is_server_provider_active(provider):
                _raise_inactive_server_provider(provider)
            await require_selected_server_pair(provider, model)
        raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")
    catalog = await _get_model_catalog()
    provider = _catalog_provider_spelling(
        {entry.get("provider") for entry in catalog}, provider
    )
    pairs = {(entry.get("provider"), entry.get("id")) for entry in catalog}
    if (provider, model) in pairs:
        await require_server_provider_grant(provider, model)
        _selected_credential_source.set("server")
        return provider, model

    # A stale catalog may not yet contain a newly discovered pair. Refresh once;
    # a warm catalog remains usable if that refresh is temporarily unavailable.
    catalog = await _get_model_catalog(refresh=True)
    provider = _catalog_provider_spelling(
        {entry.get("provider") for entry in catalog}, provider
    )
    pairs = {(entry.get("provider"), entry.get("id")) for entry in catalog}
    if (provider, model) in pairs:
        await require_server_provider_grant(provider, model)
        _selected_credential_source.set("server")
        return provider, model

    # Legacy friendly IDs have one explicit destination each.  Mapping only when
    # precisely one catalog provider has this model prevents google/Vertex guesses.
    targets = _LEGACY_PROVIDER_TARGETS.get(provider)
    providers_for_model = {p for p, m in pairs if p and m == model}
    matches = [p for p in providers_for_model if targets and p.lower() in targets]
    if len(providers_for_model) == 1 and len(matches) == 1:
        await require_server_provider_grant(matches[0], model)
        _selected_credential_source.set("server")
        return matches[0], model
    raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")


def _parse_agent_status_text(text: str) -> str | None:
    """Return auth reason from `agent status` output, or None if looks OK."""
    lower = text.lower()
    if any(
        s in lower
        for s in (
            "authentication required",
            "not authenticated",
            "not logged in",
            "please run 'agent login'",
            'please run "agent login"',
            "agent login' first",
        )
    ):
        return "auth_expired"
    if "logged in" in lower or "authenticated" in lower:
        return None
    return "unavailable"


async def probe_cursor_auth(
    *, force: bool = False, model_count: int | None = None
) -> dict[str, Any]:
    """Probe Cursor CLI/ACPX auth health for admin UI.

    Browser ``agent login`` expires and cannot be auto-refreshed.
    ``CURSOR_API_KEY`` does **not** expire — when set in the server/sidecar
    env it keeps working. Prefer the API key for Dev/prod.

    Args:
        force: Bypass the in-process probe cache.
        model_count: When provided (e.g. from a concurrent ``list_models``
            call), skip a second sidecar model enumeration.

    Returns dict: ok, reason, hint, has_api_key, model_count.
    """
    global _cursor_auth_cache
    now = time.monotonic()
    if (
        not force
        and model_count is None
        and _cursor_auth_cache is not None
        and (now - _cursor_auth_cache[0]) < _CURSOR_AUTH_CACHE_TTL_SEC
    ):
        return dict(_cursor_auth_cache[1])

    # When the caller already enumerated models, prefer that count over a
    # cached probe that may reflect a previous catalog size.
    if (
        not force
        and model_count is not None
        and _cursor_auth_cache is not None
        and (now - _cursor_auth_cache[0]) < _CURSOR_AUTH_CACHE_TTL_SEC
        and _cursor_auth_cache[1].get("model_count") == model_count
    ):
        return dict(_cursor_auth_cache[1])

    has_api_key = bool(os.environ.get("CURSOR_API_KEY", "").strip())
    if model_count is None:
        models = await list_models()
        model_count = sum(
            is_cursor_provider(str(model.get("provider", ""))) for model in models
        )
    if model_count > 0:
        status: dict[str, Any] = {
            "ok": True,
            "reason": None,
            "hint": None,
            "has_api_key": has_api_key,
            "model_count": model_count,
        }
        _cursor_auth_cache = (now, status)
        return dict(status)

    reason = "no_models"
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            reason = "unavailable"
            logger.warning("Cursor auth probe: agent status timed out")
        else:
            text = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(
                errors="replace"
            )
            parsed = _parse_agent_status_text(text)
            if parsed:
                reason = parsed
            elif proc.returncode not in (0, None):
                reason = "unavailable"
            logger.info(
                "Cursor auth probe: models=0 reason=%s returncode=%s has_api_key=%s",
                reason,
                proc.returncode,
                has_api_key,
            )
    except FileNotFoundError:
        reason = "agent_missing"
        logger.warning("Cursor auth probe: agent binary not found on PATH")
    except Exception:
        reason = "unavailable"
        logger.warning("Cursor auth probe failed", exc_info=True)

    # Empty catalog without API key → browser login likely expired.
    # CURSOR_API_KEY never expires; if key is set, do not label auth_expired.
    if reason == "no_models" and not has_api_key:
        reason = "auth_expired"
    if reason == "auth_expired" and has_api_key:
        reason = "api_key_not_applied"

    if has_api_key:
        hint = _CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT
    else:
        hint = _CURSOR_BROWSER_LOGIN_EXPIRED_HINT

    status = {
        "ok": False,
        "reason": reason,
        "hint": hint,
        "has_api_key": has_api_key,
        "model_count": model_count,
    }
    _cursor_auth_cache = (now, status)
    return dict(status)


def clear_cursor_auth_cache() -> None:
    """Clear cached cursor auth probe (e.g. after model refresh)."""
    global _cursor_auth_cache
    _cursor_auth_cache = None


class SessionCreationRefused(RuntimeError):
    """A sidecar 4xx refusal at session creation, carrying its mapped sentence."""


def _is_sessions_creation_failure(text: str) -> bool:
    """Match httpx's keyless ``Client error '4xx …' for url '…/sessions'`` text.

    The URL is matched inside the message — ``str(httpx.HTTPStatusError)`` is two
    lines — and ``[^']*`` cannot cross the closing quote, so a 4xx on
    ``/sessions/<id>/prompt`` is never a creation failure.
    """
    return "client error '4" in text.casefold() and bool(
        re.search(r"for url '[^']*/sessions'", text)
    )


def _is_rate_limited(text: str) -> bool:
    """Whether a failure is a 429, the one transient status a refusal never is.

    Rate limiting resolves itself, so it must keep the retry classifier
    retrying (``engine.core._NON_RETRYABLE_AI_ERRORS``): it is never admitted as
    a refusal, whatever shape it arrives in.
    """
    return bool(re.search(r"(?:Client error '|HTTP )429\b", text))


def _is_refusal_text(text: str) -> bool:
    """The one admission predicate: the three shapes a refusal can take.

    The marker (our own sentence), the library's **whole** creation prefix
    (never the bare ``(HTTP 4`` form, which health, model-discovery and prompt
    bodies carry too), or the keyless httpx shape. Shared by the analysis
    matcher and the chat-send mapper so the two cannot classify differently.
    A 429 is excluded: a transient limit is not a refusal.
    """
    text = text or ""
    if _is_rate_limited(text):
        return False
    return (
        text.lstrip().startswith(SESSION_REFUSAL_MARKER)
        or "Sidecar session creation failed (HTTP 4" in text
        or _is_sessions_creation_failure(text)
    )


def _sidecar_failure_parts(
    *, status: int | None = None, text: str = ""
) -> tuple[int | None, str]:
    """Split a sidecar failure into ``(status, detail)``.

    ``detail`` is internal-only: every caller compares it against
    ``_SIDECAR_REFUSAL_LITERALS`` and then drops it — the raw-text fallback can
    hold a whole response body, so it is never logged, stored or returned.
    """
    text = (text or "").strip()
    if status is None:
        found = re.search(r"Client error '(\d{3})", text) or re.search(
            r"HTTP (\d{3})\)", text
        )
        status = int(found.group(1)) if found else None
    try:
        parsed = json.loads(text)
    except ValueError, TypeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
        return status, parsed["error"]
    detail = text.removeprefix(f"Sidecar session creation failed (HTTP {status}): ")
    # A sentence rootcoz already built is re-derived from its own cause text, so
    # mapping one twice stays byte-identical instead of double-prefixing it.
    return status, detail.removeprefix(f"{SESSION_REFUSAL_MARKER}: ")


def _session_refusal(status: int | None, body: str, provider: str, model: str) -> str:
    """Build the refusal sentence for a cause; the body itself is never echoed.

    One sentence, one audience: nothing here depends on whose credential failed,
    because a job's stored error is read by viewers who are not the caller.
    """
    if body in _SIDECAR_REFUSAL_LITERALS:
        return f"{SESSION_REFUSAL_MARKER}: {body}"
    pair = f"{provider}/{model}"
    # The rejected credential outranks the Cursor legacy hint: a 401/403 says the
    # key itself was refused, not that a browser login expired.
    if status in (401, 403):
        return _REFUSAL_TEMPLATES[0].substitute(pair=pair)
    if is_cursor_provider(normalize_provider(provider)):
        return f"{SESSION_REFUSAL_MARKER}: {_CURSOR_BROWSER_LOGIN_EXPIRED_HINT}"
    return _REFUSAL_TEMPLATES[1].substitute(pair=pair, status=status)


def _session_refusal_for(
    exc: httpx.HTTPStatusError, provider: str, model: str
) -> SessionCreationRefused | None:
    """Map a sidecar 4xx at session creation; ``None`` for any other status.

    A 429 is excluded: rate limiting is transient, so the call is left
    retryable instead of turning a temporary limit into a permanent refusal.
    """
    status = exc.response.status_code
    if not 400 <= status < 500 or status == 429:
        return None
    derived, detail = _sidecar_failure_parts(status=status, text=exc.response.text)
    return SessionCreationRefused(_session_refusal(derived, detail, provider, model))


async def _create_with_refusal_mapping(
    create: Callable[[], Awaitable[str]], provider: str, model: str
) -> str:
    """Create a session, mapping a sidecar 4xx to a user-actionable refusal."""
    try:
        return await create()
    except httpx.HTTPStatusError as exc:
        refusal = _session_refusal_for(exc, provider, model)
        if refusal is None:
            raise
    # Built inside the except, raised outside it: __context__ would keep the body.
    raise refusal


# Status-free failures rootcoz writes itself. Each sentence *is* the recovery
# step, so the chat mapper forwards it verbatim; anything else without a status
# is a provider/library text and is replaced instead of echoed.
_ROOTCOZ_ACTIONABLE_ERRORS = (
    "AI session unavailable",
    "Chat credential unavailable; Start a new chat",
    "Server provider access requires an administrator grant",
)
# The two variable sentences, matched whole with every slot bounded: a sidecar
# body that merely *starts* like one of them is not ours, so it is replaced
# instead of being forwarded to a chat user.
_ROOTCOZ_ACTIONABLE_PATTERNS = (
    _refusal_pattern(_INACTIVE_PROVIDER_TEMPLATE),
    _refusal_pattern(_EXCLUDED_MODEL_TEMPLATE),
)


def _is_rootcoz_actionable_error(text: str) -> bool:
    """Whether a status-free failure is one of rootcoz's own recovery messages.

    Only a whole sentence counts: the raised sentences are templates matched
    with bounded slots, so a body wearing one of their prefixes matches no shape.
    """
    return text in _ROOTCOZ_ACTIONABLE_ERRORS or any(
        pattern.match(text) for pattern in _ROOTCOZ_ACTIONABLE_PATTERNS
    )


def format_chat_ai_user_error(
    response_text: str,
    *,
    is_admin: bool = False,
    ai_provider: str = "",
    ai_model: str = "",
) -> str:
    """Map raw sidecar/AI errors to user-friendly chat messages.

    Avoid matching the URL path ``/sessions`` as a lost chat session.
    Cursor-specific remediation applies only to Cursor sidecar providers
    (or Cursor-only markers like ``agent login`` / ``cursor_api_key`` appear).
    Credential-state hints (whether ``CURSOR_API_KEY`` is set) are admin-only.

    A session refusal is delegated to :func:`_session_refusal`, so the
    start-time ``422`` and this send-time message carry byte-identical
    sentences. Output is always a sentence rootcoz constructs: the text this
    was handed selects a cause and is then dropped, never echoed.
    """
    text = (response_text or "").strip()
    lower = text.lower()
    if not text:
        return "AI call failed. Please try again."

    friendly = normalize_provider(ai_provider)
    cursor_only_markers = ("agent login", "cursor_api_key")
    generic_auth_markers = (
        "authentication required",
        "not authenticated",
        "not logged in",
    )
    is_cursor_marker = any(s in lower for s in cursor_only_markers)
    is_generic_auth = any(s in lower for s in generic_auth_markers)

    if is_cursor_marker or (is_generic_auth and is_cursor_provider(friendly)):
        if not is_admin:
            return "Cursor is unavailable. Contact an administrator."
        has_api_key = bool(os.environ.get("CURSOR_API_KEY", "").strip())
        if has_api_key:
            return _CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT
        return _CURSOR_BROWSER_LOGIN_EXPIRED_HINT

    if is_generic_auth:
        label = friendly or "AI"
        if not is_admin:
            return f"{label} authentication failed. Contact an administrator."
        return (
            f"{label} authentication failed. Check provider credentials in "
            "Server Settings → AI."
        )

    # True lost-session cases from sidecar ("Session <id> not found"), not URL paths
    if _is_lost_session_error(lower):
        return "AI session expired. Please try sending your message again."

    # A sentence rootcoz already built is final: re-deriving its cause would
    # lose the status a 401 sentence no longer carries. Never the body — only a
    # marker sentence, which this module constructed.
    if _is_own_refusal_sentence(text):
        return text
    # A creation refusal: the admitted text selects a cause and is dropped, and
    # the admission is the one predicate, so a mid-chat prompt 4xx, a health or
    # a discovery 4xx is not dressed up as "the session could not be created".
    status, detail = _sidecar_failure_parts(text=text)
    if detail in _SIDECAR_REFUSAL_LITERALS or _is_refusal_text(text):
        return _session_refusal(status, detail, ai_provider, ai_model)
    # A status-free failure rootcoz wrote itself carries the fix — start a new
    # chat, ask for a grant — so it must not be replaced by "please try again".
    if _is_rootcoz_actionable_error(text):
        return text
    return "AI call failed. Please try again."


def _is_lost_session_error(lower_detail: str) -> bool:
    """Match sidecar lost-session errors ("Session <id> not found"), not URL paths."""
    return "session not found" in lower_detail or (
        "not found" in lower_detail
        and "session" in lower_detail
        and "/sessions" not in lower_detail
    )


def _redact_key_echo(text: str | None, key: str) -> str | None:
    """Remove predictable renderings of a key from successful AI output."""
    if text is None or not key:
        return text
    # Legacy short keys are not safe for global replacement; never expose their output.
    if len(key) < 8:
        return "[REDACTED]"
    encoded = key.encode()
    variants = {
        key,
        quote(key, safe=""),
        quote(key, safe="").lower(),
        json.dumps(key, ensure_ascii=True)[1:-1],
        base64.b64encode(encoded).decode(),
        base64.urlsafe_b64encode(encoded).decode(),
    }
    variants |= {v.rstrip("=") for v in variants if v.endswith("=")}
    variants |= {v.replace("/", "\\/") for v in variants if "/" in v}
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


async def _call_with_safe_error(
    sidecar_call: Any, *args: Any, redact_key: str | None = None, **kwargs: Any
) -> AIResult:
    """Keep sidecar credential-bearing errors out of caller logs and tracebacks."""
    try:
        result = await sidecar_call(*args, **kwargs)
        # Already our sentence (a refusal raised inside the call): re-deriving its
        # cause here would drop the status a 401 sentence no longer carries and
        # answer "sidecar HTTP None".
        if not result.success and _is_own_refusal_sentence(result.text or ""):
            return result
        # A session refusal keeps its sentence: checked before the ``redact_key is
        # None`` early return, which would otherwise leave the library's text and
        # before the keyed branch, which would blank it to "AI call failed".
        if not result.success and _is_refusal_text(
            f"{result.text or ''} {result.error or ''}"
        ):
            status, detail = _sidecar_failure_parts(text=result.text or "")
            sentence = _session_refusal(
                status,
                detail,
                str(kwargs.get("ai_provider") or ""),
                str(kwargs.get("ai_model") or ""),
            )
            return replace(result, text=sentence, error=sentence)
        if redact_key is None:
            return result
        # dataclasses.replace() drops non-field attributes — keep the
        # transcript correlation id across the copy.
        transcript_session_id = getattr(result, "transcript_session_id", None)
        if result.success:
            result = replace(
                result,
                text=_redact_key_echo(result.text, redact_key),
                error=_redact_key_echo(result.error, redact_key),
            )
            if transcript_session_id is not None:
                result.transcript_session_id = transcript_session_id
            return result
        # Encoded keys (URL, JSON, base64) and rotated keys cannot be safely
        # removed by literal replacement. Keep only fixed retry/auth signals.
        detail = f"{result.text} {result.error or ''}".lower()
        text = (
            "session not found"
            if _is_lost_session_error(detail)
            else "authentication required"
            if any(
                marker in detail
                for marker in (
                    "authentication required",
                    "not authenticated",
                    "not logged in",
                )
            )
            else "AI call failed"
        )
        result = replace(result, text=text, error=text if result.error else None)
        # Failed one-shot calls have already had session_id cleared by
        # cleanup — the transcript correlation id is the only link left.
        if transcript_session_id is not None:
            result.transcript_session_id = transcript_session_id
        return result
    except Exception as exc:
        if redact_key is None:
            raise
        # Raise outside the except block; __context__ would retain the raw error.
        safe_type = (
            type(exc)
            if type(exc) in (ValueError, RuntimeError, OSError, TimeoutError, TypeError)
            else RuntimeError
        )
        safe_text = f"{type(exc).__name__}: sidecar call failed"
    raise safe_type(safe_text)


async def create_session_safely(client: SidecarClient, **kwargs: Any) -> str:
    """Create keyed sessions without upstream logging untrusted HTTP errors.

    ``api_key`` selects the user-credential path (source ``user``);
    ``server_api_key`` passes an admin-stored server provider key (#347)
    while keeping the session classified as server-owned — the chat flow
    uses it so stored provider keys reach chat sessions exactly like they
    reach one-shot AI calls.
    """
    server_key = kwargs.pop("server_api_key", None)
    key = kwargs.get("api_key")
    if server_key is not None:
        if key is not None:
            raise ValueError("Session cannot carry both user and server API keys")
        if error := _validate_api_key(server_key):
            raise ValueError(error)
        from rootcoz.storage import create_ai_session_with_source

        body = {
            "provider": kwargs["provider"],
            "model": kwargs["model"],
            "system_prompt": kwargs["system_prompt"],
            "cwd": kwargs.get("cwd") or tempfile.gettempdir(),
            "api_key": server_key,
        }
        for field in ("agent_dir", "custom_tools", "tools"):
            if kwargs.get(field) is not None:
                body[field] = kwargs[field]

        async def create_server_keyed() -> str:
            response = await client._client.post("/sessions", json=body)
            response.raise_for_status()
            return response.json()["session_id"]

        return await create_ai_session_with_source(
            lambda: _create_with_refusal_mapping(
                create_server_keyed, kwargs["provider"], kwargs["model"]
            ),
            ai_username.get(),
            kwargs["provider"],
            "server",
            client.delete_session,
        )
    if key is None:
        from rootcoz.storage import create_ai_session_with_source

        return await create_ai_session_with_source(
            lambda: _create_with_refusal_mapping(
                lambda: client.create_session(**kwargs),
                kwargs["provider"],
                kwargs["model"],
            ),
            ai_username.get(),
            kwargs["provider"],
            "server",
            client.delete_session,
        )
    if error := _validate_api_key(key):
        raise ValueError(error)
    # Keyed sessions use the exact registered provider, never a friendly alias
    # that could route the credential to a different endpoint.
    provider, model = kwargs["provider"], kwargs["model"]
    body = {
        "provider": provider,
        "model": model,
        "system_prompt": kwargs["system_prompt"],
        "cwd": kwargs.get("cwd") or tempfile.gettempdir(),
        "api_key": key,
    }
    for field in ("agent_dir", "custom_tools", "tools"):
        if kwargs.get(field) is not None:
            body[field] = kwargs[field]
    from rootcoz.storage import (
        create_ai_session_with_source,
        get_user_ai_credential_with_generation,
    )

    username = ai_username.get()
    selected = _selected_key_generation.get()
    if selected is not None and selected[:3] == (username, provider, key):
        generation = selected[3]
    else:
        current_key, current_generation = await get_user_ai_credential_with_generation(
            username, provider
        )
        if current_key != key or current_generation is None:
            raise ValueError("AI credential changed during session creation")
        generation = current_generation

    async def create() -> str:
        response = await client._client.post("/sessions", json=body)
        response.raise_for_status()
        return response.json()["session_id"]

    return await create_ai_session_with_source(
        lambda: _create_with_refusal_mapping(create, provider, model),
        username,
        provider,
        "user",
        client.delete_session,
        generation,
    )


async def _remember_session_source(
    session_id: str, provider: str, account_id: int | None
) -> None:
    """Persist server ownership or discard a session with no valid owner."""
    from rootcoz.storage import AiSessionCollisionError, save_ai_session_source

    try:
        await save_ai_session_source(
            session_id,
            ai_username.get(),
            provider,
            "server",
            account_id,
            bootstrap_admin=account_id is None,
        )
    except AiSessionCollisionError:
        # The sidecar returned an ID already owned by another session.
        raise
    except Exception:
        try:
            await get_sidecar_client().delete_session(session_id)
        except OSError, RuntimeError, ValueError, httpx.HTTPError:
            logger.warning("Unable to clean up untracked AI session")
        raise


async def delete_ai_session(client: SidecarClient, session_id: str) -> None:
    """Delete a sidecar session and invalidate its local ownership record."""
    await client.delete_session(session_id)
    from rootcoz.storage import revoke_ai_session_source

    try:
        await revoke_ai_session_source(session_id)
    except sqlite3.Error, OSError, RuntimeError:
        logger.warning("Unable to revoke AI session mapping")


async def _prompt_safely(
    client: SidecarClient, session_id: str, message: str, timeout: float | None
) -> AIResult:
    """Mirror the sidecar prompt result without logging untrusted response fields."""
    response = await client._client.post(
        f"/sessions/{session_id}/prompt",
        json={"message": message},
        timeout=timeout or client._client.timeout,
    )
    if response.status_code != 200:
        try:
            payload = response.json()
            error = (
                payload.get("error", response.text)
                if isinstance(payload, dict)
                else response.text
            )
        except ValueError:
            error = response.text or f"HTTP {response.status_code}"
        return AIResult(success=False, text=error, error=error)
    data = response.json()
    usage_data = data.get("usage", {})
    usage = AITokenUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_write_tokens=usage_data.get("cache_write_tokens", 0),
        cost_usd=usage_data.get("cost_usd"),
        # pi-sidecar marks a numeric cost that covers only some turns; dropping the
        # flag here would store a lower bound as a complete cost.
        cost_partial=bool(usage_data.get("cost_partial", False)),
        duration_ms=usage_data.get("duration_ms"),
    )
    error = data.get("error")
    if error:
        return AIResult(
            success=False, text=data.get("text") or error, usage=usage, error=error
        )
    return AIResult(success=True, text=data.get("text", ""), usage=usage)


async def _call_user_session(
    prompt: str,
    *,
    ai_provider: str,
    ai_model: str,
    api_key: str | None = None,
    session_id: str | None = None,
    once: bool = False,
    **kwargs: Any,
) -> AIResult:
    """Avoid the sidecar convenience wrapper, which logs raw resumed-session errors."""
    client = get_sidecar_client()
    created = False
    try:
        if not session_id:
            session_id = await create_session_safely(
                client,
                provider=ai_provider,
                model=ai_model,
                system_prompt=kwargs.get("system_prompt")
                or "You are a helpful assistant.",
                cwd=kwargs.get("cwd") or tempfile.gettempdir(),
                agent_dir=kwargs.get("agent_dir"),
                custom_tools=kwargs.get("custom_tools"),
                tools=kwargs.get("tools"),
                api_key=api_key,
            )
            created = True
        if not session_id:
            raise ValueError("Missing AI session ID")
        timeout = kwargs.get("ai_call_timeout")
        result = await _prompt_safely(
            client, session_id, prompt, timeout * 60.0 if timeout else None
        )
        result.session_id = session_id
        # Correlation handle for recorded transcripts (#328): the JSONL
        # file is keyed by this id and survives session deletion.
        result.transcript_session_id = session_id
    except (
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        # Stash before cleanup clears it: a created-then-deleted session may
        # still have a transcript (header + partial turn) worth correlating.
        failed_session_id = session_id
        if created and session_id:
            try:
                await delete_ai_session(client, session_id)
                session_id = None
            except httpx.HTTPError, OSError, RuntimeError, ValueError:
                logger.warning("Unable to clean up failed AI session")
        # The outer wrapper replaces all failure details for keyed sessions.
        failed = AIResult(
            success=False, text=str(exc), error=str(exc), session_id=session_id
        )
        # The transcript correlation id survives the failure: the JSONL file
        # is keyed by it and recording is independent of call success.
        if failed_session_id:
            failed.transcript_session_id = failed_session_id
        return failed
    if once or (created and not result.success):
        try:
            await delete_ai_session(client, session_id)
            result.session_id = None
        except httpx.HTTPError, OSError, RuntimeError, ValueError:
            logger.warning("Unable to clean up AI session")
    return result


async def call_ai(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    # user key drives the credential source; a server key (#347) only
    # replaces the deployment's env/ambient credentials inside the session.
    user_key = None
    server_key = None
    if not kwargs.get("session_id"):
        if _selected_credential_source.get() != "server":
            user_key = await session_key(provider)
        if user_key is None:
            server_key = await server_provider_key(provider)
        if (key := user_key or server_key) is not None:
            kwargs["api_key"] = key
    else:
        kwargs.pop("api_key", None)
    username = ai_username.get()
    sidecar_call = _call_user_session if user_key is not None else _call_ai
    session_id = kwargs.get("session_id")
    if session_id:
        from rootcoz.storage import get_ai_session_source

        try:
            source = await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        except sqlite3.Error, OSError, RuntimeError, KeyError, TypeError:
            logger.warning("Unable to verify AI session ownership")
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        if source == "unknown" or (
            chat_session_source.get() and source != chat_session_source.get()
        ):
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
    else:
        source = "user" if user_key is not None else "server"
        if chat_session_source.get() and source != chat_session_source.get():
            logger.info("Chat AI call rejected: selected credential source unavailable")
            return AIResult(
                success=False,
                text="Chat credential unavailable; Start a new chat",
                error="Chat credential unavailable; Start a new chat",
            )
    if source == "server":
        await require_server_provider_grant(provider, model)
    if not session_id and source == "server":
        from rootcoz.storage import get_user_by_username

        account = await get_user_by_username(username) if username else None
        if account is None and username != "admin":
            raise LookupError(
                "AI session owner account changed during session creation"
            )
        account_id = account["id"] if account else None
    key = user_key if user_key is not None else server_key
    if session_id and source == "user":
        # Only the current owner's provider key can redact a keyed session.
        key = await session_key(provider)
        if key is None:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        try:
            await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        sidecar_call = _call_user_session
    if session_id and source == "server" and server_key is None:
        # A resumed server session keeps its creation-time key in the
        # sidecar; fetch the stored one so error redaction can strip it.
        server_key = await server_provider_key(provider)
        if key is None:
            key = server_key
    result = await _call_with_safe_error(
        sidecar_call,
        *args,
        redact_key=key if key is not None else "" if session_id and username else None,
        ai_provider=provider,
        ai_model=model,
        **kwargs,
    )
    if session_id and source == "user" and result.success:
        try:
            await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
    if source == "server" and result.success and chat_session_source.get():
        try:
            await require_server_provider_grant(provider, model)
        except InactiveServerProviderError as exc:
            logger.info("Chat AI reply discarded: provider deactivated mid-chat")
            return AIResult(success=False, text=str(exc), error=str(exc))
        except ExcludedServerModelError as exc:
            logger.info("Chat AI reply discarded: model deselected mid-chat")
            return AIResult(success=False, text=str(exc), error=str(exc))
        except ValueError:
            logger.info("Chat AI reply discarded after server grant changed")
            return AIResult(
                success=False,
                text="Server provider access requires an administrator grant",
                error="Server provider access requires an administrator grant",
            )
    result.credential_source = source
    if not session_id and result.session_id and user_key is None:
        await _remember_session_source(result.session_id, provider, account_id)
    return result


async def call_ai_once(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar once with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    user_key = None
    if _selected_credential_source.get() != "server":
        user_key = await session_key(provider)
    server_key = None if user_key is not None else await server_provider_key(provider)
    key = user_key if user_key is not None else server_key
    if user_key is not None:
        kwargs["api_key"] = key
        kwargs["once"] = True
        sidecar_call = _call_user_session
    else:
        if server_key is not None:
            kwargs["api_key"] = server_key
        await require_server_provider_grant(provider, model)
        # The library's call_ai_once wipes result.session_id before usage
        # recording sees it; use the multi-turn call and clean up here so
        # the session id survives for transcript correlation (#328).
        sidecar_call = _call_ai
    result = await _call_with_safe_error(
        sidecar_call,
        *args,
        redact_key=key,
        ai_provider=provider,
        ai_model=model,
        **kwargs,
    )
    if user_key is None and result.session_id:
        result.transcript_session_id = result.session_id
        try:
            await delete_ai_session(get_sidecar_client(), result.session_id)
            result.session_id = None
        except httpx.HTTPError, OSError, RuntimeError, ValueError:
            logger.warning("Unable to clean up one-shot AI session")
    result.credential_source = "user" if user_key is not None else "server"
    return result


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
    "ANALYSIS_BUILTIN_TOOLS",
    "CHAT_BUILTIN_TOOLS",
    "AIResult",
    "_setup_usage_recorder",
    "ambient_api_key_var",
    "call_ai",
    "call_ai_once",
    "clear_cursor_auth_cache",
    "format_chat_ai_user_error",
    "list_models",
    "normalize_provider",
    "probe_cursor_auth",
    "public_provider_name",
    "resolve_catalog_pair",
]
