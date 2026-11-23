"""Deep AI provider catalog, grant enforcement, and credential-source resolution.

This module is the single architectural owner of:
- User-scoped model catalog assembly (:class:`ScopedCatalog`, :func:`load_scoped_catalog`)
- Explicit provider/model + credential-source resolution (:class:`ResolvedAiSelection`, :func:`resolve_ai_selection`)
- Explicit server-provider and free-model grant enforcement (:func:`enforce_server_provider_grant`, :func:`check_server_provider_grant`)
- Ambient AI execution ContextVar scoping (:func:`scoped_ai_context`, :func:`current_pinned_credential_source`)

Backward-compatible ContextVars stay declared on :mod:`rootcoz.ai_client` solely
as backing storage for legacy callers and tests; all production callers outside
this module pass explicit value objects instead of reading side-channel state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from rootcoz import ai_client, get_child_logger

if TYPE_CHECKING:
    from rootcoz.config import Settings

logger = get_child_logger(__name__)

CredentialSource = Literal["user", "server"]


@dataclass(frozen=True, slots=True)
class ScopedCatalog:
    """Immutable user-scoped AI model catalog and per-provider listing status."""

    providers: dict[str, list[dict[str, Any]]]
    provider_status: dict[str, dict[str, bool | int]]
    rejected_user_keys: frozenset[str] = frozenset()

    def resolve_provider_spelling(self, provider: str) -> str:
        """Adopt the catalog's exact spelling for *provider*, case-insensitively.

        Checks both ``providers`` and ``provider_status`` so manual-only and
        unavailable user-key providers preserve exact custom provider casing
        (e.g. ``EnMaaS``, ``MyGateway``). Ambiguous case-distinct variants
        (e.g. ``Foo`` and ``foo``) or unknown names fall through unchanged.
        """
        return ai_client._catalog_provider_spelling(
            (*self.providers.keys(), *self.provider_status.keys()),
            provider,
        )

    def models_for(
        self, provider: str
    ) -> tuple[str, list[dict[str, Any]], dict[str, bool | int] | None]:
        """Resolve *provider* and return ``(exact_provider, models, status_entry)``.

        Preserves exact mixed-case custom provider IDs on unambiguous
        case-insensitive matches, and refuses to silently pick between
        ambiguous case-distinct catalog entries unless queried with an exact
        spelling.
        """
        cleaned = (provider or "").strip()
        normalized = ai_client.normalize_provider(cleaned)
        if not cleaned:
            return "", [], None
        matches = {
            p
            for p in (*self.providers.keys(), *self.provider_status.keys())
            if isinstance(p, str) and p and p.lower() == normalized
        }
        if len(matches) == 1:
            exact = next(iter(matches))
        elif len(matches) > 1:
            if cleaned in matches:
                exact = cleaned
            else:
                return normalized, [], None
        else:
            exact = normalized
        return exact, self.providers.get(exact, []), self.provider_status.get(exact)


@dataclass(frozen=True, slots=True)
class ResolvedAiSelection:
    """Validated AI provider/model pair bound to its resolved credential source."""

    provider: str
    model: str
    credential_source: CredentialSource

    def as_pair(self) -> tuple[str, str]:
        """Return the resolved ``(provider, model)`` tuple."""
        return self.provider, self.model


@contextmanager
def scoped_ai_context(
    username: str | None = None,
    *,
    credential_source: str | None = None,
) -> Iterator[None]:
    """Bind ambient AI execution ContextVars for the duration of a block.

    Always resets every token it sets in ``finally``, even on exception.
    """
    user_token = ai_client.ai_username.set(username) if username is not None else None
    source_token = (
        ai_client.chat_session_source.set(credential_source)
        if credential_source is not None
        else None
    )
    try:
        yield
    finally:
        if source_token is not None:
            ai_client.chat_session_source.reset(source_token)
        if user_token is not None:
            ai_client.ai_username.reset(user_token)


def current_pinned_credential_source() -> str:
    """Return the pinned credential source for the active chat context, if any."""
    return ai_client.chat_session_source.get()


def validate_pinned_source(
    selection: ResolvedAiSelection,
    pinned_source: str | None = None,
) -> None:
    """Raise ``ValueError`` when *selection* disagrees with the pinned chat source."""
    pinned = (
        current_pinned_credential_source() if pinned_source is None else pinned_source
    )
    if pinned and selection.credential_source != pinned:
        raise ValueError("Chat credential source changed during Start")


def resolve_provider_model_values(
    ai_provider: str | None = None,
    ai_model: str | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Resolve AI provider and model without catalog validation.

    Resolution order (first non-empty wins):
    1. Per-request value (``ai_provider`` / ``ai_model`` arguments)
    2. Merged settings (env var base + DB overrides via ``get_settings()``)
    """
    if settings is None:
        from rootcoz.config import get_settings

        settings = get_settings()
    provider = ai_client.normalize_provider(ai_provider or settings.ai_provider)
    model = ai_model or settings.ai_model
    return provider, model


