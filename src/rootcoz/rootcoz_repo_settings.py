"""Load and validate ``.rootcoz/settings.json`` from a cloned test repo.

Priority for analysis settings (per field):
1. Explicit request (CLI / API / UI)
2. ``.rootcoz/settings.json`` from the cloned tests repo
3. Server default (env / Admin DB)
4. Fail when a required value is still missing
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from simple_logger.logger import get_logger

from rootcoz.ai_client import VALID_AI_PROVIDERS, normalize_provider
from rootcoz.config import Settings, parse_additional_repos, parse_peer_configs
from rootcoz.models import (
    AdditionalRepo,
    AiConfigEntry,
    BaseAnalysisRequest,
    NormalizedAiProvider,
)

logger = get_logger(name=__name__)

ROOTCOZ_SETTINGS_FILENAME = "settings.json"
ROOTCOZ_SETTINGS_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "rootcoz-settings.schema.json"
)
# Hard cap for untrusted .rootcoz/settings.json from a cloned tests repo.
MAX_SETTINGS_JSON_BYTES = 256 * 1024


# Allowed top-level keys — keep in sync with RootcozRepoSettings fields.
ROOTCOZ_SETTINGS_KEYS = frozenset(
    {
        "ai_provider",
        "ai_model",
        "ai_call_timeout",
        "max_concurrent_ai_calls",
        "peer_ai_configs",
        "peer_analysis_max_rounds",
        "additional_repos",
    }
)


class RootcozPeerConfig(BaseModel):
    """Peer AI entry for ``.rootcoz/settings.json`` (no secrets / unknown keys)."""

    model_config = ConfigDict(extra="forbid")

    ai_provider: NormalizedAiProvider
    ai_model: str = Field(min_length=1)

    @field_validator("ai_model")
    @classmethod
    def _ai_model_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ai_model must not be blank")
        return v


class RootcozAdditionalRepo(BaseModel):
    """Additional repo entry for ``.rootcoz/settings.json`` (no secrets)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    ref: str = Field(default="")

    @field_validator("name", "url", "ref")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return AdditionalRepo.name_not_blank(v)

    @field_validator("url")
    @classmethod
    def _url_valid(cls, v: str) -> str:
        if not v:
            raise ValueError("url must not be blank")
        # Ensure the URL is acceptable to AdditionalRepo (HttpUrl)
        try:
            AdditionalRepo.model_validate({"name": "x", "url": v})
        except ValidationError as exc:
            # Never embed the raw URL — it may contain credentials in userinfo.
            raise ValueError("invalid additional_repos url") from exc
        # Match RepositoryManager.clone_into allowlist (https:// / git:// only).
        try:
            from rootcoz.repository import _validate_repo_url

            _validate_repo_url(v)
        except ValueError as exc:
            raise ValueError("invalid additional_repos url") from exc
        return v


class RootcozRepoSettings(BaseModel):
    """Schema for ``.rootcoz/settings.json``.

    All keys optional. Unknown keys and secret fields (e.g. ``token``) are
    rejected via ``extra='forbid'``.
    """

    model_config = ConfigDict(extra="forbid")

    ai_provider: NormalizedAiProvider | None = None
    ai_model: str | None = None
    ai_call_timeout: int | None = Field(default=None, gt=0)
    max_concurrent_ai_calls: int | None = Field(default=None, gt=0)
    peer_ai_configs: list[RootcozPeerConfig] | None = None
    peer_analysis_max_rounds: int | None = Field(default=None, ge=1, le=10)
    additional_repos: list[RootcozAdditionalRepo] | None = None

    @field_validator("ai_model")
    @classmethod
    def _ai_model_strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("ai_model must not be blank when set")
        return stripped

    @field_validator("additional_repos")
    @classmethod
    def _unique_additional_repo_names(
        cls,
        v: list[RootcozAdditionalRepo] | None,
    ) -> list[RootcozAdditionalRepo] | None:
        if v is None:
            return v
        names = [ar.name for ar in v]
        dupes = sorted(n for n, count in Counter(names).items() if count > 1)
        if dupes:
            raise ValueError(f"Duplicate additional repo names: {', '.join(dupes)}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_and_normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        unknown = sorted(set(data) - ROOTCOZ_SETTINGS_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown keys in .rootcoz/settings.json: {', '.join(unknown)}"
            )
        return data