def _record_selection(selection: ResolvedAiSelection) -> ResolvedAiSelection:
    """Mirror *selection* to the legacy ContextVar at this single write boundary."""
    ai_client._selected_credential_source.set(selection.credential_source)
    return selection


def coerce_legacy_selection(
    result: ResolvedAiSelection | tuple[str, str],
    *,
    unspecified_source: Literal["user", "server", ""] = "server",
) -> ResolvedAiSelection:
    """Adapt a ``ResolvedAiSelection`` or legacy ``(provider, model)`` tuple.

    Used only when a test monkeypatches a legacy tuple-returning hook such as
    ``resolve_catalog_pair``, ``_validate_catalog_pair``, or
    ``_validate_chat_selection``.
    """
    if isinstance(result, ResolvedAiSelection):
        return result
    resolved_provider, resolved_model = result
    raw_source = ai_client._selected_credential_source.get()
    source = raw_source if raw_source in ("user", "server") else unspecified_source
    return ResolvedAiSelection(
        provider=resolved_provider,
        model=resolved_model,
        credential_source=cast(CredentialSource, source),
    )


async def load_scoped_catalog(username: str = "") -> ScopedCatalog:
    """Build the immutable :class:`ScopedCatalog` usable by *username* or server.

    Never reads or mutates ambient ContextVars. Reads sidecar/catalog helpers
    via ``ai_client`` attributes so existing monkeypatches continue to apply.
    """
    catalog = await ai_client._get_model_catalog()
    status: dict[str, dict[str, bool | int]] = {}
    rejected_keys: set[str] = set()
    from rootcoz.storage import can_user_use_server_providers

    try:
        allowed = await can_user_use_server_providers(username) if username else False
    except sqlite3.Error, OSError, RuntimeError, ValueError, KeyError, TypeError:
        allowed = False
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    active = ai_client._active_server_provider_entries()
    if active is not None:
        ai_client.logger.debug(
            "active_server_providers filtering active: %s", sorted(active)
        )
    # Admin-managed server models (#347): a managed provider (stored selection
    # or stored server key) exposes only its selected models on server
    # credentials; a never-managed provider keeps its full catalog.
    selection = await ai_client._load_server_pool_selection()
    if selection is None:
        selection = (set(), set())
    selected_providers, selected_pairs = selection
    # Key-scoped listings captured at add/rotate (#350): for a keyed provider
    # the stored listing replaces the static catalog as the server pool's
    # model set.
    key_scoped: dict[str, dict[str, dict[str, Any]]] = {}
    key_scoped_ids: dict[str, str] = {}
    declared_session_models = ai_client._manifest_session_models()
    try:
        from rootcoz.storage import list_server_provider_key_models

        for row in await list_server_provider_key_models():
            key_scoped.setdefault(row["provider"].lower(), {})[row["model"]] = row
            key_scoped_ids.setdefault(row["provider"].lower(), row["provider"])
    except sqlite3.Error, OSError:
        ai_client.logger.warning("Unable to load key-scoped server provider models")

    def _key_scoped_entries(
        provider: str, scoped_rows: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        entries = [
            {
                "id": model,
                "name": row.get("name") or model,
                "provider": provider,
                "source": "api",
                "capabilities": row.get("capabilities") or {},
            }
            for model, row in sorted(scoped_rows.items())
            if model and ai_client._listing_entry_usable(row)
        ]
        for entry in entries:
            ai_client._mark_session_support(provider, entry, declared_session_models)
        return entries

    seen_catalog_providers: set[str] = set()
    for provider, entries in ai_client.build_friendly_catalog(catalog).items():
        seen_catalog_providers.add(provider.lower())
        if active is not None and not ai_client.is_server_provider_active(provider):
            continue
        has_selection = provider.lower() in selected_providers
        scoped_rows = key_scoped.get(provider.lower())
        if scoped_rows is not None:
            entries = _key_scoped_entries(provider, scoped_rows)
        for entry in entries:
            if has_selection and (provider.lower(), entry["id"]) not in selected_pairs:
                continue
            pairs[(provider, entry["id"])] = {
                **entry,
                "credential_sources": ["server"],
                "can_use_server_providers": allowed,
                "verified": True,
            }
    for provider_lower, scoped_rows in key_scoped.items():
        if provider_lower in seen_catalog_providers:
            continue
        provider = key_scoped_ids.get(provider_lower, provider_lower)
        if active is not None and not ai_client.is_server_provider_active(provider):
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
    if username:
        from rootcoz.storage import get_user_ai_credentials

        credentials = await get_user_ai_credentials(username)
        supported = await ai_client.supported_key_providers() if credentials else []
        for provider, key in credentials.items():
            if provider not in supported:
                continue
            try:
                discovery = await ai_client.models_for_api_key(provider, key)
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                RuntimeError,
            ) as exc:
                status[provider] = {
                    "has_api_key": True,
                    "modelListingSupported": True,
                    "unavailable": True,
                }
                if ai_client.auth_failure_status(exc):
                    rejected_keys.add(provider)
                    ai_client.logger.warning(
                        "Stored API key for provider=%s was rejected by the provider",
                        provider,
                    )
                else:
                    ai_client.logger.warning(
                        "Key-scoped model discovery unavailable for provider=%s",
                        provider,
                    )
                continue
            listing = discovery["modelListingSupported"]
            status[provider] = {"has_api_key": True, "modelListingSupported": listing}
            entries = (
                discovery["models"]
                if listing
                else [
                    {
                        "provider": provider,
                        "id": model,
                        "name": model,
                        "source": ai_client._SNAPSHOT_SOURCE,
                        "verified": False,
                    }
                    for model in ai_client._PI_MODEL_SUGGESTIONS.get(provider, ())
                ]
            )
            if not listing:
                pairs.setdefault((provider, ""), {})  # Preserve manual-only provider.
            usable: set[tuple[str, str]] = set()
            for entry in entries:
                pair = (provider, entry["id"])
                unusable = not ai_client._listing_entry_usable(entry)
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
                        "source": "api" if listing else ai_client._SNAPSHOT_SOURCE,
                        "credential_sources": ["user"],
                        "can_use_server_providers": allowed,
                        "verified": listing,
                    }
                    target = pairs[pair]
                if listing:
                    ai_client._mark_session_support(
                        provider, entry, declared_session_models, mark_on=target
                    )
                usable.add(pair)
            status[provider]["model_count"] = len(usable)
    free_pairs: set[tuple[str, str]] = set()
    try:
        from rootcoz.storage import list_free_ai_models

        free_pairs = {
            (row["provider"].lower(), row["model"])
            for row in await list_free_ai_models()
        }
    except sqlite3.Error, OSError:
        ai_client.logger.warning("Unable to load free model pairs")
    result: dict[str, list[dict[str, Any]]] = {}
    for (provider, model), entry in pairs.items():
        if (provider.lower(), model) in free_pairs:
            entry["free"] = True
        bucket = result.setdefault(provider, [])
        if model:
            bucket.append(entry)
    logger.debug(
        "Loaded scoped AI catalog for user=%s providers=%d rejected_keys=%d",
        username or "<server>",
        len(result),
        len(rejected_keys),
    )
    return ScopedCatalog(
        providers=result,
        provider_status=status,
        rejected_user_keys=frozenset(rejected_keys),
    )


async def enforce_server_provider_grant(
    username: str,
    provider: str = "",
    model: str = "",
) -> None:
    """Enforce server-pool activation, model selection, and user/free grants.

    Pure explicit grant check taking *username*, *provider*, and *model* —
    never reads or mutates ambient ContextVars.
    """
    if provider and not ai_client.is_server_provider_active(provider):
        ai_client.logger.debug(
            "Server credentials for provider=%s deactivated by active_server_providers",
            provider,
        )
        ai_client._raise_inactive_server_provider(provider)
    if provider and model:
        # Admin-deselected models are not runnable on server credentials,
        # regardless of grant — the selection is the server pool (#347).
        await ai_client.require_selected_server_pair(provider, model)
    from rootcoz.storage import (
        can_user_use_free_providers,
        can_user_use_server_providers,
    )

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
        if granted and await ai_client.is_free_ai_pair(provider, model):
            return
    raise ValueError("Server provider access requires an administrator grant")


async def check_server_provider_grant(
    username: str,
    provider: str = "",
    model: str = "",
) -> None:
    """Check server-provider grant for *username*, honoring test monkeypatches."""
    if (
        ai_client.require_server_provider_grant
        is ai_client._DEFAULT_REQUIRE_SERVER_PROVIDER_GRANT
    ):
        await enforce_server_provider_grant(username, provider, model)
        return
    with scoped_ai_context(username):
        await ai_client.require_server_provider_grant(provider, model)