_SETTINGS_ADAPTER: TypeAdapter[RootcozRepoSettings] = TypeAdapter(RootcozRepoSettings)

# Cap how many validation issues we surface (avoid huge error blobs).
_MAX_SETTINGS_VALIDATION_ERRORS = 10


class RootcozSettingsError(ValueError):
    """Invalid or unreadable ``.rootcoz/settings.json``."""


def _sanitized_validation_message(exc: ValidationError) -> str:
    """Build a settings.json error from loc + msg only (never input values)."""
    parts: list[str] = []
    for err in exc.errors()[:_MAX_SETTINGS_VALIDATION_ERRORS]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = str(err.get("msg", "invalid value"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    omitted = len(exc.errors()) - _MAX_SETTINGS_VALIDATION_ERRORS
    if omitted > 0:
        parts.append(f"...and {omitted} more")
    detail = "; ".join(parts) if parts else "invalid document"
    return f"JSON Schema validation failed for {ROOTCOZ_SETTINGS_FILENAME}: {detail}"


@dataclass(frozen=True)
class EffectiveRepoAnalysisSettings:
    """Resolved analysis knobs after request → settings.json → server merge."""

    settings: Settings
    ai_provider: str
    ai_model: str
    peer_ai_configs: list | None
    additional_repos: list[AdditionalRepo]


def rootcoz_settings_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for ``.rootcoz/settings.json``."""
    schema = _SETTINGS_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "rootcoz .rootcoz/settings.json"
    schema["description"] = (
        "Non-sensitive per-repo analysis settings. "
        "Priority for all keys: request (CLI/API/UI) > this file > server defaults."
    )
    schema["additionalProperties"] = False
    return schema


def load_rootcoz_repo_settings(tests_repo_path: Path) -> RootcozRepoSettings | None:
    """Load and validate ``.rootcoz/settings.json`` from a cloned test repo.

    Returns:
        Parsed settings, or ``None`` when the file is absent.

    Raises:
        RootcozSettingsError: File exists but is invalid JSON or fails schema,
            or the path is a symlink / escapes the repo tree.
    """
    path = tests_repo_path / ".rootcoz" / ROOTCOZ_SETTINGS_FILENAME
    # Untrusted clone: reject symlinks before exists() so dangling links fail
    # loudly instead of being treated as "file absent".
    if path.is_symlink():
        raise RootcozSettingsError(
            f"{ROOTCOZ_SETTINGS_FILENAME} must be a regular file "
            "(symlinks are not allowed)"
        )
    if not path.exists():
        return None
    if not path.is_file():
        raise RootcozSettingsError(
            f"{ROOTCOZ_SETTINGS_FILENAME} must be a regular file "
            "(directories and other non-file paths are not allowed)"
        )
    try:
        resolved = path.resolve(strict=True)
        repo_root = tests_repo_path.resolve(strict=True)
    except OSError as exc:
        raise RootcozSettingsError(
            f"Failed to resolve {ROOTCOZ_SETTINGS_FILENAME}"
        ) from exc
    if not resolved.is_relative_to(repo_root):
        raise RootcozSettingsError(
            f"{ROOTCOZ_SETTINGS_FILENAME} resolves outside the test repository"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RootcozSettingsError(
            f"Failed to stat {ROOTCOZ_SETTINGS_FILENAME}"
        ) from exc
    if size > MAX_SETTINGS_JSON_BYTES:
        raise RootcozSettingsError(
            f"{ROOTCOZ_SETTINGS_FILENAME} exceeds maximum size "
            f"({MAX_SETTINGS_JSON_BYTES} bytes)"
        )
    try:
        # Read with an explicit byte cap to avoid TOCTOU growth after the size check.
        with path.open("rb") as fh:
            raw_bytes = fh.read(MAX_SETTINGS_JSON_BYTES + 1)
    except OSError as exc:
        raise RootcozSettingsError(
            f"Failed to read {ROOTCOZ_SETTINGS_FILENAME}"
        ) from exc
    if len(raw_bytes) > MAX_SETTINGS_JSON_BYTES:
        raise RootcozSettingsError(
            f"{ROOTCOZ_SETTINGS_FILENAME} exceeds maximum size "
            f"({MAX_SETTINGS_JSON_BYTES} bytes)"
        )
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise RootcozSettingsError(
            f"Invalid encoding in {ROOTCOZ_SETTINGS_FILENAME}"
        ) from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RootcozSettingsError(
            f"Invalid JSON in {ROOTCOZ_SETTINGS_FILENAME}"
        ) from exc
    try:
        return _SETTINGS_ADAPTER.validate_python(data)
    except ValidationError as exc:
        # Surface loc + msg only — never embed input/ctx (may contain secrets).
        raise RootcozSettingsError(_sanitized_validation_message(exc)) from exc


def _request_set_ai_provider(body: BaseAnalysisRequest) -> bool:
    return bool(body.ai_provider and str(body.ai_provider).strip())


def _request_set_ai_model(body: BaseAnalysisRequest) -> bool:
    return bool(body.ai_model and str(body.ai_model).strip())


def _peer_configs_from_repo(repo: RootcozRepoSettings) -> list | None:
    if repo.peer_ai_configs is None:
        return None
    if not repo.peer_ai_configs:
        return None  # empty list in file = no peers
    return [
        AiConfigEntry(ai_provider=p.ai_provider, ai_model=p.ai_model)
        for p in repo.peer_ai_configs
    ]


def _additional_repos_from_repo(
    repo: RootcozRepoSettings,
) -> list[AdditionalRepo] | None:
    if repo.additional_repos is None:
        return None
    try:
        return [
            AdditionalRepo(name=r.name, url=r.url, ref=r.ref)  # type: ignore[arg-type]
            for r in repo.additional_repos
        ]
    except ValidationError as exc:
        raise RootcozSettingsError(
            "Invalid additional_repos in .rootcoz/settings.json"
        ) from exc


def assert_no_tests_repo_name_collision(
    tests_dir_name: str | None,
    additional_repos: list[AdditionalRepo],
) -> None:
    """Fail when an additional repo name matches the tests clone directory.

    Called after ``settings.json`` may replace ``additional_repos``, so the
    final list is checked against the already-cloned tests repo dirname.
    """
    if not tests_dir_name:
        return
    for ar in additional_repos:
        if ar.name == tests_dir_name:
            raise RootcozSettingsError(
                f"additional_repos contains name '{ar.name}' which collides "
                "with the tests repo clone directory; choose a different name"
            )


def validate_effective_ai_provider(provider: str) -> None:
    """Reject unsupported AI providers after settings overlay."""
    if not provider:
        return
    if provider not in VALID_AI_PROVIDERS:
        raise RootcozSettingsError(
            f"Unsupported AI provider: {provider}. "
            f"Valid providers: {', '.join(sorted(VALID_AI_PROVIDERS))}"
        )


def apply_rootcoz_repo_settings(
    body: BaseAnalysisRequest,
    settings: Settings,
    repo: RootcozRepoSettings | None,
    *,
    ai_provider: str = "",
    ai_model: str = "",
    peer_ai_configs: list | None = None,
    additional_repos: list[AdditionalRepo] | None = None,
) -> EffectiveRepoAnalysisSettings:
    """Merge request → ``settings.json`` → server into effective analysis settings.

    ``ai_provider`` / ``ai_model`` / ``peer_ai_configs`` / ``additional_repos``
    arguments are the pre-resolved request|server values (may be empty when
    AI resolution was deferred until after clone).
    """
    overrides: dict[str, Any] = {}

    # --- AI provider / model ---
    # request (CLI/API/UI) → settings.json → server (DB/env). Same as other keys.
    if _request_set_ai_provider(body):
        resolved_provider = normalize_provider(str(body.ai_provider))
    elif repo is not None and repo.ai_provider:
        resolved_provider = normalize_provider(repo.ai_provider)
    elif (ai_provider or settings.ai_provider or "").strip():
        resolved_provider = normalize_provider(ai_provider or settings.ai_provider)
    else:
        resolved_provider = ""

    if _request_set_ai_model(body):
        resolved_model = str(body.ai_model).strip()
    elif repo is not None and repo.ai_model:
        resolved_model = repo.ai_model
    elif (ai_model or settings.ai_model or "").strip():
        resolved_model = (ai_model or settings.ai_model or "").strip()
    else:
        resolved_model = ""

    # --- timeouts / concurrency / peer rounds ---
    if body.ai_call_timeout is not None:
        overrides["ai_call_timeout"] = body.ai_call_timeout
    elif repo is not None and repo.ai_call_timeout is not None:
        overrides["ai_call_timeout"] = repo.ai_call_timeout

    if body.max_concurrent_ai_calls is not None:
        overrides["max_concurrent_ai_calls"] = body.max_concurrent_ai_calls
    elif repo is not None and repo.max_concurrent_ai_calls is not None:
        overrides["max_concurrent_ai_calls"] = repo.max_concurrent_ai_calls

    if "peer_analysis_max_rounds" in body.model_fields_set:
        overrides["peer_analysis_max_rounds"] = body.peer_analysis_max_rounds
    elif repo is not None and repo.peer_analysis_max_rounds is not None:
        overrides["peer_analysis_max_rounds"] = repo.peer_analysis_max_rounds

    # --- peer configs ---
    if body.peer_ai_configs is not None:
        resolved_peers: list | None = body.peer_ai_configs or None
    elif repo is not None and repo.peer_ai_configs is not None:
        resolved_peers = _peer_configs_from_repo(repo)
    elif peer_ai_configs is not None:
        resolved_peers = peer_ai_configs
    elif settings.peer_ai_configs:
        resolved_peers = parse_peer_configs(settings.peer_ai_configs) or None
    else:
        resolved_peers = None

    # --- additional repos ---
    if body.additional_repos is not None:
        resolved_additional = list(body.additional_repos)
    elif repo is not None and repo.additional_repos is not None:
        resolved_additional = _additional_repos_from_repo(repo) or []
    elif additional_repos is not None:
        resolved_additional = list(additional_repos)
    else:
        parsed = parse_additional_repos(settings.additional_repos)
        resolved_additional = [AdditionalRepo(**r) for r in parsed] if parsed else []

    merged = settings
    if overrides:
        merged_data = settings.model_dump(mode="python") | overrides
        merged = Settings(**merged_data)

    validate_effective_ai_provider(resolved_provider)

    return EffectiveRepoAnalysisSettings(
        settings=merged,
        ai_provider=resolved_provider,
        ai_model=resolved_model,
        peer_ai_configs=resolved_peers,
        additional_repos=resolved_additional,
    )


# Fields from settings.json that enrichment (and other callers) must see on the
# same Settings instance passed into CI source analysis / process_analysis_with_id.
_REPO_SETTINGS_OVERLAY_FIELDS: tuple[str, ...] = (
    "ai_call_timeout",
    "max_concurrent_ai_calls",
    "peer_analysis_max_rounds",
)


def propagate_repo_settings_overlay(
    caller_settings: Settings, effective_settings: Settings
) -> None:
    """Copy settings.json overlay knobs onto the caller's Settings instance.

    Analysis rebinds a local ``settings`` to ``effective.settings``, which
    does not update the caller's reference. Enrichment in
    ``process_analysis_with_id`` still holds the original object — mutate it so
    timeouts / concurrency / peer rounds match analysis.
    """
    for field_name in _REPO_SETTINGS_OVERLAY_FIELDS:
        setattr(
            caller_settings,
            field_name,
            getattr(effective_settings, field_name),
        )


def effective_repo_settings_request_params_patch(
    effective: EffectiveRepoAnalysisSettings,
) -> dict[str, Any]:
    """Build ``request_params`` fields reflecting post-overlay effective values."""
    peers = effective.peer_ai_configs or []
    return {
        "ai_provider": effective.ai_provider,
        "ai_model": effective.ai_model,
        "peer_ai_configs": [
            c.model_dump() if hasattr(c, "model_dump") else c for c in peers
        ],
        "additional_repos": [
            ar.model_dump(mode="json") if hasattr(ar, "model_dump") else ar
            for ar in effective.additional_repos
        ],
        "ai_call_timeout": effective.settings.ai_call_timeout,
        "max_concurrent_ai_calls": effective.settings.max_concurrent_ai_calls,
        "peer_analysis_max_rounds": effective.settings.peer_analysis_max_rounds,
    }


async def persist_effective_repo_settings(
    job_id: str, effective: EffectiveRepoAnalysisSettings
) -> None:
    """Rewrite stored ``request_params`` with values after ``settings.json`` overlay.

    Enqueue-time ``request_params`` are a pre-clone snapshot. Without this patch,
    ``results show`` keeps looking like ``settings.json`` was ignored.

    Nested ``additional_repos[].token`` values are encrypted before write so the
    overlay path matches enqueue-time at-rest encryption.

    Best-effort: storage failures are logged and swallowed so analysis continues.
    """
    from rootcoz.encryption import encrypt_sensitive_fields
    from rootcoz.storage import patch_result_json

    patch = encrypt_sensitive_fields(
        effective_repo_settings_request_params_patch(effective)
    )

    def _apply(result: dict) -> None:
        params = result.get("request_params")
        if not isinstance(params, dict):
            params = {}
            result["request_params"] = params
        params.update(patch)

    try:
        await patch_result_json(job_id, _apply)
    except Exception:
        logger.warning(
            "Failed to persist settings.json overlay into request_params (job_id=%s)",
            job_id,
            exc_info=True,
        )


def write_rootcoz_settings_schema(path: Path | None = None) -> Path:
    """Write the JSON Schema file (used by tests / packaging)."""
    out = path or ROOTCOZ_SETTINGS_SCHEMA_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(rootcoz_settings_json_schema(), indent=2) + "\n",
        encoding="utf-8",
    )
    return out


def resolve_tests_repo_url(body: BaseAnalysisRequest, settings: Settings) -> str:
    """Resolve tests repo URL: explicit request value wins (even if empty).

    When ``tests_repo_url`` is present on the request body (including ``\"\"``),
    that value is authoritative and server settings are not used. When omitted,
    fall back to ``settings.tests_repo_url``.
    """
    if "tests_repo_url" in body.model_fields_set:
        return str(body.tests_repo_url or "").strip()
    return str(settings.tests_repo_url or "").strip()


def tests_repo_available(body: BaseAnalysisRequest, settings: Settings) -> bool:
    """True when a tests repo URL is available (request or server)."""
    return bool(resolve_tests_repo_url(body, settings))


async def resolve_repo_analysis_settings(
    job_id: str | None,
    tests_repo_path: Path | None,
    body: BaseAnalysisRequest,
    settings: Settings,
    *,
    ai_provider: str = "",
    ai_model: str = "",
    peer_ai_configs: list | None = None,
    additional_repos: list[AdditionalRepo] | None = None,
) -> EffectiveRepoAnalysisSettings:
    """Single entrypoint: load ``settings.json``, merge, propagate, persist.

    Used by every analysis path (Jenkins, file, raw, re-analyze). Sources must
    not implement their own merge — call this once after the tests repo clone.

    ``request_params`` are rewritten only when a ``settings.json`` file was
    actually loaded, so enqueue-time snapshots are not wiped on a no-op merge.

    Raises:
        RootcozSettingsError: when the file exists but fails validation.
    """
    repo = None
    if tests_repo_path is not None:
        repo = load_rootcoz_repo_settings(tests_repo_path)
        if repo is not None:
            logger.info(
                "Loaded .rootcoz/settings.json from %s",
                tests_repo_path / ".rootcoz" / ROOTCOZ_SETTINGS_FILENAME,
            )
    effective = apply_rootcoz_repo_settings(
        body,
        settings,
        repo,
        ai_provider=ai_provider,
        ai_model=ai_model,
        peer_ai_configs=peer_ai_configs,
        additional_repos=additional_repos,
    )
    propagate_repo_settings_overlay(settings, effective.settings)
    if job_id and repo is not None:
        await persist_effective_repo_settings(job_id, effective)
    return effective