async def resolve_ai_selection(
    provider: str,
    model: str,
    *,
    username: str | None = None,
) -> ResolvedAiSelection:
    """Validate an exact pair (or unambiguous legacy route) and return its source."""
    provider = ai_client.normalize_provider(provider)
    model = (model or "").strip()
    effective_user = ai_client.ai_username.get() if username is None else username

    if effective_user:
        if ai_client.scoped_models is ai_client._DEFAULT_SCOPED_MODELS:
            catalog = await load_scoped_catalog(effective_user)
        else:
            with scoped_ai_context(effective_user):
                scoped_dict = await ai_client.scoped_models()
                catalog = ScopedCatalog(
                    providers=scoped_dict,
                    provider_status=ai_client.model_listing_status.get() or {},
                    rejected_user_keys=ai_client._rejected_user_keys.get(),
                )
        provider = catalog.resolve_provider_spelling(provider)
        status = catalog.provider_status
        # Key discovery failed for this provider: the caller's own key cannot
        # serve the pair, but a server-pool entry still can (#592).
        unavailable = bool(status.get(provider, {}).get("unavailable"))
        rejected_key = provider in catalog.rejected_user_keys
        for entry in catalog.providers.get(provider, []):
            if entry["id"] == model and (
                entry["verified"] or "server" in entry["credential_sources"]
            ):
                source = cast(CredentialSource, entry["credential_sources"][0])
                if unavailable and source != "server":
                    continue
                if source == "server":
                    await check_server_provider_grant(effective_user, provider, model)
                logger.debug(
                    "Resolved AI selection user=%s provider=%s model=%s source=%s",
                    effective_user,
                    provider,
                    model,
                    source,
                )
                return _record_selection(
                    ResolvedAiSelection(
                        provider=provider,
                        model=model,
                        credential_source=source,
                    )
                )
        if rejected_key:
            raise ai_client.RejectedUserKeyError(
                f"Your stored key for provider '{provider}' was rejected by the"
                f" provider and the server pool does not serve {provider}/{model}:"
                " update or remove the key, then retry"
            )
        if unavailable:
            raise ValueError(
                "Key-scoped model discovery unavailable; retry or select server credentials"
            )
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
            logger.debug(
                "Resolved manual-only AI selection user=%s provider=%s model=%s source=user",
                effective_user,
                provider,
                model,
            )
            return _record_selection(
                ResolvedAiSelection(
                    provider=provider,
                    model=model,
                    credential_source="user",
                )
            )
        # scoped catalog withdrew this pair's server source — the administrator
        # deactivated the provider or deselected the model — so say why.
        friendly_catalog = ai_client.build_friendly_catalog(
            await ai_client._get_model_catalog()
        )
        matched_provider = ai_client._catalog_provider_spelling(
            friendly_catalog.keys(), provider
        )
        if matched_provider in friendly_catalog:
            if not ai_client.is_server_provider_active(matched_provider):
                ai_client._raise_inactive_server_provider(matched_provider)
            await ai_client.require_selected_server_pair(matched_provider, model)
        raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")

    raw_catalog = await ai_client._get_model_catalog()
    provider = ai_client._catalog_provider_spelling(
        {entry.get("provider") for entry in raw_catalog}, provider
    )
    pairs = {(entry.get("provider"), entry.get("id")) for entry in raw_catalog}
    if (provider, model) in pairs:
        await check_server_provider_grant("", provider, model)
        return _record_selection(
            ResolvedAiSelection(
                provider=provider,
                model=model,
                credential_source="server",
            )
        )

    # A stale catalog may not yet contain a newly discovered pair. Refresh once;
    # a warm catalog remains usable if that refresh is temporarily unavailable.
    raw_catalog = await ai_client._get_model_catalog(refresh=True)
    provider = ai_client._catalog_provider_spelling(
        {entry.get("provider") for entry in raw_catalog}, provider
    )
    pairs = {(entry.get("provider"), entry.get("id")) for entry in raw_catalog}
    if (provider, model) in pairs:
        await check_server_provider_grant("", provider, model)
        return _record_selection(
            ResolvedAiSelection(
                provider=provider,
                model=model,
                credential_source="server",
            )
        )

    # Legacy friendly IDs have one explicit destination each. Mapping only when
    # precisely one catalog provider has this model prevents google/Vertex guesses.
    targets = ai_client._LEGACY_PROVIDER_TARGETS.get(provider)
    providers_for_model = {p for p, m in pairs if p and m == model}
    matches = [p for p in providers_for_model if targets and p.lower() in targets]
    if len(providers_for_model) == 1 and len(matches) == 1:
        await check_server_provider_grant("", matches[0], model)
        return _record_selection(
            ResolvedAiSelection(
                provider=matches[0],
                model=model,
                credential_source="server",
            )
        )
    raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")


async def resolve_selection_via(
    resolver_fn: Callable[..., Awaitable[Any]],
    provider: str,
    model: str,
    *,
    username: str | None = None,
    default_fn: Any = None,
    reset_stale: bool = False,
    unspecified_source: Literal["user", "server", ""] = "server",
) -> ResolvedAiSelection:
    """Resolve via :func:`resolve_ai_selection`, or bridge a monkeypatched hook.

    When *resolver_fn* matches *default_fn*, bypasses legacy tuple/ContextVar
    plumbing and calls :func:`resolve_ai_selection` directly. When a test
    monkeypatched *resolver_fn*, runs it (scoping ``ai_username`` if *username*
    is supplied) and wraps the returned pair using :func:`coerce_legacy_selection`.
    """
    if default_fn is not None and resolver_fn is default_fn:
        if reset_stale:
            ai_client._selected_credential_source.set("")
        return await resolve_ai_selection(provider, model, username=username)

    if reset_stale:
        ai_client._selected_credential_source.set("")
    if username is not None:
        with scoped_ai_context(username):
            result = await resolver_fn(provider, model)
    else:
        result = await resolver_fn(provider, model)
    return coerce_legacy_selection(result, unspecified_source=unspecified_source)
